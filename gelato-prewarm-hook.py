#!/usr/bin/env python3
"""
gelato-prewarm-hook.py — Event-driven Gelato pre-warm receiver.

Listens for Jellyfin Webhook plugin "Playback Start" notifications and pre-warms
the NEXT episode in the same series via a background POST /Items/{next}/PlaybackInfo.

This populates Gelato's in-memory `streamsync` cache (per-user, per-episode) for
the next episode while the current one plays, so advancing hits ~0.02s instead of
a full addon query (~2-15s).

Config via env (all required at runtime — no secrets in this file):
  JF_BASE    Jellyfin base URL          (default http://localhost:8096)
  JF_KEY     Jellyfin API key           REQUIRED
  HOOK_PORT  listen port                (default 8800)
  LOG_DIR    log directory              (default ~/gelato-prewarm/logs)
  STATE_DIR  dedup state dir            (default ~/gelato-prewarm/state)
"""
import http.server, json, os, sys, time, urllib.request, urllib.error, datetime

BASE = os.environ.get("JF_BASE", "http://localhost:8096")
KEY = os.environ.get("JF_KEY")
if not KEY:
    print("FATAL: JF_KEY env var required (Jellyfin API key)", flush=True)
    sys.exit(1)
PORT = int(os.environ.get("HOOK_PORT", "8800"))
LOG_DIR = os.environ.get("LOG_DIR", os.path.expanduser("~/gelato-prewarm/logs"))
STATE_DIR = os.environ.get("STATE_DIR", os.path.expanduser("~/gelato-prewarm/state"))
MIN_INTERVAL_SEC = 90  # don't re-warm the same (user,item) more often than this

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "hook.log"), "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def jf(path, method="GET", body=None, timeout=60):
    """Call the Jellyfin API. Returns (status, json_or_text, elapsed_seconds)."""
    hdr = {"Authorization": f'MediaBrowser Token="{KEY}"'}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdr, method=method)
    t = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
        code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
    dt = time.time() - t
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        parsed = raw.decode("utf-8", "replace")
    return code, parsed, dt

def next_episode(series_id, current_item_id, user_id):
    """Find the episode that comes right after current_item_id in the series.
    Uses /Shows/{seriesId}/Episodes which returns ordered episodes across seasons,
    then picks the first with a later IndexNumber/Season. Returns its Id or None."""
    code, data, _ = jf(f"/Shows/{series_id}/Episodes?userId={user_id}")
    if code != 200 or not isinstance(data, dict):
        return None
    items = data.get("Items", [])
    # find current position
    idx = None
    for i, it in enumerate(items):
        if it.get("Id") == current_item_id:
            idx = i
            break
    if idx is None:
        return None
    # next episode after current in the ordered list
    for it in items[idx + 1:]:
        # skip specials (index 0) — want the actual next episode
        if it.get("Type") == "Episode" and it.get("IndexNumber", 0) >= 1:
            return it.get("Id")
    return None

def prewarm(next_id, user_id):
    """Fire the pre-warm PlaybackInfo (populates Gelato streamsync cache)."""
    code, parsed, dt = jf(f"/Items/{next_id}/PlaybackInfo?UserId={user_id}", method="POST", body={})
    ms = parsed.get("MediaSources", []) if isinstance(parsed, dict) else []
    log(f"Prewarm next={next_id} status={code} time={dt:.2f}s mediaSources={len(ms)}")
    return code

def should_run(user_id, item_id):
    """Dedup: skip if we warmed this (user,item) recently."""
    f = os.path.join(STATE_DIR, "last_warm.json")
    try:
        with open(f) as fh:
            last = json.load(fh)
    except Exception:
        last = {}
    now = time.time()
    if last.get("key") == f"{user_id}:{item_id}" and (now - last.get("ts", 0)) < MIN_INTERVAL_SEC:
        return False
    last["key"] = f"{user_id}:{item_id}"
    last["ts"] = now
    try:
        with open(f, "w") as fh:
            json.dump(last, fh)
    except Exception:
        pass
    return True

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            payload = {}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}).encode())
        # dispatch async
        try:
            self.handle_payload(payload)
        except Exception as e:
            log(f"Handler error: {e}")

    def handle_payload(self, p):
        ntype = p.get("NotificationType", "")
        if ntype != "PlaybackStart":
            return
        user_id = p.get("UserId")
        item_id = p.get("ItemId")
        series_id = p.get("SeriesId")
        ep_num = p.get("IndexNumber")
        item_type = p.get("ItemType")
        name = p.get("ItemName", "")
        log(f"Hook: type={ntype} itemType={item_type} ep={ep_num} itemId={item_id} series={series_id} name={name!r} user={user_id}")
        if not (user_id and item_id and series_id):
            log("  Missing UserId/ItemId/SeriesId — skipping")
            return
        if not should_run(user_id, item_id):
            log("  Dedup hit — skip")
            return
        if item_type not in (None, "Episode"):
            log(f"  ItemType={item_type} not an Episode — skip")
            return
        nxt = next_episode(series_id, item_id, user_id)
        if not nxt:
            log("  No next episode found")
            return
        prewarm(nxt, user_id)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"gelato-prewarm-hook alive")
        log("GET health check")

    def log_message(self, fmt, *args):
        pass  # quiet

if __name__ == "__main__":
    log(f"Starting gelato-prewarm-hook on port {PORT} -> Jellyfin {BASE}")
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()
