# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM python:3.13-slim AS deps

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.13-slim

# System libraries required by Radiance and numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates unzip \
        libglu1-mesa libxext6 libxi6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Radiance 5.4a — LBNL-ETA build required by lbt-recipes/honeybee-radiance
# https://github.com/LBNL-ETA/Radiance/releases/tag/04ef26bb
RUN wget -q -O /tmp/radiance.zip \
      "https://github.com/LBNL-ETA/Radiance/releases/download/04ef26bb/Radiance_04ef26bb_Linux.zip" \
    && unzip -q /tmp/radiance.zip -d /tmp/ \
    && tar -xzf /tmp/radiance-5.4.04ef26bb60-Linux.tar.gz -C / --strip-components=1 \
    && rm -f /tmp/radiance.zip /tmp/radiance-5.4.04ef26bb60-Linux.tar.gz

ENV PATH="/usr/local/radiance/bin:${PATH}"
ENV RAYPATH="/usr/local/radiance/lib"

# Python packages (copied from build stage — no pip, no build tools in image)
COPY --from=deps /install /usr/local

# Application code
WORKDIR /app
COPY api/        ./api/
COPY simulation/ ./simulation/
COPY pycompat/   ./pycompat/
COPY weather/    ./weather/

# Runtime directories (overridden by docker-compose volumes in production)
RUN mkdir -p studies data

EXPOSE 8000

# 2 workers handles a typical classroom load; tune via WORKERS env var
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS:-2}"]
