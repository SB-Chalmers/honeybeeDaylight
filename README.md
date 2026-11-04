# Simulation Service

Send a compact building description and get daylight (Foton) and energy
(EnergyPlus) results back. The service is stateless: it rebuilds the Honeybee
model, runs the simulations in temporary folders, answers, and forgets.
Storing results is the client application's job.

```
client ──HTTPS──▶ api (FastAPI) ──▶ Redis queue ──▶ daylight worker (Foton, GPU, 1 job at a time)
                     ▲                         └──▶ energy worker  (EnergyPlus, CPU, 1 job per core)
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
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
on the host. Without a GPU, delete the `deploy:` block of the `daylight`
service and Foton runs on the CPU (much slower).

## API

| Method | Path | Does |
|---|---|---|
| `POST` | `/v1/daylight` | Daylight factor per sensor, plus DA/sDA when `daylight.run_sda` is true |
| `POST` | `/v1/energy` | Annual heating/cooling/total, monthly heat balance, hourly heating and cooling |
| `POST` | `/v1/simulate` | Both, in parallel |
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

- **Weather.** `gothenburg_tmyx.epw` unless the request includes `"epw": "<EPW file text>"`.
- **Floors.** Every floor gets a room and a sensor grid. `sensor_grids` gives
  each floor's `start_sensor_index` and `sensor_count` in the flat arrays.
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
| `python scripts/stress_test.py --key <k> --constructions c.json` | 20 students for 20 minutes (see the script for options) |

Settings live in `.env` (see `.env.example`). The ones you may want to change:
`APP_KEYS`, `ALLOWED_ORIGINS`, `ENERGY_CONCURRENCY` (default: CPU cores),
`RESULT_TTL_SECONDS` (default 600), `FOTON_BACKEND`.

## Deployment

On the workstation (Ubuntu with Docker, the NVIDIA driver and the NVIDIA
Container Toolkit installed):

```bash
sudo systemctl enable docker            # containers come back after a reboot
git clone https://github.com/SB-Chalmers/honeybeeDaylight.git && cd honeybeeDaylight
cp .env.example .env                    # set APP_KEYS
docker compose up -d --build
```

To update: `git pull && docker compose up -d --build`. To roll back:
`git checkout <commit> && docker compose up -d --build`.

Ubuntu installs security updates by itself (`unattended-upgrades`). Leave
driver and Docker upgrades for a quiet day, not during a course.

## Local development

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
FOTON_BACKEND=reference pytest tests/
```

To run the stack without Docker: start `redis-server`, then
`celery -A worker.celery_app worker -Q daylight,energy` (on macOS add
`--pool threads`, because the default pool fails there) and
`uvicorn api.main:app --reload`. Energy needs EnergyPlus 24.2 on `PATH`
(or `EPLUS_BINARY=/path/to/energyplus`).

## Capacity

Measured 2026-11-04 on a 4-core cloud machine (energy only, 1 to 5 storey
buildings, each student waiting 5 to 30 s between requests):

| Students | Median | p95 | Requests/min |
|---|---|---|---|
| 20 | 2.5 s | 10 s | 60 |
| 40 | 4.3 s | 14 s | 113 |
| 80 | 23 s | 34 s | 136 (saturated) |

- **Energy** scales with cores: about 34 runs per minute per core, which is
  roughly **10 active students per core**. Running more workers than cores
  only adds waiting.
- **Daylight** runs one job at a time on the GPU (about 1 s per job on an
  M4 Pro), so it serves about 60 requests per minute. Measure it on the real GPU.
- **Docker** on Linux gives containers every core and all memory unless
  limits are set, so leave `cpus:`/`mem_limit:` out of the compose file. If you
  do limit CPUs with `--cpus`, set `ENERGY_CONCURRENCY` to match, because
  `nproc` in the container still reports every core. Each EnergyPlus run needs
  about 200 MB of memory. Docker Desktop (macOS/Windows) runs in a VM with its
  own CPU and memory limits (Settings → Resources).

## Scaling later

- **More GPUs in one machine:** run one `daylight` worker per GPU, each limited to its own device.
- **More machines:** start `daylight`/`energy` workers on any host that can reach Redis.
  Nothing else changes. Keep Redis private (firewall or a password in `REDIS_URL`).
- **Cloud (e.g. AWS EC2):** a `g5`/`g6` instance (NVIDIA A10G/L4) runs this compose file
  as is with the NVIDIA Container Toolkit. A cheaper pattern is a small always-on instance
  for `api` + `redis` + `energy`, with GPU instances for `daylight` started only for class hours.
  Put HTTPS in front (a load balancer or Caddy/nginx) so API keys are not sent in the clear.
- **Bigger classes:** the queue length (`llen daylight`) tells you when to add workers.
