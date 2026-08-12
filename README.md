# Daylight Simulation API

![Python](https://img.shields.io/badge/python-3.13-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.139-009688?logo=fastapi&logoColor=white)
![Foton](https://img.shields.io/badge/Foton-0.2.0-blueviolet)
![Honeybee](https://img.shields.io/badge/Honeybee-models-yellow?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PC9zdmc+)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![Tests](https://img.shields.io/badge/tests-65%20passing-brightgreen?logo=pytest&logoColor=white)

A teaching tool that accepts a building description and returns daylight simulation results (Daylight Factor and spatial Daylight Autonomy) via a REST API. Built with [Honeybee](https://www.ladybug.tools/honeybee.html) + [Foton](https://github.com/snjsomnath/foton) + [FastAPI](https://fastapi.tiangolo.com/).

---

## Deploying on a University VM

### Prerequisites (one-time)

```bash
# Install Docker (Ubuntu / Debian)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in for the group change to take effect.
```

### 1. Clone the repository

```bash
git clone <your-repo-url> honeybeeDaylight
cd honeybeeDaylight
```

### 2. Add weather files

Copy at least one `.epw` file into `./weather/`. The filename becomes the location ID exposed by `GET /v1/locations`.

```bash
# Example
cp /path/to/gothenburg.epw weather/gothenburg.epw
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set `ALLOWED_ORIGINS` to the URL of your frontend app:

```
ALLOWED_ORIGINS=https://your-frontend.youruni.ac.uk
```

Leave it as `*` during local testing.

### 4. Start the API

```bash
docker compose up -d
```

The API is now running at `http://localhost:8000`.
Interactive docs: `http://localhost:8000/docs`

### 5. Verify

```bash
curl http://localhost:8000/v1/locations
# → {"locations":[{"id":"gothenburg","label":"Gothenburg"}]}
```

---

## Updating

```bash
git pull && docker compose up -d --build
```

---

## Exposing on a public URL (nginx)

Most university servers have nginx. Add a site config:

```nginx
# /etc/nginx/sites-available/daylight
server {
    listen 80;
    server_name daylight.youruni.com;

    location / {
        proxy_pass         http://localhost:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/daylight /etc/nginx/sites-enabled/
sudo nginx -s reload
```

Then update `.env`:

```
ALLOWED_ORIGINS=https://daylight.youruni.com
```

```bash
docker compose restart api
```

---

## Useful ops commands

| Command | Purpose |
|---|---|
| `docker compose logs -f` | Live logs |
| `docker compose ps` | Status and health |
| `docker compose restart api` | Restart without rebuild |
| `docker compose down` | Stop everything |
| `docker compose up -d --build` | Rebuild and redeploy |

---

## Runtime volumes

Three directories are mounted as Docker volumes. They persist across container restarts and rebuilds.

| Directory | Contents |
|---|---|
| `./weather/` | `.epw` weather files — drop new cities here |
| `./studies/` | Per-study simulation outputs |
| `./data/` | SQLite database (`studies.db`) |

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/studies` | Submit a target building plus optional context buildings + start study |
| `GET` | `/v1/studies/{id}` | Poll study status |
| `GET` | `/v1/studies/{id}/result` | Fetch plottable results |
| `GET` | `/v1/studies/{id}/model.hbjson` | Download the generated Honeybee model |
| `POST` | `/v1/studies:run-sync` | DF only, returns results directly |
| `GET` | `/v1/locations` | List available weather locations |

Full request/response schemas at `/docs`.

### Multi-floor target buildings and context

By default, the API creates one stacked Honeybee Room and one SensorGrid for
every target-building floor. Results remain flat ordered point-cloud arrays;
`sensor_grids` identifies each floor's `start_sensor_index` and `sensor_count`.
Set `simulate_all_floors` to `false` to retain the legacy selected-floor mode.

To analyze a site with multiple buildings, submit one request per target
building in parallel. Put every other building in that request's
`context_buildings` array. Context buildings are exported as opaque detached
geometry, so they can shade the target without receiving sensor grids.

```json
{
    "room": {
        "footprint_coordinates": [[0,0],[6,0],[6,5],[0,5]],
        "floor_to_floor_height": 3.0,
        "floors": 4,
        "simulate_all_floors": true,
        "selected_floor_number": 4
    },
    "context_buildings": [
        {
            "footprint_coordinates": [[8,0],[14,0],[14,5],[8,5]],
            "floors": 4,
            "floor_to_floor_height": 3.0
        }
    ],
    "run_sda": false,
    "quality": "full"
}
```

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest

# Run tests
pytest tests/ -v

# Start dev server on localhost:8000 (auto-reload)
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Open the API at `http://127.0.0.1:8000` and interactive docs at
`http://127.0.0.1:8000/docs`.

### If a previous process did not close cleanly

If you see `address already in use` for port `8000`, stop the old server and
start again:

```bash
# 1) Try graceful stop first
lsof -ti :8000 | xargs kill

# 2) If still busy, force stop
lsof -ti :8000 | xargs kill -9

# 3) Start the API again
source .venv/bin/activate
uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload
```

Quick check that the API is up:

```bash
curl -I http://127.0.0.1:8000/docs
```

> Foton automatically selects Metal, Vulkan, or its deterministic reference CPU
> backend. Set `FOTON_BACKEND=reference` to force CPU execution. Annual sDA still
> needs Radiance `gendaymtx` for EPW/WEA sky-matrix preparation; daylight
> transport and metric reduction are performed by Foton. The Docker image
> provides `gendaymtx` automatically.
>
> API daylight studies use Foton's final-quality solver settings with the
> 146-patch Tregenza sky basis. The legacy `quality: "draft"` request value is
> still accepted for client compatibility but no longer lowers solver quality.
