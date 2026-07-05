# Lightweight web image for the job-hunter dashboard + pipeline.
# Comes up LIVE and auto-generates an ADMIN_TOKEN (printed on startup) that gates
# writes; add your keys, Sheet and profile in the browser at /settings — no file
# editing. Set DEMO_MODE=1 to run the read-only public showcase instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEMO_MODE=0 \
    NAUKRI_SKIP_PLAYWRIGHT=1 \
    TRACKER_CSV=/data/tracker.csv

WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY . .

# Persist the tracker outside the image layer (mount a volume at /data).
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
