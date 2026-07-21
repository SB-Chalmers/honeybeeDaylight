# ── Stage 1: Python dependencies ─────────────────────────────────────────────
FROM python:3.13-slim AS deps

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.13-slim

# System libraries required by Radiance and numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates \
        libglu1-mesa libxext6 libxi6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Radiance ray-tracing binaries
# Official NREL release: https://github.com/NREL/Radiance/releases
ARG RADIANCE_VERSION=5.4.0
RUN wget -q -O /tmp/radiance.tar.gz \
      "https://github.com/NREL/Radiance/releases/download/${RADIANCE_VERSION}/radiance-${RADIANCE_VERSION}-Linux.tar.gz" \
    && tar -xzf /tmp/radiance.tar.gz -C /usr/local \
    && rm /tmp/radiance.tar.gz

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
