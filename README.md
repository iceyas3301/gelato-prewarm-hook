# gelato-prewarm-hook

Event-driven pre-warming of Gelato's next-episode stream cache on Jellyfin.

**Problem:** with Gelato-backed streams (AIOStreams/debrid), advancing to the next episode makes Jellyfin wait while Gelato re-queries the addon on demand (~2–15s including probe), then starts a fresh transcode.

**Fix:** Jellyfin's official Webhook plugin fires on **Playback Start**; a small local receiver (this repo) immediately pre-warms the *next* episode's Gelato `streamsync` cache in the background. Advancing to that episode then hits the in-memory cache (~0.02s) — no addon query, no visible stall.

## Architecture

```
Jellyfin (Webhook plugin) --Playback Start POST--> receiver :8800 --POST /Items/{next}/PlaybackInfo--> Jellyfin Gelato (warm)
```

- Receiver: pure-Python `http.server`, no dependencies beyond stdlib.
- Runs as a systemd user service; survives reboots.
- Requires the official [jellyfin/jellyfin-plugin-webhook](https://github.com/jellyfin/jellyfin-plugin-webhook) plugin on Jellyfin.

## Install

### 1. Jellyfin Webhook plugin

Download the latest release zip from the [releases page](https://github.com/jellyfin/jellyfin-plugin-webhook/releases/latest), extract it into a versioned directory inside the Jellyfin container, and restart:

```bash
docker cp Webhook_<version>/ jellyfin:/config/plugins/Webhook_<version>/
docker restart jellyfin
docker logs jellyfin | grep 'Loaded plugin: Webhook'
```

Compatibility: the plugin `meta.json` `targetAbi` must be ≤ your server version.

### 2. Receiver

```bash
mkdir -p ~/scripts ~/.config/systemd/user
cp gelato-prewarm-hook.py ~/scripts/
cp gelato-prewarm-hook.service ~/.config/systemd/user/
# edit the .service: set JF_BASE and JF_KEY (Jellyfin API key)
systemctl --user daemon-reload
systemctl --user enable --now gelato-prewarm-hook.service
curl http://127.0.0.1:8800/   # -> "gelato-prewarm-hook alive"
```

The unit writes logs and dedup state under `%h/gelato-prewarm/{logs,state}` (`%h` = your
home directory; the script's own defaults use `~/gelato-prewarm/...`). Override with the
`LOG_DIR` / `STATE_DIR` environment variables if you keep things elsewhere.

### 3. Webhook configuration (API only, no dashboard)

Jellyfin plugin guid: `71552a5a-5c5c-4350-a2ae-ebe451a30173`.

```
GET /Plugins/{guid}/Configuration
POST /Plugins/{guid}/Configuration   # full-replace
```

Generic destination (JSON body):

```json
{
  "NotificationTypes": ["PlaybackStart"],
  "WebhookUri": "http://<receiver-host>:8800/hook",
  "EnableEpisodes": true,
  "SendAllProperties": true,
  "EnableWebhook": true,
  "UserFilter": [],
  "Template": "",
  "Headers": [],
  "Fields": []
}
```

Key points:
- `SendAllProperties: true` POSTs the full data dict as JSON — no Handlebars template needed (`Template` is base64-decoded Handlebars; `SendAllProperties` bypasses it).
- `UserFilter: []` (all users) is **required** — the payload carries `UserId`, and the warm call must use that user's id (Gelato's cache key is per-user).
- `EnableEpisodes: true` only; other item types off, so it only fires on episodes.

Verified Playback Start payload keys: `NotificationType`, `ItemId`, `SeriesId`, `IndexNumber`, `ItemType`, `UserId`, `NotificationUsername`, `ItemName`, + session/base-item fields.

## How it works

On POST to `/hook`:

1. Skip unless `NotificationType == "PlaybackStart"` and `ItemType` is an Episode.
2. Require `UserId`, `ItemId`, `SeriesId`; dedup `(user_id, item_id)` within 90s.
3. Next episode: `GET /Shows/{seriesId}/Episodes?userId={uid}`, locate the current `ItemId`, take the first following `Type=="Episode"` with `IndexNumber >= 1` (skips index-0 specials; works across season boundaries).
4. Pre-warm: `POST /Items/{next_id}/PlaybackInfo?UserId={uid}` with body `{}` and `Content-Type: application/json` (else 415). This populates Gelato's `streamsync:{userId}:{episodeId}` cache (TTL = `StreamTTL`, default 3h).
5. Logs result + timing to `hook.log`.

## Verification

- **Receiver chain (fully scriptable):** POST a synthetic payload with the same shape → `hook.log` shows `Hook: type=PlaybackStart …` then `Prewarm next=<id> status=200 time=Ns` (cold) → a fresh `POST /Items/{next}/PlaybackInfo` returns in ~0.02s with no second `SyncStreams` in the Jellyfin logs.
- **Real client path cannot be simulated via API.** `POST /Sessions/Playing` returns 204 but does NOT raise the server-side PlaybackStart event when called with just an API key (it needs a real user playback session). The definitive plugin→hook test is pressing play on a device, then checking the hook log for the `Hook:` line.

## Caveats

- Gelato's cache is **in-memory + per-user** → a Jellyfin restart wipes it. The first episode played after a restart has no pre-warmed next (nothing was playing to trigger on yet); every subsequent advance is instant.
- The warm call is still a real addon query — the point is it runs in the background during current playback, so it's invisible.
- The plugin fires per-user in the session; multi-user sessions may warm multiple times — dedup + the per-user cache handle it.

## Rollback

- Remove webhook: `POST /Plugins/{guid}/Configuration` with all `*Options` arrays `[]`.
- Stop receiver: `systemctl --user stop gelato-prewarm-hook.service`.
- Remove plugin: delete `/config/plugins/Webhook_<version>/` in the container, restart Jellyfin.

## License

MIT
