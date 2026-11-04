# Fire exposure modeler

Independent Python 3.14 component for the FireGUard map. The application API resolves owned immutable fire
observations, obtains suitable DTN weather, freezes inputs and retains outputs. This component performs bounded
calculations only; it owns no visitor sessions, databases, queues or agent prompts. The separate public-data
acquisition helper runs in the API's owned background job before frozen inputs reach the numerical pool.

## Modes and interpretation

- **Landscape surface-fire demo:** actual LANDFIRE LF2024 FBFM40 fuel and LF2020 elevation on aligned regional
  30 m grids, DEM-derived slope/aspect, suitable DTN wind and explicitly assumed moisture/open-fuel wind exposure.
  Pyrothermel supplies local behavior; a directed arrival grid integrates terrain-plane travel times through
  every crossed cell. Nonburnable water blocks this surface-fire mode; unknown data never becomes flat terrain,
  fuel, zero wind or a pretend firebreak. The reported point remains a demo ignition, rather than a verified
  current active front. No spotting, crown fire, suppression or urban structure-fire propagation is modeled.
  Missing inputs, a nonburnable ignition cell, invalid seals and reached data/domain boundaries produce a typed
  failure with empty geometry. The API may acquire a larger crop from the same products for `DOMAIN_TRUNCATED`;
  this changes the recorded domain, not the engine or source. Observed calm wind and native zero spread are valid;
  successful zero-spread output contains only the initialized cell.
- **Exposure:** distance bands around a reported point or reliably matched perimeter, plus a bounded point-anchored
  downwind ellipse when suitable wind exists. Distances and wind shaping are explicit demo settings, not spread
  rates, arrival predictions or calibrated burn probabilities. Missing/stale/calm wind preserves distance context.
- **Surface-fire demo scenario:** Pyrothermel 0.1.4 / Behave calculates directional surface-fire spread for a
  homogeneous standard fuel model, a stated moisture preset, flat slope, open unsheltered fuels and constant wind.
  The reported point is treated as a demo ignition; elapsed-time outlines do not mean future arrival at an
  existing fire. Observed perimeters, spotting, crown fire, suppression and changing weather are not propagated.
  Missing wind or unavailable native output produces a typed unavailable scenario.

The latter two contracts remain compatible with retained historical receipts. They are separate context/scenarios,
not replacement results for a failed landscape assessment.

Coordinate support is global WGS84, including zero, high latitudes, poles and antimeridian crossings. The matched
perimeter changes proximity context; it does not establish an active front. Provider discovery coverage is separate
from coordinate support. No manual landscape download or fixed geographic scenario is required.

## Contracts and HTTP

`fire_risk_modeler/models.py` is the pure canonical contract. The application API and schema exporter can import it
without loading settings, model libraries, the service or native calculator.

- `GET /health`: process health. Startup validates declared settings and the private service credential.
- `POST /v1/exposure/assessments`: authenticated synchronous calculation; **200** with a completed result. It creates
  no retained modeler resource and sends no `Location` header. Body/hash/resolution/vertex limits are validated
  before calculation. Capacity limits return **429**; bounded timeouts/unavailable dependencies return **503**.
- `POST /v1/landscape/assessments`: private schema-2 calculation of one sealed numerical input. A completed
  success or typed scientific/input failure returns **200**; transport/admission failures use explicit HTTP
  errors. Cell, directional-stencil, compressed-body, output-body and concurrent-calculation limits apply.
  A configured calculation deadline of zero means no arbitrary wall-clock cutoff. Public saved reads never call
  a provider or this service; the API owns independently renewed job leases and publication.

Requests carry an immutable `fire_ref`, separate assessment intent ID and the canonical `model_input_hash`.
Identical frozen inputs may safely calculate again after an ambiguous network timeout; API reservation/publication
owns idempotency. The API's retained-result GET never calls this service.

`FIRE_RISK_SERVICE_KEY` is a server-only bearer credential with at least 32 non-whitespace characters. ECS provides
it through Secrets Manager. `FIRE_RISK_ENVIRONMENT=ecs` requires every declared setting as an environment binding;
local mode reads the existing defaults from `deployment/settings.toml`. Settings and ranges have one authority.

## Local development and checks

After repository bootstrap, set `FIRE_RISK_SERVICE_KEY` through your local environment, using the same value for
the API's private-modeler binding. Do not put it in Vite variables or command arguments.

```bash
.venv/bin/python fire-risk-modeler/scripts/local_server.py
PYTHONPATH=fire-risk-modeler .venv/bin/python -m pytest fire-risk-modeler/tests
.venv/bin/ruff check fire-risk-modeler
```

The local service binds `127.0.0.1:8085`; ECS binds port 8085 inside its private network. The test suite verifies
physical distances, global geometry, holes, downwind direction, immutable identity/hash behavior, missing weather,
authentication, capacity retained through timed-out work, reference native output and cumulative scenario outlines.

Build from the repository root:

```bash
docker build -f fire-risk-modeler/Dockerfile --target verification .
docker build -f fire-risk-modeler/Dockerfile --target runtime .
```

The builder compiles the native calculator using the repository lock for runtime and isolated build dependencies.
The runtime contains no compiler, runs as an unprivileged user and uses the deployment's read-only filesystem /
scratch-storage conventions. Calculations perform no network I/O.

## Landscape implementation and proof

`models.py` remains a pure import boundary. `landscape_acquisition.py` exposes
`prepare_landscape(fire, radius_m, limits)` to the background API worker. The limits dictionary supplies
`timeout_seconds`, `max_response_bytes` and `max_cells`; zero timeout uses HTTPX's no-deadline mode. Only canonical
regional `LF2024_FBFM40_*` and `LF2020_Elev_*` endpoints are admitted for CONUS, Alaska and Hawaii. Numerical TIFF
requests disable the default renderer, use nearest-neighbor categorical sampling and verify crop georeferencing.

`landscape.py` seals little-endian fuel int16 and elevation float32 arrays with bounded zlib/base64 encoding.
It validates exact standard fuel codes, decompressed size/hash, seed containment and no-data derivatives.
A geodesic Jacobian converts regional projected gradients and edge directions to physical east/north, accounting
for grid scale and true north; raster rows increase southwards. Elevation is metres, native slope is degrees and
aspect is downslope compass direction. Sources, source TIFF hashes, raster hash, acquisition time, CRS, resolution
and extent remain in the immutable manifest.

`landscape_spread.py` uses native head/backing rates and the exact Behave `FromIgnitionPoint` radial equation.
It caches exact local states without slope/aspect quantization. Primitive directional edges integrate each
intersected cell and conservatively block diagonal corner crossings through nonburnable data. Travel distance
includes the local terrain rise. This is a finite-grid approximation, not a normal-front level-set algorithm or
a claim of equivalence to an operational fire simulator. A radius-five stencil still has angular discretization
error; spatial refinement alone does not remove that error.

Tests compare all 40 standard fuel codes against native directional outputs, spatial/directional refinement
against uniform wavelets, real fuel/elevation data in `tests/data/king-landscape.json`, water walls, diagonal
barriers, unknown data, source-point conflicts, calm wind, zero spread and each regional acquisition path.
The retained KING crop is one public-data fixture; its test wind is explicitly synthetic. Successful local
execution is not a deployed Linux performance measurement, U.S.-wide coverage proof or live DTN acceptance.

The maintained `scripts/benchmark_landscape.py` measures the actual solver with explicitly synthetic wind,
fuel and terrain, without provider requests. In the verified Linux runtime, mount `fire-risk-modeler/scripts`
read-only at `/benchmark`, apply the CPU/memory limits from the captured `deployment/settings.toml`, and run:

```bash
python /benchmark/benchmark_landscape.py --max-grid --enforce-resource-limits --output /tmp/maximum.json
```

Both maximum-grid cases run by default: an all-burnable windy domain and an extreme varied-fuel/terrain interior
with an explicit nonburnable boundary. The report retains failures and partial reached work, native-state counts,
runtime, request/response sizes, the actual result's DynamoDB attribute envelope, process RSS and Linux cgroup
usage. Flushed progress records distinguish ongoing
work from a stalled process. There is no calculation deadline. `--size 102` selects a separate representative
measurement; it does not prove the 250,000-cell capacity. One isolated calculation does not prove concurrent
admission capacity, and synthetic capacity evidence does not establish source coverage or forecast validity.
The measured maximum varied case returned 198,183 response bytes; the declared response capacity is 256 KiB.
The API also enforces an independent 350 KiB retained attribute-envelope guard, including nested coordinates
and record metadata. A finished numerical measurement is not a passing capacity result unless both bounds hold.

## Native calculator evidence and licenses

[Pyrothermel 0.1.4](https://pypi.org/project/pyrothermel/0.1.4/) uses the
[MIT license](https://github.com/j-tenny/pyrothermel/blob/master/LICENSE.txt), and wraps
[Behave's public-domain implementation](https://github.com/firelab/behave/blob/master/LICENCE.md).
[pyproj](https://github.com/pyproj4/pyproj) uses MIT; [Shapely](https://pypi.org/project/shapely/) uses BSD, and
its GEOS dependency retains LGPL terms. These do not require a simulation-software subscription. Existing provider
terms and AWS operating costs still apply.

The unmodified PyPI 0.1.4 source built and ran under Python 3.14.6 on macOS ARM64. Its
[published README example](https://github.com/j-tenny/pyrothermel) for TL8, low dead/moderate live moisture,
zero wind and the stated canopy inputs yielded **0.035296680717723905 km/h** and **0.8505040748815654 m** flame
length, matching the published rounded reference. The component test reproduces that reference.

Metric Pyrothermel inputs/outputs use km/h; the canonical service contract uses m/s and converts explicitly.
`wind_input_mode="ten_meter"` adjusts the stated 10 m wind to midflame with zero canopy cover/height/ratio.
This is an explicit demo assumption because the current DTN adapter does not establish measurement height.
Verified native direction: wind **from** 0 degrees produces maximum spread toward 180 degrees, and reversing
wind reverses preferred growth. The adapter passes meteorological FROM directly to Behave; geometry uses the
calculator's geographic direction-of-interest rates without an invented ellipse ratio.

For standard fuel 1, moderate dead/live moisture, open canopy, flat slope and 36 km/h ten-metre wind, the native
head rate was **6.510120889882055 km/h**, backing **0.4751908322819036 km/h** and crosswind
**0.8857299106933826 km/h**. These verify integration/units, not the actual fuels or future behavior at a fire.
The Linux Docker verification target also passed all 41 component tests, including the native references.
The runtime image built and served `/health` in an isolated container. These verify packaged execution;
AWS service publication and live provider acceptance remain separate release checks.
