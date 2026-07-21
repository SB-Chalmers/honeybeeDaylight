# Daylight Simulation API — Teaching Tool

## Context
Students use a separate design app to describe a building and submit it to this service for daylight simulation. The API must be:
- **Simple** — one request to start a study, one to get results
- **Stateless from the student's perspective** — no session management, no room IDs to track
- **Fast enough for a classroom** — DF in seconds; sDA in under a minute on typical student models
- **Server-managed** — EPW files live on the server; students pick a location by name

## Current Status
| Component | Status |
|---|---|
| JSON-driven room inputs (`study_info.json`) | ✅ Done |
| Footprint → Honeybee room (polyface extrusion) | ✅ Done |
| Orientation offset (rotation around Z) | ✅ Done |
| Per-cardinal WWR + apertures | ✅ Done |
| `wwr` single-float shorthand (fan-out to all cardinals) | ✅ Done |
| Wall thickness → overhang + fin shading | ✅ Done |
| Additional H/V shading depth | ✅ Done |
| Sensor grid from footprint (configurable spacing) | ✅ Done |
| Multi-floor elevation offset | ✅ Done |
| DF simulation (`daylight_minimal_optimised.py`) | ✅ Done |
| Annual daylight / multi-recipe (`daylight_sims.py`) | ✅ Done |
| Result visualisation (`vis_results.py`, `vis_annual_results.py`) | ✅ Done |
| Simulation modularised (`inputs`, `model`, `runner`, `results`) | ✅ Done |
| Unit tests (`tests/test_inputs.py`, `test_model.py`, `test_results.py`) | ✅ Done |
| Folder housekeeping (`simulation/`, `scripts/`, `api/`, `.gitignore`) | ✅ Done |
| FastAPI HTTP layer | ✅ Done |
| `GET /v1/locations` endpoint | ✅ Done |
| SQLite study store (`api/store.py`) | ✅ Done |
| `Dockerfile` + `docker-compose.yml` | ✅ Done |
| `requirements.txt`, `.dockerignore`, `.env.example` | ✅ Done |
| `README.md` with deployment instructions | ✅ Done |
| API running locally on `localhost:8000` | ✅ Done |
| `DaylightService.ts` (API client in frontend) | ⬜ Not started |
| `BuildingConfig` — add `orientation_offset`, `wall_thickness` | ⬜ Not started |
| `DesignNode` — add `study_id`, replace dummy metrics with API values | ⬜ Not started |

## Goal
Turn the existing Honeybee/Radiance workflow into a simple web API where a client can:
1. Send room inputs: footprint coordinates, orientation offset (defaults to north), floor-to-floor height, WWR per cardinal direction, selected floor number for simulation, sensor grid config (optional, defaulting to 0.5 m × 0.5 m grid), wall thickness (drives overhang + fin shading depth), optional additional horizontal shading depth, optional additional vertical shading depth.
2. Run a daylight study: DF or sDA.
3. Receive a plottable response.

## Architecture
Two layers, no message broker needed at this scale:
1. **FastAPI** — validation, geometry build, background task dispatch, response
2. **FastAPI `BackgroundTasks`** — runs the Honeybee recipe in-process; no Redis/Celery/RQ unless concurrent load requires it

Flow:
1. Student POSTs building description + study type → gets `study_id` immediately
2. Study runs as a background task
3. Student polls status until `complete`
4. Student fetches plottable result

For DF specifically, a sync convenience route returns results directly (typically <10s).

## Endpoints

### 1) Start Study
`POST /v1/studies`

DF always runs. Set `run_sda: true` to also run annual daylight (requires `location`).

`wwr` sets the same ratio on all four walls. Use `wwr_by_cardinal` to override per direction. If both are present, `wwr_by_cardinal` wins.

Request:
```json
{
  "run_sda": false,
  "location": "gothenburg",
  "thresholds": { "da_lux": 300, "sda_target_pct": 50 },
  "quality": "draft",
  "room": {
    "footprint_coordinates": [[0,0],[6,0],[6,5],[0,5]],
    "floor_to_floor_height": 3.0,
    "selected_floor_number": 1,
    "orientation_offset": 0,
    "wwr": 0.4,
    "wwr_by_cardinal": { "north": 0.40, "east": 0.30, "south": 0.25, "west": 0.20 },
    "wall_thickness": 0.30,
    "additional_horizontal_shading_depth": 0.00,
    "additional_vertical_shading_depth": 0.00,
    "sensor_grid": { "x_dim": 0.5, "y_dim": 0.5, "offset": 0.75 }
  }
}
```

Response — `sensor_count` is echoed so the frontend can validate result array length before polling:
```json
{
  "study_id": "std_456",
  "status": "queued",
  "sensor_count": 192,
  "links": {
    "status": "/v1/studies/std_456",
    "result": "/v1/studies/std_456/result"
  }
}
```

### 2) Check Status
`GET /v1/studies/{study_id}`

```json
{ "study_id": "std_456", "status": "running", "stage": "daylight_factor_ray_tracing" }
```

`status` values: `queued` → `running` → `complete` | `failed`

### 3) Get Result
`GET /v1/studies/{study_id}/result`

Schema is always the same. `sda` is `null` when `run_sda` was `false`.

```json
{
  "study_id": "std_456",
  "df": {
    "summary": { "mean_df": 2.8, "min_df": 0.4, "max_df": 6.3 },
    "values": [2.1, 2.5, 3.0]
  },
  "sda": {
    "summary": { "sda_300_50": 61.5, "sensor_count": 96, "passing_sensors": 59 },
    "values": [44.0, 57.2, 62.8],
    "pass": [false, true, true]
  }
}
```

```json
{
  "study_id": "std_457",
  "df": {
    "summary": { "mean_df": 2.8, "min_df": 0.4, "max_df": 6.3 },
    "values": [2.1, 2.5, 3.0]
  },
  "sda": null
}
```

### 4) Sync Convenience Route (DF only)
`POST /v1/studies:run-sync`

- Same request body as `POST /v1/studies` but `run_sda` is ignored (always `false`)
- Returns the full result payload directly (no polling); `sda` is always `null`
- Returns `202` + `study_id` if the study exceeds 30s (falls back to async flow)

### 5) List Available Locations
`GET /v1/locations`

Returns the EPW files available on the server so the frontend can populate a dropdown without hardcoding strings.

```json
{
  "locations": [
    { "id": "gothenburg", "label": "Gothenburg, Sweden" },
    { "id": "stockholm", "label": "Stockholm, Sweden" }
  ]
}
```

### Error responses
All errors return the same shape so the frontend has one code path:
```json
{ "error": "invalid_footprint", "message": "Footprint must have at least 3 points." }
```

HTTP status codes: `400` validation, `404` study not found, `422` schema error, `500` simulation failure.

## Internal Data Flow
1. Validate room params (footprint ≥ 3 pts, WWR in [0,1], floor ≥ 1, etc.)
2. Build Honeybee room from footprint + orientation + shading params
3. Apply per-cardinal apertures + overhang/fin shading
4. Build sensor grid
5. Export HBJSON to per-study working folder
6. Run recipe as background task; write status to in-memory store (dict for MVP, DB later)
7. Parse result files (`.res` / `.da` + `.pts`) into plot payload
8. Store payload; mark study `complete`

## Storage and Execution
- Framework: **FastAPI**
- CORS: enabled for all origins in development; lock to the frontend origin in production
- Async: **FastAPI `BackgroundTasks`** (upgrade to RQ/Celery only if concurrent class load requires it)
- Study state: in-memory dict for MVP; SQLite for persistence
- Artifacts: per-study folder under `./studies/{study_id}/`
- EPW files: server-managed under `./weather/`; students reference by location name
- HBJSON: written per study, discarded after result is parsed

## Contracts to Freeze Early
1. Geometry conventions
- Footprint winding order: counter-clockwise when viewed from above (Ladybug Tools convention)
- Floor index semantics: 1-based; floor elevation = `(selected_floor_number - 1) × floor_to_floor_height`
- Orientation reference: `orientation_offset = 0` means north aligned with +Y axis; positive values rotate clockwise
- Footprint coordinates: 2D `[x, y]` pairs in metres; polygon auto-closed if last point ≠ first

2. Shading conventions
- `wall_thickness` is the **baseline** shading depth applied as both overhang and left/right fins
- `additional_horizontal_shading_depth` stacks on top of `wall_thickness` for the overhang only
- `additional_vertical_shading_depth` stacks on top of `wall_thickness` for fins only
- Shading is skipped on a face if its resolved depth is 0

3. Grid conventions
- Sensor spacing: `x_dim`, `y_dim` (default 0.5 m)
- Sensor offset from floor: `offset` (default 0.75 m)
- Grid falls back to script defaults if `sensor_grid` key is omitted

4. WWR conventions
- `wwr` (single float) applies the same ratio to all four cardinals — matches the frontend's single slider
- `wwr_by_cardinal` overrides per direction; if both are present, `wwr_by_cardinal` wins
- Both map `[0, 1]`; faces with resolved `wwr = 0` receive no aperture and no shading

5. Result payload
- Schema is always identical regardless of which studies ran
- `df` block is always present
- `sda` block is always present but `null` when `run_sda` was `false`
- DF: `df.values[]` — one float per sensor
- sDA: `sda.values[]` (DA %) + `sda.pass[]` (boolean), same length and index order
- `summary` blocks carry scalar KPIs; frontend never needs to compute them
- No coordinates — client reconstructs positions from the room params it already sent

6. Config file
- `study_info.json` at project root is the single input file
- Must contain a `room_inputs` key with the room schema above
- Recipe writes its own `results/study_info.json` separately; do not conflate

7. Frontend adapter conventions
- `BuildingData.points` is `Point3D[]` (x/y/z) — drop z before sending; z is always 0 at ground level
- `window_to_wall_ratio` is a single float in the frontend — fan out to all four cardinals when building the request, or add per-cardinal fields to `BuildingConfig`
- `orientation_offset` and `wall_thickness` are not yet in `BuildingConfig` — must be added
- `DesignNode.metrics.spatialDaylightAutonomy` maps to `sda.summary.sda_300_50` from the result; `mean_df` is a new metric field
- `DesignNode` needs a `study_id` field to link a saved design to its API result

## Minimal MVP Scope
Implement only these endpoints first:
1. `POST /v1/studies`
2. `GET /v1/studies/{id}`
3. `GET /v1/studies/{id}/result`
4. `POST /v1/studies:run-sync`
5. `GET /v1/locations`

This is enough to support DF end-to-end with frontend plotting. sDA follows once DF is wired up.