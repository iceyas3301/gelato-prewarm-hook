#!/usr/bin/env python3
"""
gelato-prewarm-hook.py — Event-driven Gelato stream and subtitle pre-warm receiver.

Listens for Jellyfin Webhook plugin "Playback Start" notifications and pre-warms
the NEXT episode in the same series:
1. Calls POST /Items/{next}/PlaybackInfo to populate Gelato's in-memory streamsync cache.
2. Identifies embedded text subtitles and triggers background extraction via the Jellyfin
   subtitle stream endpoint so that subtitle files are cached locally before the user advances.

Config via env. The systemd unit supplies JF_BASE and JF_KEY — no secrets live in this file:
  JF_BASE    Jellyfin base URL          REQUIRED in practice (the unit sets it)
  JF_KEY     Jellyfin API key           REQUIRED (exits if unset)
  HOOK_PORT  listen port                default 8800
  LOG_DIR    log directory              default ~/gelato-prewarm/logs
  STATE_DIR  dedup state dir            default ~/gelato-prewarm/state
"""
import http.server
import json
import os
import sys
import time
import urllib.request
import urllib.error
import datetime
import threading

BASE = os.environ.get("JF_BASE", "http://<JELLYFIN_HOST>:8096")
KEY = os.environ.get("JF_KEY")
if not KEY:
    print("FATAL: JF_KEY env var required (Jellyfin API key)", flush=True)
    sys.exit(1)
PORT = int(os.environ.get("HOOK_PORT", "8800"))
LOG_DIR = os.environ.get("LOG_DIR", os.path.expanduser("~/gelato-prewarm/logs"))
STATE_DIR = os.environ.get("STATE_DIR", os.path.expanduser("~/gelato-prewarm/state"))
MIN_INTERVAL_SEC = 90  # don't re-warm the same (user,item) more often than this
SUBTITLE_CACHE_BASE = os.path.expanduser("~/docker/jellyfin/config/data/subtitles")

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


def jf(path, method="GET", body=None, timeout=60, json_out=True):
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
    except Exception as e:
        return 0, str(e), time.time() - t
    dt = time.time() - t
    if not json_out:
        return code, raw, dt
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        parsed = raw.decode("utf-8", "replace")
    return code, parsed, dt


def next_episode(series_id, current_item_id, user_id):
    """Find the episode that comes right after current_item_id in the series."""
    code, data, _ = jf(f"/Shows/{series_id}/Episodes?userId={user_id}")
    if code != 200 or not isinstance(data, dict):
        return None
    items = data.get("Items", [])
    idx = None
    for i, it in enumerate(items):
        if it.get("Id") == current_item_id:
            idx = i
            break
    if idx is None:
        return None
    for it in items[idx + 1:]:
        if it.get("Type") == "Episode" and it.get("IndexNumber", 0) >= 1:
            return it.get("Id")
    return None


def cache_path(ms_id, idx):
    """Jellyfin caches subtitles as /config/data/subtitles/<first2>/<dashed-guid>/<idx>.srt"""
    if "-" not in ms_id and len(ms_id) == 32:
        dashed = "-".join([ms_id[0:8], ms_id[8:12], ms_id[12:16], ms_id[16:20], ms_id[20:]])
    else:
        dashed = ms_id
    return os.path.join(SUBTITLE_CACHE_BASE, dashed[0:2], dashed, f"{idx}.srt")


def find_subtitle_index(source):
    """Find the first embedded text subtitle stream index (prefer English if tagged)."""
    streams = source.get("MediaStreams", [])
    eng_idx = None
    first_text_idx = None
    for s in streams:
        if s.get("Type") == "Subtitle" and s.get("IsTextSubtitleStream"):
            idx = s.get("Index")
            if idx is not None:
                if first_text_idx is None:
                    first_text_idx = idx
                lang = (s.get("Language") or "").lower()
                if lang in ("eng", "en"):
                    eng_idx = idx
                    break
    return eng_idx if eng_idx is not None else first_text_idx


def prewarm_subtitles(next_id, media_source):
    """Trigger background extraction of embedded subtitle stream if not already cached."""
    ms_id = media_source.get("Id")
    if not ms_id:
        return
    sub_idx = find_subtitle_index(media_source)
    if sub_idx is None:
        log(f"  Subtitles: no embedded text subtitle streams found for ms_id={ms_id}")
        return

    cpath = cache_path(ms_id, sub_idx)
    if os.path.exists(cpath) and os.path.getsize(cpath) > 0:
        log(f"  Subtitles: already cached for ms_id={ms_id} idx={sub_idx}")
        return

    log(f"  Subtitles: triggering extraction for next_id={next_id} ms_id={ms_id} idx={sub_idx}...")
    sub_url = f"/Videos/{next_id}/{ms_id}/Subtitles/{sub_idx}/Stream.srt"
    status, _, elapsed = jf(sub_url, json_out=False, timeout=600)
    exists = os.path.exists(cpath)
    size = os.path.getsize(cpath) if exists else 0
    ok = status == 200 and exists and size > 0
    log(f"  Subtitles: status={status} in {elapsed:.1f}s cached={exists} size={size}B ok={ok}")


def prewarm(next_id, user_id):
    """Fire the pre-warm PlaybackInfo and then pre-warm subtitles."""
    code, parsed, dt = jf(f"/Items/{next_id}/PlaybackInfo?UserId={user_id}", method="POST", body={}, timeout=300)
    ms = parsed.get("MediaSources", []) if isinstance(parsed, dict) else []
    log(f"Prewarm next={next_id} status={code} time={dt:.2f}s mediaSources={len(ms)}")
    if code == 200 and ms:
        # Prewarm subtitle for first media source in the background
        prewarm_subtitles(next_id, ms[0])
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
        
        # Dispatch in background worker thread to keep webhook response instantaneous
        threading.Thread(target=self.handle_payload, args=(payload,), daemon=True).start()

    def handle_payload(self, p):
        try:
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
        except Exception as e:
            log(f"Handler error: {e}")

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
