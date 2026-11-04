# Simulation Service

Send a compact building description and get daylight (Foton) and energy
(EnergyPlus) results back. The service is stateless: it rebuilds the Honeybee
model, runs the simulations in temporary folders, answers, and forgets.
Storing results is the client application's job.

```
client ──HTTPS──▶ api (FastAPI) ──▶ Redis queue ──▶ daylight worker (Foton, GPU, 1 job at a time)
                     ▲                         └──▶ energy worker  (EnergyPlus, CPU, 1 job per core)
                     └──────── result (kept in Redis for 1 h, then gone)
```

Celery spreads the work: workers pull a job when they are free, so a burst of
requests queues up instead of overloading the machine, and more machines can
join by starting workers against the same Redis.

## Run it

```bash
cp .env.example .env          # set APP_KEYS (one secret per client app)
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
| `GET` | `/health` | Liveness (no signature needed) |

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

- Send requests with `Content-Encoding: gzip`. Responses are gzipped when the
  client sends `Accept-Encoding: gzip`.
- Large arrays come back packed: `{"dtype", "scale", "shape", "data"}`, where
  `data` is base64 of a little-endian typed array and `value = raw / scale`.
  DF and DA use `uint16` (0.01 % steps, 2 bytes per sensor). Points and
  hourly series use `float32`. Send `"pack": false` to get plain JSON lists.

```js
const bytes = Uint8Array.from(atob(p.data), c => c.charCodeAt(0));
const raw = new ({uint16: Uint16Array, float32: Float32Array})[p.dtype](bytes.buffer);
const values = Array.from(raw, v => v / p.scale);
```

### Signing requests

Each client app has an id and a secret in `APP_KEYS` (`heath:<secret>,mr-app:<secret>`).
Every request sends:

```
X-App-Id:     heath
X-Timestamp:  <unix seconds>
X-Signature:  hex(HMAC_SHA256(secret, "<timestamp>\n<METHOD>\n<path>\n<sha256 hex of the uncompressed body>"))
```

Requests older than 5 minutes are rejected. **Keep the secret on a server.**
A secret in browser JavaScript is public, so a web app should sign on its
backend or through a small proxy. `scripts/client.py` is a working client.
If `APP_KEYS` is empty, signing is off (local development only).

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
| `python scripts/client.py --secret <s>` | Send one signed test request |
| `python scripts/stress_test.py --secret <s> --constructions c.json` | 20 students for 20 minutes (see the script for options) |

Settings live in `.env` (see `.env.example`). The ones you may want to change:
`APP_KEYS`, `ALLOWED_ORIGINS`, `ENERGY_CONCURRENCY` (default: CPU cores),
`RESULT_TTL_SECONDS` (default 3600), `FOTON_BACKEND`.

## Workstation upkeep

`ops/maintenance.sh` installs OS updates nightly (and reboots if needed) and
emails the superusers when disk, memory, CPU or GPU reach 90 %. Setup
instructions are at the top of the script. The containers come back after a
reboot by themselves (`restart: unless-stopped`) as long as Docker is enabled
(`sudo systemctl enable docker`).

## Local development

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
FOTON_BACKEND=reference pytest tests/
```

To run the stack without Docker: start `redis-server`, then
`celery -A worker.celery_app worker -Q daylight,energy` and
`uvicorn api.main:app --reload`. Energy needs EnergyPlus 24.2 on `PATH`
(or `EPLUS_BINARY=/path/to/energyplus`).

## Scaling later

- **More GPUs in one machine:** run one `daylight` worker per GPU, each limited to its own device.
- **More machines:** start `daylight`/`energy` workers on any host that can reach Redis.
  Nothing else changes. Keep Redis private (firewall or a password in `REDIS_URL`).
- **Cloud (e.g. AWS EC2):** a `g5`/`g6` instance (NVIDIA A10G/L4) runs this compose file
  as is with the NVIDIA Container Toolkit. A cheaper pattern is a small always-on instance
  for `api` + `redis` + `energy`, with GPU instances for `daylight` started only for class hours.
  Put HTTPS in front (a load balancer or Caddy/nginx) since signatures stop tampering but not snooping.
- **Bigger classes:** the queue length (`llen daylight`) tells you when to add workers.
