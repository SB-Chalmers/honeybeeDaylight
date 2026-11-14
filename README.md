# Simulation Service

Send a compact building description and get daylight (Foton) and energy
(EnergyPlus or EPSM RC) results back. The service is stateless: it rebuilds the Honeybee
model, runs the simulations in temporary folders, answers, and forgets.
Storing results is the client application's job.

```
client ──HTTPS──▶ api (FastAPI) ──▶ Redis queue ──▶ daylight worker (Foton on the GPU, 1 job per core)
                     ▲                         └──▶ energy worker   (EnergyPlus or EPSM RC, 1 job per core)
                     └──────── result (kept in Redis for 10 min, then gone)
```

Celery spreads the work: workers pull a job when they are free, so a burst of
requests queues up instead of overloading the machine, and more machines can
join by starting workers against the same Redis.

## Run it

```bash
cp .env.example .env          # set APP_KEYS (one key per client app)
docker compose up -d --build
curl http://localhost:8000/health   # {"status":"ok"}
```

The GPU worker needs the NVIDIA driver and the
NVIDIA Container Toolkit (see [Deployment](#deployment))
on the host. Without a GPU, delete the `deploy:` block of the `daylight`
service and Foton runs on the CPU (much slower).

## API

| Method | Path | Does |
|---|---|---|
| `POST` | `/v1/daylight` | Daylight factor per sensor, plus DA/sDA when `daylight.run_sda` is true |
| `POST` | `/v1/energy` | Annual heating/cooling/total, monthly heat balance, hourly heating and cooling |
| `POST` | `/v1/energy-rc` | The same request and response, computed with EPSM RC (ISO 52016-1) in about 0.1 s; see [EPSM RC energy](#epsm-rc-energy) |
| `POST` | `/v1/simulate` | Both, in parallel |
| `POST` | `/v1/grasshopper` | The request as a Grasshopper file (`heath.ghx`) that rebuilds the same model; see [grasshopper/README.md](grasshopper/README.md) |
| `GET` | `/health` | Liveness (no key needed) |

Full schemas are at `/docs`. A minimal request:

```json
{
  "room": {"footprint_coordinates": [[0,0],[8,0],[8,6],[0,6]], "floors": 3, "wwr": 0.4},
  "context_buildings": [{"footprint_coordinates": [[10,0],[16,0],[16,6],[10,6]], "floors": 4}],
  "daylight": {"run_sda": true},
  "energy": {"constructions": {"wall": "…", "floor": "…", "roof": "…", "window": "…"},
             "building_program": "Office"}
}
```

- **Daylight quality.** `preview` by default (about 0.6 s per job on the
  workstation GPU, twice the throughput). Send `"daylight": {"quality": "final"}`
  for the full sample budget (about 1.2 s).
- **Weather.** `gothenburg_tmyx.epw` unless the request includes `"epw": "<EPW file text>"`.
- **Floors.** By default only `room.selected_floor_number` (1 = ground) is
  simulated. `"simulate_all_floors": true` gives every floor a room and a
  sensor grid, and the job takes proportionally longer. `sensor_grids` gives
  each floor's `start_sensor_index` and `sensor_count` in the flat arrays.
- **Sensor grid.** 0.5 m spacing, 0.75 m above the floor, aligned with the
  first footprint edge. Change it with `room.sensor_grid` (`x_dim`, `y_dim`,
  `offset`). Job time grows with the sensor count.
- **Shared walls.** `room.shared_floors_by_edge` lists, per footprint edge,
  the floors whose wall touches a neighbour. Those walls become adiabatic
  and get no windows.
- **Constructions** are names from the external EPSM service (`EPSM_BASE_URL`).

### Small payloads

- Requests are plain JSON (about 1 KB for a building). Responses are gzipped
  when the client sends `Accept-Encoding: gzip` (browsers always do).
- Large arrays come back packed: `{"dtype", "scale", "shape", "data"}`, where
  `data` is base64 of a little-endian typed array and `value = raw / scale`.
  DF and DA use `uint16` (0.01 % steps, 2 bytes per sensor). Points and
  hourly series use `float32`. Send `"pack": false` to get plain JSON lists.

```js
const bytes = Uint8Array.from(atob(p.data), c => c.charCodeAt(0));
const raw = new ({uint16: Uint16Array, float32: Float32Array})[p.dtype](bytes.buffer);
const values = Array.from(raw, v => v / p.scale);
```

### API keys

Each client app has its own key in `APP_KEYS` (`heath:<key>,mr-app:<key>`)
and sends it as `Authorization: Bearer <key>`. Run the API behind HTTPS so
keys are not readable in transit. A key in browser JavaScript can be read by
anyone using the page, so it keeps out other sites and scripts, not a
determined user. If `APP_KEYS` is empty, keys are not checked (local
development only). `scripts/client.py` is a working client.

To issue a key, generate one, add it to `APP_KEYS` in `.env`, and reload the API:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# APP_KEYS=heath:<key>,mr-app:<key>,new-app:<new key>
docker compose up -d api      # recreates the API with the new .env
```

To revoke one, delete it from `APP_KEYS` and run the same command.
To rotate one without downtime, add the new key under a second name
(`heath-2:<new key>`), switch the client over, then remove the old one.

## Useful commands

| Command | What it does |
|---|---|
| `docker compose up -d --build` | Build and start everything (also how you deploy an update after `git pull`) |
| `docker compose ps` | Status of each service |
| `docker compose logs -f api daylight energy` | Live logs |
| `docker compose restart energy` | Restart one service |
| `docker compose up -d --scale energy=2` | More energy worker containers |
| `docker compose exec redis redis-cli llen daylight` | Daylight jobs waiting in the queue (same for `energy`) |
| `docker compose exec daylight celery -A worker.celery_app inspect active` | Jobs running right now |
| `docker compose down` | Stop everything |
| `python scripts/client.py --key <k>` | Send one test request |
| `python scripts/stress_test.py --key <k> --constructions c.json` | A class of 30 for 10 minutes (bursts, give-ups, retries); writes `report.html` (see the script for steps) |

Settings live in `.env` (see `.env.example`). The ones you may want to change:
`APP_KEYS`, `ALLOWED_ORIGINS`, `ENERGY_CONCURRENCY` / `DAYLIGHT_CONCURRENCY`
(default: chosen from the hardware, see [Capacity](#capacity)),
`RESULT_TTL_SECONDS` (default 600), `FOTON_BACKEND`.

## Deployment

On an Ubuntu workstation with an NVIDIA GPU and Docker. Steps 1 and 2 are
once per machine.

1. **Check the driver.** `nvidia-smi` should list the GPU.

2. **Install the NVIDIA Container Toolkit** (needs sudo):

   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker && sudo systemctl enable docker
   ```

   The restart stops every container on the machine for a few seconds, not
   only this one. `docker run --rm --gpus all ubuntu nvidia-smi -L` should
   then list the GPU.

3. **Clone and configure:**

   ```bash
   git clone https://github.com/SB-Chalmers/honeybeeDaylight.git && cd honeybeeDaylight
   cp .env.example .env && chmod 600 .env
   ```

   In `.env`, set:
   - `APP_KEYS`: one key per client app (see [API keys](#api-keys)).
   - `FOTON_BACKEND=vulkan`, so daylight fails loudly if the GPU is missing
     instead of falling back to the CPU (minutes per job instead of seconds).
   - Leave `ENERGY_CONCURRENCY` and `DAYLIGHT_CONCURRENCY` unset: the workers
     size themselves from the cores and GPUs they see.

4. **Start it:** `docker compose up -d --build --wait`

5. **Check that Foton sees the GPU inside the container:**

   ```bash
   docker compose exec daylight python -c "from foton import Engine; Engine({'backend': 'vulkan'}); print('vulkan ok')"
   ```

   If it says `Unable to find a Vulkan driver`, run it again with
   `-e VK_LOADER_DEBUG=error` after `exec` to see which library is missing.

6. **Send a request with a key:**
   `python scripts/client.py --url http://<host>:8000 --key <key> --path /v1/daylight`.
   Without a key the API answers 401.

To update: `git pull && docker compose up -d --build`. To roll back:
`git checkout <commit> && docker compose up -d --build`.

Ubuntu installs security updates by itself (`unattended-upgrades`). Leave
driver and Docker upgrades for a quiet day, not during a course.

## Local development

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-epsm.txt pytest   # EPSM needs Rust (rustup.rs)
FOTON_BACKEND=reference pytest tests/
```

The EnergyPlus test runs only when `energyplus` is on `PATH`.

To run the stack without Docker: start `redis-server`, then
`celery -A worker.celery_app worker -Q daylight,energy` (on macOS add
`--pool threads`, because the default pool fails there) and
`uvicorn api.main:app --reload`. Energy needs EnergyPlus 24.2 on `PATH`
(or `EPLUS_BINARY=/path/to/energyplus`).

## Capacity

Worker counts are chosen at startup by `worker/concurrency.py`: one process per
physical core for each queue. A daylight job is almost all CPU work (rebuilding
and meshing the Honeybee model), so the GPU is not the limit; the cores are.
Each EnergyPlus run needs about 200 MB of memory. Leave `cpus:`/`mem_limit:` out
of the compose file; if you do limit CPUs, the workers see it and run fewer processes.

Measured 2026-11-14 on the Precision 7540 (i9-9880H, 8 cores, Quadro RTX 3000):
24 clients for 5 minutes, each sending a random building to `/v1/daylight` and
`/v1/energy-rc` at the same time and waiting for both: 872 runs a minute, median
1.1 s, p95 1.6 s, no errors in 8,736 calls. The energy workers were 95% busy and
the GPU 27%. With EnergyPlus (`/v1/energy`) instead of EPSM RC, the same machine
managed 145 runs a minute at a median of 9.3 s (2026-11-12).

One request on an idle machine takes about 0.5 s (daylight `preview`), 0.15 s
(EPSM RC) or 2.5 s (EnergyPlus).

Two things keep the queues short (`api/jobs.py`):

- **Identical requests share one job.** The job is keyed by a hash of the fields
  it reads, so a repeated request reuses a queued, running or finished job.
  Results stay for `RESULT_TTL_SECONDS`.
- **Abandoned jobs are cancelled.** When every client waiting for a job has
  disconnected before it starts, the job is revoked and never runs.

`/v1/simulate` answers when both daylight and energy are done. To show each as
soon as it is ready, call `/v1/daylight` and `/v1/energy-rc` at the same time.

More machines can join by starting `daylight`/`energy` workers against the same
Redis (keep Redis private). The queue length (`llen daylight`) tells you when.

## EPSM RC energy

`POST /v1/energy-rc` takes the `/v1/energy` request and returns the same result,
computed with [EPSM RC](https://github.com/SB-Chalmers/epsm_rc) (ISO 52016-1 hourly
model) instead of EnergyPlus. `simulation/energy/rc.py` builds the same building
EnergyPlus gets:
- footprint, storeys, orientation;
- per-facade window ratio, with shared walls as adiabatic party walls;
- the overhang, context buildings, and the building's own wings shading each other
  (L, U and courtyard plans);
- the EPSM constructions and the Honeybee program's loads and schedules;
- ideal air, and natural ventilation.

Window fins are not modelled. A run takes about 0.1 s, against 2 to 4 s for EnergyPlus.

On 24 test buildings in Göteborg, against `/v1/energy`:
- **Monthly heating** is within ±1 kWh/m² in 76% of months (mean miss 0.7 kWh/m²).
- **Monthly cooling** is within ±1 kWh/m² in 84% of months (mean miss 0.5 kWh/m²).
- **Design changes** (glazing, insulation, windows, rotation, overhangs, neighbours,
  natural ventilation) move heating and cooling in the same direction as EnergyPlus.

RC over-predicts heating more the colder the climate; EnergyPlus's dynamic window
films lose less heat than ISO's fixed coefficients.

The EPSM packages are pinned in `requirements-epsm.txt` and built into wheels in the
Dockerfile's first stage (epsm_rc needs Rust).
