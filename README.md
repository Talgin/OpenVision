# OpenVision — cloud video analytics

A client signs up in the app, connects cameras, the video reaches our GPU
boxes, the existing TRT pipeline analyses it, and the client sees their own
results. Design and reasoning: `CLOUD_ANALYTICS_ARCHITECTURE.md`.

```
client camera ─pull / SRT push / edge agent─► mediamtx (ingest/)  ──rtsp cam/<id>──► trt-streamer-pyservicemaker (GPU)
                     ▲ auth hook                    ▲ paths                                 │ face_events
                     │                              │                                trt-processor
 app ──REST/WS──► cloud_api ◄── scheduler (places cameras on GPU nodes) ◄────────────────┘ filtered_events
                     ▲                                                                       │
                     └──────────────────────── bridge (tenant-scoped events) ◄────────────────┘
```

| dir | what |
|---|---|
| `cloud_api/` | FastAPI service: tenants, users/roles, cameras, ingest auth hook, events, stats, live WebSocket; workers `scheduler`, `bridge`, `demo` |
| `ingest/mediamtx.yml` | ingest config; paths are created at runtime by the scheduler |
| `edge_agent/` | the container that runs on a client's LAN and pushes their cameras out over SRT |
| `deploy/` | `docker-compose.demo.yml` (fake events, for app development), `docker-compose.yml` (real) |
| `docs/MOBILE_API_HANDOFF.md` | API contract for the Flutter app |
| `upstream/` | local clones of the repos this builds on (git-ignored) |

## Run

```bash
docker compose -f deploy/docker-compose.demo.yml up --build    # http://localhost:8000/api/docs
cloud_api/run_tests.sh -q                                        # 46 tests, SQLite + fakeredis, no services needed
```

## How it plugs into the existing pipeline

- **Into the GPU**: the scheduler calls trt-streamer-pyservicemaker's
  `nvmultiurisrcbin` REST API (`/add`, `/remove`, `/get-stream-info`), with
  the same payloads as `common/sources.py`. A GPU node is one streamer
  instance, registered with
  `POST /internal/platform/gpu-nodes {"name", "streamer_url": "http://gpu1:50011/api/v1/stream", "plan": "face"|"body", "capacity"}`
  (header `X-Platform-Token`). The streamer should run without `sources.json`
  cameras, so the scheduler owns its stream list: streams it did not assign are removed.
- **Out of the GPU**: the bridge reads trt-processor's `filtered_events`
  (zlib + msgpack) with its own consumer group, so trt-broker is unaffected.
- **Camera ids** are global integers, because trt-processor does
  `int(camera_id)`, and they also serve as the DeepStream sensor id and the
  ingest path `cam/<id>`.

## Face matching per tenant

trt-processor matches faces in Milvus collections shared by everyone on that
processor. For the cloud it must run with `MULTI_TENANT=true`, from branch
`feat/multi-tenant-matching` of trt-processor (not merged yet). Then:

- the scheduler keeps the Redis hash `openvision:camera_tenant` (camera_id →
  tenant_id) up to date in the pipeline Redis, every tick;
- the processor restricts every search to the camera's tenant and tags every
  insert with it (a Milvus dynamic field, so no collection rebuild is needed);
- a camera missing from the hash gets no matching at all (fail closed), and
  known faces enrolled before the change, which have no tenant, never match.

The same branch fixes unknown people getting a new id on every sighting.
Cloud cameras carry no face categories, so for them the tenant alone is the
scope of a search. Body-only tenants are unaffected: the scheduler never
places them on a face node.

## Known persons and erasure

`/api/v1/persons` enrolls a face through trt_api, branch `feat/multi-tenant`
(not merged yet), which stores the tenant id on the face; `Person.id` is the
`person_id` in Milvus and in events. Set `OV_FACE_API_URL` to trt_api on the
GPU side network; it has no authentication of its own, so never expose it.

What is erased, and when:

| what | when | how |
|---|---|---|
| a person's face rows and photo | person deleted | trt_api `/delete_face/` |
| event crops and frames | event expires (retention), camera deleted, company deleted | scheduler, `purge_events` |
| every face of a company: known, unknown, historical | company deleted | trt_api `/delete_tenant/`; the company row stays until this succeeds |

Not yet erased: the unknown and historical embeddings of a company that still
exists are kept beyond its retention period, and trt-processor's MongoDB
copy of events has no tenant id.

## Database changes

```bash
cd cloud_api && python -m alembic revision --autogenerate -m "what changed"
```

The API container runs `alembic upgrade head` before it starts.
`tests/test_migrations.py` fails when a model changes without a migration.

## Not done yet

Sending push notifications (needs the Firebase project), publishing the
edge-agent image to a registry, the Stage 0 measurements on a real VPS, a run
with a real DeepStream node and trt_api, the customer web app, and metering
for billing. The scheduler has been run against a stand-in
that implements the same REST API.
