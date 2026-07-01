FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching. gunicorn serves the Flask app in production.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# App code — the CLI engine is imported unmodified by the web app and by
# bambuddy_to_manyfold.py's own __main__ block if you exec into the container.
COPY bambuddy_to_manyfold.py bambuddy_manyfold_web.py ./
COPY templates ./templates
COPY static ./static

# Sync-state + web config live on a mounted volume so they survive restarts.
ENV SYNC_STATE_FILE=/data/bambuddy_sync_state.json \
    WEB_CONFIG_FILE=/data/bambuddy_manyfold_web_config.json \
    HOST=0.0.0.0 \
    PORT=8089
VOLUME ["/data"]
EXPOSE 8089

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8089/',timeout=4).status==200 else 1)"

# One worker (single in-flight sync job, enforced by an in-process lock) +
# threads for SSE connections held open for a sync's full multi-minute
# duration (Manyfold's rate limit can itself sleep for minutes per retry) plus
# normal request handling. --timeout is generous for the same reason: a
# single blocking request can legitimately run far longer than a typical API
# call. --access-logfile - sends HTTP access logs to stdout for docker logs.
CMD ["gunicorn", "-b", "0.0.0.0:8089", "-w", "1", "--threads", "12", "--timeout", "300", "--access-logfile", "-", "bambuddy_manyfold_web:app"]
