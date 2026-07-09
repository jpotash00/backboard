# Offboard Engine API. Builds the FastAPI service; the Python engine + SDK stay off npm/PyPI.
# See docs/DEPLOY.md for the full checklist (Redis, volumes, signing secrets, TLS).
FROM python:3.12-slim

WORKDIR /app

# Install deps first so the layer caches across code-only changes. `.[api,redis]` pulls the
# HTTP server and the optional shared-session backend, so the same image runs either the
# single-instance (in-memory) or multi-instance (Redis) topology purely by env.
COPY pyproject.toml README.md ./
COPY engine ./engine
COPY api ./api
COPY onboarding ./onboarding
COPY learning ./learning
COPY eval ./eval
RUN pip install --no-cache-dir ".[api,redis]"

# The append-only data asset (transcripts / resolutions / outcomes) must survive redeploys.
# Point it at a mounted volume; the default here assumes /data is mounted (see DEPLOY.md).
ENV OFFBOARD_RUNS_DIR=/data/runs
VOLUME ["/data"]

# The host sets $PORT; uvicorn binds it via offboard-api. 8000 is the local default.
ENV PORT=8000
EXPOSE 8000

# Non-root for a smaller blast radius.
RUN useradd --create-home --uid 10001 offboard && mkdir -p /data/runs && chown -R offboard /data
USER offboard

CMD ["offboard-api"]
