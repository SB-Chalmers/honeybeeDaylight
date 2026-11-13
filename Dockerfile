# One image for the API and both Celery workers (docker-compose.yml picks the command).

# EnergyPlus 24.2, without its example files and docs.
FROM nrel/energyplus:24.2.0 AS eplus
WORKDIR /EnergyPlus-24.2.0-94a887817b-Linux-Ubuntu22.04-x86_64
RUN rm -rf ExampleFiles WeatherData Documentation DataSets MacroDataSets

# EPSM wheels for /v1/energy-rc. epsm_rc has a Rust extension, so it is built here and
# only the wheels go into the final image.
FROM python:3.13.18-slim AS epsm
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
COPY requirements-epsm.txt .
# Only the EPSM wheels: their other dependencies install in the final image against its pins.
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels -r requirements-epsm.txt

FROM python:3.13.18-slim

# libvulkan1 lets Foton find a GPU, and libxext6 and libegl1 let it load the NVIDIA Vulkan
# driver (libGLX_nvidia) that the container toolkit mounts. libgomp1 and
# libx11-6 are needed by EnergyPlus.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libvulkan1 libgomp1 libx11-6 libxext6 libegl1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=eplus /EnergyPlus-24.2.0-94a887817b-Linux-Ubuntu22.04-x86_64 /opt/energyplus
ENV PATH="/opt/energyplus:${PATH}" \
    LD_LIBRARY_PATH=/opt/energyplus \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1
# Fail the build if EnergyPlus is missing a shared library.
RUN ! ldd /opt/energyplus/energyplus | grep 'not found'

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=epsm /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY api/ api/
COPY worker/ worker/
COPY simulation/ simulation/
COPY weather/ weather/
COPY grasshopper/ grasshopper/

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
