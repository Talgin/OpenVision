# OpenVision mobile app — API handoff for the Flutter developer

Version 0.2 · 30.10.2026 · backend: `cloud_api/` in this repo

**Changes since 0.1** (your questions of 30.10):
account deletion (§4); admins can set a team member's password (§12); new
token and rename for edge agents (§7.3); PATCH and `null` (§3);
`openapi.json` now describes the real error body on every route and no longer
lists internal endpoints; the audit cursor is a string (§3).

This is everything you need to start the client app. The backend is built and
tested; you can run it on your laptop with fake data and build against it
today, without cameras or a GPU. Where something is not built yet it says
**Planned** — don't build UI for it yet, or build it behind a flag.

---

## 1. What the app is

OpenVision is a cloud video-analytics service. A company signs up, connects
its CCTV cameras, our GPU servers analyse the video (people, faces), and the
company sees the results in the app: which cameras are working, what was
detected, when, on which camera, with a snapshot.

The app is one client of a REST + WebSocket API. There is no mobile-specific
backend; a web dashboard will use the same API.

**MVP scope for the app:**

1. Sign up / sign in / sign out, staying signed in across restarts
2. Dashboard: today's counts, cameras online, a per-hour chart
3. Camera list with live status, and *why* a camera is not working
4. Add a camera (three ways, §7), rename, switch on/off, delete
5. Event feed: newest first, infinite scroll, filter by camera / known / time
6. Event detail: face crop, full frame with the box drawn, time, camera
7. Live: new events and camera status changes arrive without refreshing
8. Team (admins only): list users, invite with a role, change role, remove
9. Register the device for push notifications (the sending side is Planned)

---

## 2. The backend you'll use

**Shared dev server (no setup):** connect to the office WireGuard VPN, then use

- API base: `http://10.8.0.53:8000/api/v1`
- Interactive docs: http://10.8.0.53:8000/api/docs
- WebSocket: `ws://10.8.0.53:8000/api/v1/live`

It is the demo setup described below (same accounts, fake events every few
seconds) and it is reachable only through the VPN. Everyone on the VPN shares
the same demo account, so expect cameras someone else added. Ask
Talgat if you want the data reset or an account of your own.

Plain HTTP is fine for this dev server. Android blocks cleartext HTTP by
default, so allow it for `10.8.0.53` only, in a debug-only
`network_security_config.xml`. On iOS, add an ATS exception in the debug
`Info.plist`. Production will be HTTPS only.

### Or run it on your own machine

You need Docker. From the repo root:

```bash
docker compose -f deploy/docker-compose.demo.yml up --build
```

That starts Postgres, Redis, the API on **http://localhost:8000**, and a
simulator that creates a fake event every few seconds, in the same format the
real GPU pipeline produces. On first start it creates a demo account:

| email | password | role |
|---|---|---|
| `demo@openvision.dev` | `demo-password-1` | owner (can do everything) |
| `viewer@openvision.dev` | `demo-password-1` | viewer (read-only) |

The demo account has three cameras: two online, one offline (so you can see
both states), and new events keep arriving on the online ones.

The demo has no video ingest, so a camera **you** add stays `pending`
forever. That is expected. Everything else about it (the response, the
push URLs, edit, delete, the live `camera_status` message) behaves as in
production. Adding a *pull* camera runs a real connection test, so in the
demo it fails with `source_unreachable` unless you give it a real public RTSP URL.

- Interactive API docs (try every call in the browser): http://localhost:8000/api/docs
- OpenAPI JSON (for code generation if you want it): http://localhost:8000/api/openapi.json

**From a phone or emulator**, `localhost` is the device itself:

- Android emulator: use `http://10.0.2.2:8000`
- Real device on the same Wi-Fi: use your laptop's IP, e.g. `http://192.168.1.20:8000`

Image URLs in responses are built from `OV_PUBLIC_BASE_URL`. Set it to the
address your device uses, or images will point at `localhost`:

```bash
OV_PUBLIC_BASE_URL=http://10.0.2.2:8000 docker compose -f deploy/docker-compose.demo.yml up
```

To wipe everything and start over:
`docker compose -f deploy/docker-compose.demo.yml down -v`.

---

## 3. Conventions

| | |
|---|---|
| Base path | `/api/v1` |
| Format | JSON, UTF-8, `Content-Type: application/json` |
| Auth | `Authorization: Bearer <access_token>` on every call except register / login / refresh / logout |
| Times | ISO 8601 in UTC, e.g. `2026-10-29T08:15:30Z`. Convert to local time for display; send UTC in query params |
| IDs | users, tenants, events, edge agents: UUID strings. **Cameras: integers** |
| Unknown fields | Ignore them. We add fields without bumping the version; we don't remove or rename them without one |
| Nulls | Any field typed `X \| null` below can be `null`; handle it |
| PATCH | Omit a field to leave it unchanged. Send `null` to clear a field that can be empty (e.g. camera `location`). `null` for a field that can't be empty (`name`, `enabled`, ...) is a 422 `validation_error` on that field |

### Errors

Every error has the same shape, whatever the status code:

```json
{
  "error": {
    "code": "camera_limit_reached",
    "message": "your plan allows 4 cameras",
    "details": { "limit": 4 }
  }
}
```

**Branch on `code`, never on `message`.** `message` is English, for logs and
for developers. Write your own user-facing text per `code` (the app will need
Russian and Kazakh). `details` is `null` or an object whose shape depends on
the code. The full list of codes is in §11.

Validation errors (422, `validation_error`) list the fields:

```json
{"error": {"code": "validation_error", "message": "request is invalid",
  "details": [{"field": "password", "message": "String should have at least 10 characters"}]}}
```

`field` matches the JSON key, so you can show the message under the matching form field.

`openapi.json` declares this body (`ErrorOut`) for 400, 401, 403, 404, 409,
422 and 429 on every route, so a generated client parses errors correctly.

### Pagination

Lists that grow without limit (events, audit) use cursors. A cursor is always a string:

```
GET /api/v1/events?limit=50
→ {"items": [...], "next_cursor": "MjAyNi0xMC0yOVQw..."}

GET /api/v1/events?limit=50&cursor=MjAyNi0xMC0yOVQw...
→ {"items": [...], "next_cursor": null}        ← null = last page
```

The cursor is opaque: don't parse it or build one yourself. Keep the other
filters unchanged while you page. Small lists (cameras, users, edge agents)
are plain arrays with no paging.

---

## 4. Authentication

### Tokens

- **access_token**: a JWT valid for 15 minutes (`expires_in: 900`). Send it as a Bearer token.
- **refresh_token**: an opaque string valid for 30 days. Use it only to get a new pair.

Store both in **`flutter_secure_storage`** (Keychain / Keystore), never in
SharedPreferences. Don't decode the JWT to make decisions; use `GET /auth/me`.

### Calls

**Sign up** creates the company and its owner, and signs them in:

```
POST /api/v1/auth/register
{"company_name": "Acme LLP", "full_name": "Aida S.", "email": "aida@acme.kz",
 "password": "at-least-10-chars", "device_name": "Pixel 8"}
→ 201 {"access_token": "...", "refresh_token": "ovr_...", "token_type": "bearer", "expires_in": 900}
```

Errors: `email_taken` (409), `validation_error` (422). The password needs 10 to 128 characters.
`device_name` is optional; it will be shown in a future "active sessions" list.

**Sign in**:

```
POST /api/v1/auth/login
{"email": "aida@acme.kz", "password": "...", "device_name": "Pixel 8"}
→ 200 same body as register
```

Errors: `invalid_credentials` (401) for both a wrong password and an unknown
email (deliberately the same), `account_suspended` (403), and `rate_limited`
(429, after 5 tries a minute, with `details.retry_after` in seconds).

**Refresh**:

```
POST /api/v1/auth/refresh
{"refresh_token": "ovr_..."}
→ 200 new pair; the old refresh token is now dead
```

Error: `invalid_refresh_token` (401). Clear the stored tokens and go to sign in.

**Sign out**:

```
POST /api/v1/auth/logout
{"refresh_token": "ovr_..."}
→ 204
```

Clear local tokens whatever the response.

**Who am I** — call it after sign in and on app start:

```
GET /api/v1/auth/me
→ 200
{
  "user":   {"id": "uuid", "email": "aida@acme.kz", "full_name": "Aida S.", "role": "owner",
             "is_active": true, "created_at": "...", "last_login_at": "..."},
  "tenant": {"id": "uuid", "name": "Acme LLP", "plan": "face", "max_cameras": 4, "retention_days": 30},
  "permissions": ["account.write", "audit.read", "cameras.control", "cameras.read",
                  "cameras.write", "events.read", "users.read", "users.write"]
}
```

**Change password** (signs out every other session):

```
PUT /api/v1/auth/me/password
{"current_password": "...", "new_password": "..."}
→ 204          errors: wrong_password (400), validation_error (422)
```

After this the stored refresh token no longer works. Sign the user in again with the new password.

**Delete account** (required by the App Store and Google Play for apps with sign-up):

```
POST /api/v1/auth/delete-account
{"password": "..."}
→ 204          errors: wrong_password (400), rate_limited (429)
```

What it deletes depends on who calls it, so say so on the confirmation screen:

- **Owner**: the whole company. Every user is signed out and removed at once,
  and their emails can be used for a new sign-up immediately. Cameras stop.
  Cameras, events and images are erased in the background, usually within a
  minute. Not reversible.
- **Anyone else**: only their own user. The company and its data stay.

After 204, clear local tokens and go to the start screen.

### ⚠️ Refresh rotation: the one thing to get right

Refresh tokens are **single-use**. If the same refresh token is presented
twice, the server assumes it was stolen and **revokes the whole session**:
both requests get 401, and the user is signed out.

The common way to trigger this by accident: the access token expires, three
requests fail with 401 at the same moment, and each one calls `/auth/refresh`
on its own. So:

1. **Only one refresh at a time.** In your Dio interceptor (or equivalent),
   keep a single `Future<TokenPair>?` for the refresh in flight. Every 401
   awaits that future and then retries its request once with the new token.
2. **Save the new pair before retrying anything.** If the app dies between
   receiving the new pair and saving it, the user has to sign in again.
3. Refresh **proactively** when `expires_in` is nearly used up (say 60 s
   left), and on app resume, so most requests never see a 401.
4. A 401 from `/auth/refresh` itself means sign out. Don't retry it.

### Roles and permissions

Drive the UI from `permissions` in `/me`, not from `role`. The table below is
what the server enforces:

| permission | viewer | operator | admin | owner | what it unlocks |
|---|:-:|:-:|:-:|:-:|---|
| `cameras.read` | ✓ | ✓ | ✓ | ✓ | camera list and detail |
| `events.read` | ✓ | ✓ | ✓ | ✓ | events, stats, live |
| `cameras.control` | | ✓ | ✓ | ✓ | switch a camera on/off |
| `cameras.write` | | | ✓ | ✓ | add / edit / delete cameras, edge agents |
| `users.read` / `users.write` | | | ✓ | ✓ | team screen |
| `audit.read` | | | ✓ | ✓ | security log |
| `account.write` | | | | ✓ | company settings (Planned) |

A call without the permission returns 403 `forbidden`. If a role changes
while the user is signed in, it takes effect on their next request, so
re-fetch `/me` when you get an unexpected 403.

---

## 5. Cameras

### The camera object

```json
{
  "id": 17,
  "name": "Entrance",
  "location": "Main door",
  "mode": "pull",
  "source_url": "rtsp://***@cam.example.com:554/Streaming/Channels/101",
  "edge_agent_id": null,
  "enabled": true,
  "status": "online",
  "status_reason": null,
  "stream_info": {"codec": "h264", "width": 1920, "height": 1080, "fps": 25.0},
  "last_seen_at": "2026-10-29T08:15:00Z",
  "last_event_at": "2026-10-29T08:14:41Z",
  "created_at": "2026-10-20T10:00:00Z"
}
```

`source_url` always has the password removed. The real URL is never sent to
the app, after it has been saved.

### Status and reason: what to show the user

`status` is the badge colour; `status_reason` explains it. **Showing the
reason is what keeps a broken camera from becoming a support call**, so give
every reason its own sentence. Suggested wording:

| status | meaning | badge |
|---|---|---|
| `pending` | just saved, setting up (a few seconds) | grey, spinner |
| `connecting` | set up, no video yet | amber, spinner |
| `online` | video arriving and being analysed | green |
| `offline` | was working, video stopped | red |
| `error` | can't work until the client fixes something | red |
| `disabled` | switched off by the client | grey |

| status_reason | suggested text |
|---|---|
| `null` | (nothing to explain) |
| `waiting_for_video` | "Waiting for video. Check that your recorder is sending the stream to the address we gave you." |
| `edge_agent_offline` | "The OpenVision box at your site is offline. Check that it is powered on and connected to the internet." |
| `no_video` | "Video stopped arriving. Check the camera's power and network." (also show `last_seen_at`: "last seen 3 h ago") |
| `waiting_for_gpu` | "Video is fine, waiting for analysis capacity. No action needed." (our problem, not theirs) |
| `source_unreachable` | "We can't reach this camera. Check the address and that it is reachable from the internet." |
| `source_auth_failed` | "The camera refused the username or password." |
| `source_not_found` | "The camera has no stream at this path. Check the URL." |
| `unsupported_codec` | "This camera sends video in a format we can't analyse (we need H.264 or H.265). Change it in the camera's settings." |
| `no_video_track` | "The stream has no video." |
| `private_address_refused` | "This is a local network address, so we can't reach it from the cloud. Use an OpenVision box (edge agent) instead." |
| `ingest_error` | "Something went wrong on our side. We've been notified." |

New reasons may appear later. For an unknown reason, show the status badge
with a generic "Not working, contact support" and log the code.

### Calls

```
GET    /api/v1/cameras                     → [camera, ...]            cameras.read
GET    /api/v1/cameras/{id}                → camera                   cameras.read
POST   /api/v1/cameras/probe               → probe result             cameras.write
POST   /api/v1/cameras                     → 201 camera (+ push)      cameras.write
PATCH  /api/v1/cameras/{id}                → camera                   see below
POST   /api/v1/cameras/{id}/rotate-key     → push credentials         cameras.write
DELETE /api/v1/cameras/{id}                → 204                      cameras.write
```

**PATCH** takes any subset of `{"name", "location", "enabled", "source_url"}`.
`"location": null` clears the location; `null` for the other three is a 422.
A body with only `enabled` needs `cameras.control` (operators can do it);
anything else needs `cameras.write`. Changing `source_url` re-runs the check
in §7.1 and can fail the same way.

**DELETE** removes the camera from the API immediately. Ingest, analysis and
its events are removed in the background.

Errors: `not_found` (404; also for another company's camera id),
`camera_limit_reached` (409, `details.limit`), `probe_failed` (422, §7.1).

---

## 6. Events

### The event object

```json
{
  "id": "6f1c0c2e-6a55-4b8e-9d1a-3a8f0b9e7c11",
  "camera_id": 17,
  "camera_name": "Entrance",
  "kind": "face",
  "occurred_at": "2026-10-29T08:15:30Z",
  "person_id": "0d6c2f1e-1111-2222-3333-444455556666",
  "is_known": true,
  "confidence": 0.87,
  "bbox": [640, 220, 96, 120],
  "crop_url": "https://api.../api/v1/media/eyJ...",
  "frame_url": "https://api.../api/v1/media/eyJ...",
  "media_expires_at": "2026-10-29T08:25:30Z",
  "attributes": null
}
```

- `kind`: currently always `"face"`. Body / line-crossing events will add
  more kinds; show unknown kinds with a generic icon and don't crash.
- `person_id`: a stable id for the same person across events. Unknown people
  get an id too, so "seen 4 times today" works for them as well. Known
  people's names are Planned (§10).
- `is_known`: matched someone in the company's people list.
- `confidence`: 0–1, `null` for unknown people. Show it as a percentage at most; don't make users read it.
- `bbox`: `[x, y, width, height]` in **pixels of the full frame**. To draw it
  on the image, scale it by displayed size ÷ the image's natural size.
- `attributes`: reserved for clothing / gender / age, `null` for now.

### Images: URLs expire

`crop_url` and `frame_url` are **signed URLs that stop working at
`media_expires_at`** (10 minutes). They need no auth header, so
`Image.network` / `CachedNetworkImage` works directly. But:

- **Cache by event id, not by URL.** With `cached_network_image`, set
  `cacheKey: '${event.id}_crop'`. The URL changes on every fetch; the image does not.
- If an image fails to load with 403 (`media_link_expired`), re-fetch that
  event with `GET /events/{id}` to get fresh URLs. Don't retry the old URL.
- Either URL can be `null` (no image kept for that event); show a placeholder.

### Calls

```
GET /api/v1/events                       events.read
    ?camera_id=17&camera_id=18           repeat the key for several cameras
    &known=true|false
    &person_id=...                       everything about one person
    &kind=face
    &since=2026-10-29T00:00:00Z          inclusive
    &until=2026-10-30T00:00:00Z          exclusive
    &limit=50                            1..200, default 50
    &cursor=...
  → {"items": [event, ...], "next_cursor": "..." | null}       newest first

GET /api/v1/events/{id}                  → event
```

### Dashboard numbers

```
GET /api/v1/stats/summary?since=...&until=...       events.read
    defaults: the last 24 hours; at most 31 days between since and until
→ {
  "since": "...", "until": "...",
  "total_events": 312, "known_events": 40, "unique_people": 118,
  "cameras_online": 2, "cameras_total": 3,
  "by_camera": [{"camera_id": 17, "camera_name": "Entrance", "events": 200, "known": 30}],
  "by_hour":   [{"start": "2026-10-29T08:00:00Z", "events": 41}]
}
```

`by_hour` leaves out hours with no events; fill the gaps with zero when you
draw the chart. Hours are UTC hours: for Kazakhstan (UTC+5) they still line
up with local hours, because the offset is a whole number of hours. Error:
`invalid_range` (422).

---

## 7. Adding a camera: three ways

The first screen of "Add camera" should ask *how* the camera is reachable,
because the answer changes the whole flow:

| mode | the user's situation | what they give us |
|---|---|---|
| `pull` | "My camera/recorder has an internet address" (public IP, cloud NVR) | an `rtsp://` URL |
| `push` | "My recorder/VMS can send a stream" (Hikvision / Dahua / Milestone / ffmpeg) | nothing; **we** give them an address |
| `edge` | "My cameras are only on our local network" (most customers) | the camera's local URL + which OpenVision box reads it |

Guide the user to the right one. A short questionnaire is usually better than
naming the three modes.

### 7.1 Pull

1. The user enters the URL, e.g. `rtsp://admin:pass@203.0.113.5:554/Streaming/Channels/101`.
2. Optional but recommended: test it first, without saving:
   ```
   POST /api/v1/cameras/probe   {"url": "rtsp://..."}
   → 200 {"ok": true, "reason": null,
          "stream": {"codec": "h264", "width": 1920, "height": 1080, "fps": 25.0},
          "warnings": ["low_fps"]}
   ```
   This always returns 200; read `ok`. It can take **up to ~12 s**, so show a
   spinner with a "Connecting to your camera…" label. If `ok`, show "1920×1080,
   25 fps, H.264 ✓". Warnings (`low_fps` under 10 fps, `low_resolution` under
   480p) mean the camera works but results will be worse. Show them, but
   allow saving. If not `ok`, show the text for `reason` from §5.
3. Save:
   ```
   POST /api/v1/cameras   {"name": "Entrance", "location": "Main door", "mode": "pull", "source_url": "rtsp://..."}
   ```
   The server probes again and refuses to save a URL that doesn't work:
   ```
   422 {"error": {"code": "probe_failed", "message": "...",
                  "details": {"reason": "source_auth_failed", "stream": null}}}
   ```

A local address (`192.168.*`, `10.*`, `127.*` …) is always refused with
`private_address_refused`. Offer the edge flow instead.

### 7.2 Push

1. `POST /api/v1/cameras {"name": "Checkout", "mode": "push"}`
2. The response has a `push` object, **only this once**:
   ```json
   "push": {
     "srt_url":  "srt://ingest.openvision.kz:8890?streamid=publish:cam/18:push:ovk_...&pkt_size=1316",
     "rtsp_url": "rtsp://push:ovk_...@ingest.openvision.kz:8554/cam/18",
     "stream_key": "ovk_..."
   }
   ```
3. Show both URLs with copy buttons and a "Share" action. They're long, so
   don't make anyone retype them. Say that SRT is preferred and RTSP is the
   fallback. Warn that it won't be shown again.
4. The camera stays `connecting` / `waiting_for_video` until video arrives,
   then turns `online`. Watch it over the WebSocket (§8) on the same screen,
   so the user sees it go green while still at their recorder.
5. If they lose the key: `POST /cameras/{id}/rotate-key` returns a new `push`
   object, and the old key stops working.

Treat these URLs like passwords: no analytics events, no crash-report breadcrumbs.

### 7.3 Edge (OpenVision box on the customer's LAN)

An *edge agent* is a small program (a Docker container or a mini-PC we ship)
on the customer's network. It reads their cameras locally and sends the video
to us; nothing on their network is opened to the internet. One agent serves
all the cameras at a site.

1. List agents: `GET /api/v1/edge-agents` → `[{"id", "name", "online", "last_seen_at", "created_at"}]`
2. If there is none, create one: `POST /api/v1/edge-agents {"name": "Warehouse PC"}` →
   `201 {..., "token": "ova_...", "install_command": "docker run -d ..."}`.
   The token and command are shown **once**. Show the command with a copy
   button. Typically an IT person runs it, so offer "Share / send by email".
   `online` turns true within ~30 s of the agent starting.
3. Add the camera with its **local** URL:
   ```
   POST /api/v1/cameras
   {"name": "Loading bay", "mode": "edge", "edge_agent_id": "<agent uuid>",
    "source_url": "rtsp://admin:pass@192.168.1.64:554/stream1"}
   ```
   There's no probe for edge cameras: we can't reach their LAN. The camera
   shows `edge_agent_offline` until the agent connects, then
   `waiting_for_video`, then `online`.
4. **Lost the token or command?** `POST /api/v1/edge-agents/{id}/rotate-token`
   → `{"token", "install_command"}`, shown once. The old token stops working
   at once, so the agent has to be reinstalled with the new command. Its
   cameras stay as they are and come back when it reconnects. Say this on the
   confirmation screen.
5. Rename: `PATCH /api/v1/edge-agents/{id} {"name": "..."}` → the agent.
6. `DELETE /api/v1/edge-agents/{id}` removes an agent and switches off its cameras.

---

## 8. Live updates (WebSocket)

```
wss://<host>/api/v1/live            (ws:// for the local demo)
```

Use `web_socket_channel`. The token goes **in the first message, not in the
URL** (URLs end up in proxy logs).

```
→ {"type": "auth", "token": "<access token>"}          within 10 s of connecting
← {"type": "ready", "data": {"expires_at": 1793270000}}  unix seconds

← {"type": "event",          "data": <event object, §6>}
← {"type": "camera_status",  "data": <camera object, §5>}   on any status change, and on create / edit
← {"type": "camera_deleted", "data": {"id": 17}}
← {"type": "ping"}                                      every 25 s; nothing to answer

→ {"type": "subscribe", "camera_ids": [17, 18]}        only these cameras' events; null = all (default)
→ {"type": "auth", "token": "<new access token>"}      after each token refresh; you get a new "ready"
```

The server closes the socket with:

- **4401**: no auth, a bad token, or the token expired without a new `auth`
  frame. Refresh tokens (§4) and reconnect.
- **4403**: the role can't read events.

**Reconnecting.** Mobile networks drop sockets all the time. On any close
other than 4403, reconnect with backoff (1 s, 2 s, 4 s … capped at 30 s; reset
after a stable minute). Also reconnect on app resume. Treat 60 s without any
frame (pings come every 25 s) as a dead connection.

**Live messages are best effort.** Messages sent while you were disconnected
are not replayed. After every (re)connect, re-fetch `GET /cameras` and the
first page of `GET /events` and merge by id. The socket is for freshness; REST
is the source of truth. Event `id`s are unique, so de-duplicate on them.

Keep the socket open only while the app is in the foreground. Background
alerts are push notifications' job (§9).

---

## 9. Push notifications

Register the device after sign in, and again whenever FCM gives you a new token:

```
POST   /api/v1/devices   {"platform": "android" | "ios" | "web", "push_token": "<FCM token>"}
→ 201 {"id": "uuid", "platform": "android", "created_at": "..."}

DELETE /api/v1/devices/{id}  → 204        on sign out, before clearing tokens
```

Registering is idempotent: the same token registered again replaces the old
row, even if it belonged to another user on a shared phone.

**Planned:** the server doesn't send pushes yet; it needs our Firebase
project, which you'll get separately. Use Firebase Cloud Messaging on both
platforms (APNs through FCM). The planned payload is a data message
`{"type": "event", "event_id": "...", "camera_id": "17"}` with a notification
title and body. On tap, open `GET /events/{event_id}`.

---

## 10. Not built yet

So you don't build against things that don't exist:

| feature | state |
|---|---|
| People list (upload a photo, name a person, `is_known` → name) | **Planned.** Events already carry `person_id`; names will come from a `/persons` API |
| Live video / camera snapshot in the app | **Planned.** For now show the last event's frame as the camera's thumbnail |
| Push sending | **Planned** (§9); device registration works now |
| Forgot password by email / email verification | **Planned** (needs an email service). Until then an admin sets a new password (§12); on the sign-in screen, "Forgot password?" can say "ask your administrator" |
| Company settings, plan / billing | **Planned** (`account.write`) |
| Active sessions list ("sign out other devices") | **Planned.** Changing the password already signs out everything else |
| Event acknowledgement / notes | Not planned yet; tell us if you need it |

---

## 11. Error codes

| code | HTTP | where | what to do |
|---|---|---|---|
| `validation_error` | 422 | anywhere | show `details[].message` under each `field` |
| `unauthorized` | 401 | anywhere | refresh (§4), retry once; if it fails again, sign out |
| `invalid_credentials` | 401 | login | "Wrong email or password" |
| `invalid_refresh_token` | 401 | refresh | sign out, go to the sign-in screen |
| `rate_limited` | 429 | login | "Too many attempts, try again in N s" (`details.retry_after`) |
| `account_suspended` | 403 | login, anywhere | "This account is suspended, contact support"; sign out |
| `forbidden` | 403 | anywhere | the role can't do this: hide the action; re-fetch `/me` |
| `owner_protected` | 403 | team | the owner can't be changed or removed |
| `self_protected` | 403 | team | you can't change your own role or remove yourself |
| `wrong_password` | 400 | change password, delete account | "Password is wrong" |
| `email_taken` | 409 | register, team | "An account with this email already exists" |
| `not_found` | 404 | anywhere | gone, or never existed: pop the screen and refresh the list |
| `camera_limit_reached` | 409 | add camera | "Your plan allows N cameras" (`details.limit`) |
| `probe_failed` | 422 | add / edit pull camera | show the text for `details.reason` (§5) |
| `not_push_camera` | 422 | rotate key | only push cameras have a key |
| `invalid_cursor` | 400 | events, audit | drop the cursor, reload from the first page |
| `invalid_range` | 422 | stats | since must be before until, at most 31 days apart |
| `media_link_expired` | 403 | image URLs | re-fetch the event for fresh URLs |

An unknown `code`: show a generic "Something went wrong" and log the code with the HTTP status.

---

## 12. Team screen (admins)

```
GET    /api/v1/users                → [user, ...]                                  users.read
POST   /api/v1/users                {"email", "full_name", "role", "password"}     users.write
PATCH  /api/v1/users/{id}           {"full_name"?, "role"?, "is_active"?}          users.write
POST   /api/v1/users/{id}/password  {"new_password"}  → 204                       users.write
DELETE /api/v1/users/{id}           → 204                                          users.write
```

**Set password** is for a team member who forgot theirs: the admin types a
new one and passes it on. The member is signed out everywhere. It doesn't
work on the owner or on yourself (`owner_protected` / `self_protected`);
for your own password use `PUT /auth/me/password`.

`role` is `admin`, `operator` or `viewer` (`owner` can't be assigned). For now
the admin sets an initial password and passes it on; email invites are
Planned. Deactivating (`is_active: false`) signs the user out at once.

---

## 13. Things to keep out of the app

- No camera URLs, stream keys or agent tokens in logs, analytics or crash
  reports. They are credentials to someone's CCTV.
- Faces are personal data (biometrics). Don't save crops or frames to the
  gallery or to shared storage; keep the image cache in the app's private
  cache directory. Don't send them to any third-party service.
- Turn on `FLAG_SECURE` on Android, and blur the app-switcher snapshot on
  iOS, for the event screens. This is a reasonable default; confirm with us
  if it gets in the way.

---

## 14. Questions

Backend: Talgat Islamgozhayev. When the API doesn't do what a screen needs,
ask before working around it. Most changes on our side are small, and a
workaround in the app tends to stay forever.
