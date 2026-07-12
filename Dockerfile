# Lightweight web image for the job-hunter dashboard + pipeline.
# Comes up LIVE and auto-generates an ADMIN_TOKEN (printed on startup) that gates
# private reads and writes; add your keys, Sheet and profile in the browser at
# /settings. Set DEMO_MODE=1 to run the read-only public showcase instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEMO_MODE=0 \
    NAUKRI_SKIP_PLAYWRIGHT=1 \
    TRACKER_CSV=/data/tracker.csv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
# Install exactly the lockfile's core + web dependencies. The Playwright extra is
# deliberately not selected, keeping the image small and matching
# NAUKRI_SKIP_PLAYWRIGHT=1.
RUN uv sync --locked --no-install-project --extra web

COPY . .

# This only declares the mount point; it does not provision durable storage.
# Docker Compose and Fly mount /data explicitly. Render needs a separately
# configured paid persistent disk; its free filesystem remains ephemeral.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
