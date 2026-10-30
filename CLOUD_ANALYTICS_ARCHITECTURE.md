# Cloud analytics: client RTSP -> our GPU -> their web/mobile account

Written 28.10.2026. The idea: a client registers in a web or mobile app, adds
their camera(s), the stream reaches our GPU server, the normal SVision
pipeline runs on it, and the client sees their own results after logging in.
Multi-tenant SaaS, not a per-site deployment.

Nothing here is built yet. This is the plan plus a test recipe for stage 0
that can be run this week.

Related: `SECURITY_POLICY.md` (what the policy requires of new code),
`WORKER_GPU_TUNING.md` (how many streams a card actually holds),
`../SESSION_HANDOVER.md` (video lab, whose `job_api` is the seed of the
scheduler).

---

## 1. What exists today and what does not

Everything we run now is **one deployment per site**: Cotton, Avatariya,
Headliner1, Uly dala each have their own master, worker, Mongo, Milvus,
MinIO on the customer's own box, and a static `sources_1.json` of cameras.

| Piece | Status |
|---|---|
| Detection / tracking / embedding (`svision-worker`) | exists, reusable as is |
| Matching + storage (`svision_master`, Mongo/Milvus/MinIO) | exists, **single-tenant** |
| VMS delivery (`svision_dispatcher`) | exists, not needed in this product |
| Web UI with login (`svision-monitoring`, JWT in `src/security.py`) | auth pattern reusable, data model is not |
| Job launcher (`svision-video/job_api`) | closest thing to a scheduler, built for one-off video runs |
| **Tenant concept** | **does not exist anywhere** (`grep -i tenant` over master/dispatcher/monitoring: nothing) |
| **Ingest layer** | does not exist |
| **Scheduler / camera registry** | does not exist |
| **Client-facing API + mobile** | does not exist |

So the streaming is the small part. The build is tenancy, orchestration and
the client portal.

---

## 2. Target shape

```
client web / mobile ──add camera (url + creds)──► Tenant API (auth, RBAC, quotas)
                                                        │
                                              camera registry (Postgres)
                                                        │
                                                  Scheduler
                                                        │
 client camera ───► Ingest (mediamtx) ───────────► GPU worker pool
 (pull / push / edge agent)   all sources become     (N streams per GPU)
                              rtsp://ingest/<tenant>/<cam>      │
                                                         svision_master
                                                  (Mongo / Milvus / MinIO, tenant-scoped)
                                                                │
                                              Results API ──► web page + mobile app
```

---

## 3. Getting the stream in — three modes, all three needed

"The client gives us an RTSP link" works only when that link is reachable
from the internet. For most clients the cameras sit behind NAT on a private
LAN and no such URL exists; when it does exist, it usually means a camera was
port-forwarded, which is a security hole to avoid building on, and the address
dies whenever the ISP rotates the IP.

| Mode | For whom | How |
|---|---|---|
| **Pull** | public VMS / NVR, static IP, cloud camera | client pastes `rtsp://user:pass@host:554/...`; ingest pulls. Validate at submit time with ffprobe and show codec / resolution / fps back in the UI before saving |
| **Push** | client can configure their NVR or VMS to publish | we issue a stream key, they push `srt://ingest.<domain>:8890?streamid=publish:<key>` (or RTMP). No inbound firewall on their side |
| **Edge agent** | the common case: cameras on a private LAN | a small Docker container or mini-PC we hand over: WireGuard back to our ingest plus a local relay. One tunnel covers all their cameras, nothing exposed publicly |

All three terminate at **mediamtx** in our DC, so everything downstream reads
a uniform internal `rtsp://ingest/<tenant>/<camera>`. mediamtx also gives
reconnect, per-path authentication, on-demand start (don't pull a stream
nobody is watching) and a metrics endpoint that feeds the "camera offline"
state in the UI. We already use mediamtx in `../rtsp-machine`.

Prefer **SRT** over RTSP for the push mode across the public internet: it
reconnects by itself and absorbs packet loss into a latency buffer
(200-500 ms), where RTSP just drops frames — and dropped frames degrade
line-crossing and tracking, which is exactly what we debug hardest.

---

## 4. Scheduler

The missing middle. A camera is added, and something has to: find a GPU slot
with free capacity, start a worker job for that camera, watch its health,
restart it, and rebalance when a card or a box dies.

Capacity is the unit the product is priced in. From our own measurements
(`WORKER_GPU_TUNING.md`): on an A4000 / 5060 Ti class card, roughly **6-12
streams at 1080p** with RT-DETR + RetinaFace + ArcFace, fewer at 4K or with
`PGIE_INTERVAL=0`. Faces off (`FACE_DETECTION_ENABLED=false`, MR !169) buys
a lot back, so a body-analytics-only plan is much cheaper to serve than a
face-recognition plan — worth reflecting in the pricing tiers.

`svision-video/job_api` already launches a worker for a given source with a
given config and tracks the run; it is the natural seed, extended from
"one video file" to "a long-lived camera job".

---

## 5. Multi-tenancy

The part where a mistake is unrecoverable, so build it structurally, not as
a `WHERE` clause someone can forget.

- `tenant_id` on every record: Mongo collections, MinIO prefix or bucket per
  tenant, and above all **Milvus partitions** — a search that crosses
  partitions returns another company's faces.
- Camera credentials are secrets: Vault, or at minimum encrypted columns.
  Never plaintext in Postgres, never in logs, never in an exported config.
- RBAC: tenant admin / operator / viewer, plus our own support role with its
  use logged. `svision-monitoring` has the JWT and role scaffolding to copy.
- Quotas per tenant: max cameras, retention days, event rate, storage.
- Every security event logged (login, role change, camera added or removed,
  support access), with no tokens and no full personal data in the log line.

---

## 6. Results to web and mobile

One API serves both:

- REST for lists, filters, history, camera state.
- WebSocket or SSE for live events.
- Presigned MinIO URLs for crops and snapshots, short TTL, tenant-scoped.

Mobile is then a client of the same API, with no backend of its own. The
video lab's UI work (VMS-like theme, event cards) is a usable starting point
for the look.

---

## 7. Hard constraints to settle before writing code

**Personal data law.** We would be processing third parties' biometric data
on our infrastructure, as a processor acting for the client. In Kazakhstan
that implies localization (servers in KZ), a written processing agreement per
client, consent collected by the client but auditable by us, retention limits
and deletion at the end of the contract. `SECURITY_POLICY.md` puts faces,
crops and analytics logs in the personal-data category. **Get legal to confirm
before the first paying client**, not after.

**Bandwidth.** 1080p H.264 is ~4-8 Mbit/s per camera, 4K is ~8-16. Fifty
cameras is 200-300 Mbit/s of sustained inbound, continuous. That is a real DC
line, and it is a cost per camera, not per client.

**Client camera quality.** We get whatever they own: bad angles, 15 fps,
H.265, night noise, 2 Mbit/s over-compressed streams. The pipeline has to
degrade gracefully and the UI has to tell the client *why* a camera produces
no events, or every such camera becomes a support ticket.

**Where the GPU lives.** Cloud GPU rental per stream is expensive at scale;
our own box in a KZ DC is cheaper past a certain number of cameras and also
answers the localization question. Decide early, it changes the cost model.

---

## 8. Stage 0 — the test that can be run now

Goal: prove the path `local rtsp-machine -> public relay -> our GPU box ->
results`, without buying a static IP and without opening anything on our
network.

**No GPU is needed on the relay.** A ~$5/month VPS with a public IP is
enough; the GPU stays on the dev box (`10.100.5.102`) or Avatariya and pulls
from the relay over its normal internet egress. Both ends are outbound-only.

Cloudflare, for the record:

| Option | Verdict |
|---|---|
| Cloudflare proxy (orange cloud) | HTTP/HTTPS only, does not carry RTSP |
| Cloudflare Spectrum (arbitrary TCP) | works, Enterprise pricing |
| Cloudflare Tunnel (`cloudflared`) | carries TCP, but the pulling side must also run `cloudflared access tcp` — fine between our own boxes, useless for client onboarding |
| Cloudflare Stream Live | ingests RTMPS/SRT but returns HLS at 3-30 s latency; we would have to pull HLS and re-feed DeepStream. Wrong tool |
| ISP static IP | works, but exposes RTSP inbound on the office router |

### Steps

1. **Relay VPS**, in KZ if possible (latency and, later, localization). Open
   8554/tcp (RTSP) and 8890/udp (SRT) only. Run mediamtx with authentication
   on — publish user and read user separate, strong passwords, kept out of git.

2. **Push from the local machine** (the `../rtsp-machine` loop stands in for a
   client camera):

   ```bash
   ffmpeg -rtsp_transport tcp -i rtsp://127.0.0.1:7554/cotton \
          -c copy -f mpegts "srt://RELAY:8890?streamid=publish:test1&latency=300000"
   ```

   RTSP push is the fallback if SRT gives trouble:
   `-c copy -f rtsp -rtsp_transport tcp rtsp://user:pass@RELAY:8554/test1`

3. **Pull on the GPU box**: point a worker source at
   `rtsp://user:pass@RELAY:8554/test1` and run the normal pipeline. The video
   lab is the easiest place to see the result without touching a live site.

4. **Measure, and write the numbers down**: glass-to-event latency, frames
   dropped over an hour, behaviour when the local link is cut and restored,
   actual bitrate consumed, FPS held on the GPU. These numbers decide whether
   the product is viable before anything is built.

5. **Then the edge agent**: same test, but WireGuard from the local machine to
   the relay instead of an ffmpeg push, and the relay pulling the camera
   through the tunnel. That is the variant most real clients will need.

---

## 9. TO-DO

Stage 0 — feasibility (days)
- [ ] Rent the relay VPS, install mediamtx with auth, firewall to the two ports
- [ ] Push `rtsp-machine` to it over SRT, pull from the dev box, run the pipeline
- [ ] Record latency / loss / bitrate / FPS; decide go or no-go
- [ ] Repeat with WireGuard instead of a push (edge-agent variant)

Stage 1 — one tenant, end to end (weeks)
- [x] mediamtx as the real ingest, pull + push paths (`ingest/`, HTTP auth hook)
- [x] Camera registry (Postgres) and an API to add / remove / validate a camera
- [x] Scheduler: camera -> GPU slot -> worker job, with health and restart
- [ ] Run against a real DeepStream node (so far: a stand-in with the same REST API)
- [ ] Results visible in a web UI behind login (the API is there; the web app is not)

Stage 2 — multi-tenant
- [x] `tenant_id` end to end: API, events, Milvus rows (dynamic field, not
      partitions); images are tracked per event, so no MinIO prefixes needed
- [x] Credential storage (encrypted columns), never in logs
- [x] RBAC, quotas, security event log
- [x] Isolation tests: a tenant must not be able to reach another's data by any path
- [x] Known persons per tenant (trt_api `feat/multi-tenant`), erasure on deletion

Stage 3 — product
- [x] Edge agent with a one-command install
- [ ] Edge agent image published to a registry
- [x] Mobile API, handoff doc, demo server on the VPN
- [ ] Mobile app (the Flutter developer, in progress)
- [ ] Push sending (needs a Firebase project); production domain with HTTPS
- [ ] Metering for billing (camera-hours, with and without faces)
- [ ] Legal: processing agreement template, retention and deletion policy

Open questions for Talgat
- [ ] Own GPU box in a KZ DC, or rented cloud GPU?
- [ ] Is face recognition in scope for this product, or body analytics only
      at first? It changes GPU cost per camera several times over and changes
      the legal weight of the whole thing
- [ ] Who is the first pilot client, and what cameras do they actually have?
