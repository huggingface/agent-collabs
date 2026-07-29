FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
# GET /v1/watch.sh serves clients/collab_watch.sh off the filesystem, so the
# script has to ship in the image next to app/ (the route resolves it relative
# to the package, matching the local backend/clients/ layout).
COPY clients ./clients

EXPOSE 7860

# --workers 1 is REQUIRED, not a resource choice: the long-poll waiter registry
# (app/notify.py) is in-process, so a writer can only wake waiters parked on its
# own worker. With more workers, wakes would reach a fraction of watchers and
# every other `wait=` would silently time out — indistinguishable from a quiet
# board. Scaling this out needs a shared bus (Redis pubsub), not a bigger
# --workers; see WATCH_DESIGN.md §8.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860", "--workers", "1"]
