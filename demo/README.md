# Foton demo

A one-page proof of concept: change a room with sliders on a phone and see
the daylight factor that Foton computes on the GPU. The page calls the
simulation API's `POST /v1/daylight`; it does not run Foton itself, so it
shares the API's daylight queue with every other app.

```
phone ──▶ nginx :8080 ──(adds API key)──▶ api :8000 /v1/daylight ──▶ daylight queue
```

- `index.html`: the page (no libraries).
- `nginx.conf.template`: serves the page and forwards `/v1/daylight` with the
  key, so the key stays on the server. Limits each visitor to 20 runs a minute
  so the demo cannot crowd out class use.
- `docker-compose.yml`: one `nginx:alpine` container, about 10 MB of memory.

## Run it

On the machine that runs the simulation API:

1. Add a key for the demo to `APP_KEYS` in the API's `.env`
   (`...,foton-demo:<key>`) and reload the API: `docker compose up -d api`.
2. In this folder: `echo DEMO_KEY=<key> > .env && chmod 600 .env`
3. `docker compose up -d`
4. Open `http://<host>:8080`.

`DEMO_PORT` and `API_URL` in `.env` change the port and the API address
(default `http://host.docker.internal:8000`). To stop: `docker compose down`.
