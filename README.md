# Daylight Simulation API

![Python](https://img.shields.io/badge/python-3.13-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.139-009688?logo=fastapi&logoColor=white)
![Radiance](https://img.shields.io/badge/Radiance-5.4-orange)
![Honeybee](https://img.shields.io/badge/Honeybee-Radiance-yellow?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PC9zdmc+)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![Tests](https://img.shields.io/badge/tests-51%20passing-brightgreen?logo=pytest&logoColor=white)

A teaching tool that accepts a building description and returns daylight simulation results (Daylight Factor and spatial Daylight Autonomy) via a REST API. Built with [Honeybee](https://www.ladybug.tools/honeybee.html) + [Radiance](https://www.radiance-online.org/) + [FastAPI](https://fastapi.tiangolo.com/).

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
| `POST` | `/v1/studies:run-sync` | DF only, returns results directly |
| `GET` | `/v1/locations` | List available weather locations |

Full request/response schemas at `/docs`.

### Multi-building context

The API runs one target-room study at a time. To analyze a site with multiple
buildings, submit one request per target building in parallel. Put every other
building in that request's `context_buildings` array. Context buildings are
exported as opaque detached geometry, so they can shade the target building
without receiving sensor grids or their own daylight results.

```json
{
    "room": {
        "footprint_coordinates": [[0,0],[6,0],[6,5],[0,5]],
        "floor_to_floor_height": 3.0,
        "selected_floor_number": 1
    },
    "context_buildings": [
        {
            "footprint_coordinates": [[8,0],[14,0],[14,5],[8,5]],
            "floors": 4,
            "floor_to_floor_height": 3.0
        }
    ],
    "run_sda": false,
    "quality": "draft"
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

# Start dev server (auto-reload)
uvicorn api.main:app --reload
```

> Radiance binaries must be installed at `/usr/local/radiance` (or wherever
> `honeybee_radiance.config.folders.radiance_path` points) for simulations to run.
> The Docker image handles this automatically.
