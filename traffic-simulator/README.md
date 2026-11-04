# Traffic simulator

SUMO-based evacuation and crew-access simulation for FireGUard: bounded U.S. road networks
from OpenStreetMap, including Alaska and Hawaii, civilian demand, fire-crew dispatch, an assumed
expanding hazard, three policies (baseline, coordinated, phased), optional
TomTom speed limits, optional public observation feeds, and the durable job store shared
with the workspace API. Design and interpretation:
[simulator architecture](../architecture/32-traffic-simulator-architecture.md),
[background road preparation](../architecture/32-traffic-simulator-architecture.md#background-road-preparation),
[business justification](../architecture/33-evacuation-crew-access-business-justification.md).
Contributor guidance: [AGENTS.md](AGENTS.md).

| Path | Contents |
|---|---|
| `traffic_simulator/` | Engine (`simulation`, `network`, `network_cache`, `network_preparation`, `input_bundles`, and `result_reuse` for [identical runs](../architecture/32-traffic-simulator-architecture.md#reusing-identical-runs)), feeds (`feeds`, `traffic`, `corridor`, `provider_calls`), worker loop (`worker`) and its per-phase job timings (`timings`), saved example areas (`examples`), models and settings |
| `traffic_simulator/osm_corridors.py` | OSM road candidates, stable corridor identities, source-way matching, bounded study extents and local projection |
| `traffic_simulator/osm_extracts.py`, `osm_source_cache.py` | Canonical Geofabrik regional selection, immutable source manifests, complete-way/restriction extraction and bounded source caches |
| `traffic_simulator/demand.py` | Seeded resident/crew plans shared by automatic corridor eligibility and simulation |
| `traffic_simulator/store.py`, `persistence.py`, `errors.py` | Job store and leases, and the transaction, idempotency and safe-error layer that the whole workspace API also uses (its agent, radio, firetruck and incident brokers and demo sessions) |
| `traffic_simulator/http_logging.py` | Keeps outbound HTTP client logs from recording provider URLs, which can carry keys |
| `traffic_simulator/data/` | Preserved Caltrans highway geometry for archived Paradise and Malibu examples; fresh road preparation uses OSM |
| `traffic_simulator/testing.py` | Test-only pytest plugin, registered by the tests' `conftest.py` and never imported by runtime code: the `grid` fixture (a real 5×5 network built with the installed `netconvert`) and `create_job`, which the simulator and backend API tests use to queue local jobs |
| `tests/` | Unit and real-SUMO (`-m integration`) tests |
| `scripts/` | SUMO and Osmium native runtime gates, `configure_tomtom.py` (local key) |
| `Dockerfile` | Worker image: checksum-pinned SUMO 1.27.1 and Osmium 1.19.1, `python -m traffic_simulator.worker` |

The HTTP API lives in `backend/` and imports this package; it
never runs SUMO itself in AWS. Locally, the workspace API runs this worker
in-process (`backend/scripts/local_server.py`) and stores jobs,
networks and results in the git-ignored `.runtime/` folder.

```bash
.venv/bin/python -m pytest traffic-simulator/tests             # from the repository root
.venv/bin/python -m pytest traffic-simulator/tests -m integration
docker build -f traffic-simulator/Dockerfile --target verification .   # optional; CodeBuild runs this
```

Configuration: `STORAGE_MODE` is required, `local` or `aws`, with no default;
any other value, including an unset one, stops the worker and the API at
start-up. Tests set `local` in their `conftest.py`. `STORAGE_MODE=aws` also
requires `AWS_REGION`, `ARTIFACT_BUCKET`, `JOBS_TABLE` and `QUEUE_URL` (there is
no default Region). `DATA_DIR` holds scratch and local job data (default
`.runtime/`, created on first use), including the local road-network cache in
`DATA_DIR/networks/`. `TOMTOM_API_KEY` and `FIRMS_MAP_KEY` are optional provider
keys, injected from Secrets Manager in AWS. Fresh road discovery and preparation use
public dated Geofabrik regional PBF extracts and require neither key. Caltrans feeds are
optional California context; missing district selection or coverage does not block
the road network or imply an open or closed road.

Operational limits come from `deployment/settings.toml` ([decision D23](../architecture/decisions.md)), read
through `settings.limit()`; the [settings table](../architecture/29-workspace-operations.md#operational-settings)
lists each one with its default and range. AWS mode requires each variable, which
the stack sets from the file, and stops at start-up naming one that is missing or
outside its range; local runs and tests use the file's values, and tests override
them through the variables. Both images copy `deployment/settings.toml` for these
checks. Only the worker reads `POLICY_PARALLELISM`, `TOMTOM_REQUEST_CONCURRENCY` and
the [road-network cache](../architecture/32-traffic-simulator-architecture.md#road-network-cache)
settings, which `worker.loop()` checks when it starts. Locally the in-process worker
also prepares the Paradise example when it starts (an OpenStreetMap download the
first time, a cache hit afterwards); start it with `DEMO_AREAS_TO_PREBUILD=` to skip
that. The tests' `conftest.py` files set it empty. Guided preparation resolves a road
corridor and assumed exit direction from the same OSM snapshot used by `netconvert`,
then seals the concrete scenario for review. A reviewed run restores that exact
network before fetching context. Completed recordings and frozen parent inputs keep
their original road geometry and source snapshots.

Local road preparation also needs the reviewed Osmium CLI (1.19.1, libosmium 2.23.1) on `PATH`.
On the current macOS toolchain `brew install osmium-tool` supplies it; the source-built Docker image pins and
checks these versions independently. A missing or mismatched CLI is an explicit configuration failure.

New discovery and road preparation are background jobs. The publisher's region index selects coverage for the
actual requested bounds, including state seams and split Aleutian bounds. A new job resolves current convenience
URLs to dated files, verifies publisher MD5, records SHA-256 and the PBF snapshot timestamp, and accepts intersecting
regions only from one timestamp. No other provider, older date, or simulated-road substitute is selected on failure.
Osmium performs seekable multi-pass complete-way extraction with restriction members; reference checks and duplicate
identity checks precede SUMO conversion. Candidate corridors retain source way IDs; the existing passenger, modeled
exit-gate and civilian/crew rules still apply. No passenger roads is an explicit unavailable outcome.
Automatic gate selection runs the existing seeded resident/crew plan and skips candidates with no usable civilian
route or, when crews are requested, no usable crew approach. Individual unresolved plans retain their accounting.

Raw regional files use `DATA_DIR/osm-extracts/` and worker-only `osm-extracts/` objects in the existing artifact bucket.
Small manifests are published last under a renewable source lease; partially downloaded or inconsistent bytes never
become a source cache entry. Exact source hashes, geographic inputs, tool recipe and engine versions select derived
network keys. Warm derived-network hits need source metadata resolution, not a new regional download. Frozen saved
networks replay without resolving today's source. Regional cache capacity and free disk are checked before acquisition;
extracted XML has a separate size bound. Source cache files are distinct from the derived-network cache.

`OSM_SOURCE_HTTP_TIMEOUT_SECONDS=0` and `ROAD_PREPARATION_TIMEOUT_SECONDS=0` disable application transport/native wall
deadlines. Renewable job leases keep long work pending. Publisher index acquisition, regional resolution and exact
file download run in owned spawned transport processes; the parent checks cancellation/lease authority, terminates
and reaps its child even when an HTTP socket is idle. Byte progress uses bounded messages; regional files stay on
owned partial scratch paths and the parent retains verification/publication authority. The shared controller also
serves fire-risk acquisition without an upward worker dependency. Native cancellation terminates its child.
Progress records source resolution, download byte counts, extraction, conversion and validation. The old Overpass
setting remains for historical configuration compatibility and is not read by new road preparation.

The combined immutable native artifact now includes Osmium and its libraries. This changes its recipe once; subsequent
unchanged builds reuse the complete artifact through the existing release protocol. Local macOS native proofs do not
establish the Linux worker resource envelope; the Linux image and extraction gate must pass before deployment.

The maintained resource proof accepts the requested location and extent; it never hardcodes a production area:

```bash
STORAGE_MODE=local .venv/bin/python traffic-simulator/scripts/benchmark_osm.py \
  --lat 47.6062 --lon -122.3321 --radius-km 2 --output /tmp/osm-road-proof.json
```

This explicitly downloads one canonical current regional snapshot into fresh isolated local scratch, prepares cold
and warm networks from those exact bytes, and runs the real seeded civilian/crew baseline in SUMO twice. It compares
source, bounds, network, gates, resolved scenario, all demand and native accounting. Flushed JSON phase records precede
the final JSON; the optional output file contains that final report. The report includes actual CLI versions,
source hashes, timings and separate process/largest-child RSS peaks; those peaks are not an aggregate memory measure.
Linux cgroup v1/v2 peak/limits are recorded when available. Owned scratch file content and allocated blocks are
sampled every 0.1 seconds; the labelled sampled high-water mark can miss changes between samples and excludes
filesystem metadata. Report the actual filesystem capacity separately: observed usage does not establish an enforced
30 GiB quota. Run the same command inside the verified worker image with the configured CPU/memory limits to measure
that environment. This is an explicit public-data proof command; ordinary tests use offline source fixtures.
