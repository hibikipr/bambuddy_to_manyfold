FROM python:3.15-slim

# The base image's own OS packages lag Debian's security patch cadence —
# util-linux (util-linux/mount/login/libuuid1/libsmartcols1/libmount1/
# liblastlog2-2/libblkid1/bsdutils, all built from the same source package)
# was flagged by CI's Trivy scan at 2.41-5 with several CVEs fixed in
# 2.41.3-1/2.41.5-0+deb13u1. Upgrading here (rather than waiting on the next
# python:3.14-slim rebuild) keeps every build current regardless of how
# stale the upstream base image's own OS layer is.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Set at build time from the release git tag (see .github/workflows/docker-publish.yml);
# defaults to "dev" for a plain local `docker build` with no --build-arg.
ARG VERSION=dev

WORKDIR /app

# pip vendors its own copies of urllib3 and msgpack, and Trivy reports CVEs against them. The base image's pip lags:
# 26.1.2 vendored urllib3 2.7.0 and msgpack 1.1.2, both with fixed releases. Upgrading pip first picks up the fixed
# vendored copies on every build (26.3: urllib3 2.8.0, msgpack 1.2.4). Unpinned on purpose, like the apt upgrade above.
# hadolint ignore=DL3013
RUN pip install --no-cache-dir --upgrade pip

# Deps first for layer caching. gunicorn serves the Flask app in production.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# App code — the CLI engine is imported unmodified by the web app and by
# bambuddy_to_manyfold.py's own __main__ block if you exec into the container.
COPY bambuddy_to_manyfold.py bambuddy_manyfold_web.py i18n.py ./
COPY templates ./templates
COPY static ./static

# Sync-state + web config live on a mounted volume so they survive restarts.
ENV APP_VERSION=$VERSION \
    SYNC_STATE_FILE=/data/bambuddy_sync_state.json \
    WEB_CONFIG_FILE=/data/bambuddy_manyfold_web_config.json \
    HOST=0.0.0.0 \
    PORT=8089
VOLUME ["/data"]
EXPOSE 8089

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8089/',timeout=4).status==200 else 1)"]

# One worker (single in-flight sync job, enforced by an in-process lock) +
# threads for SSE connections held open for a sync's full multi-minute
# duration (Manyfold's rate limit can itself sleep for minutes per retry) plus
# normal request handling. --timeout is generous for the same reason: a
# single blocking request can legitimately run far longer than a typical API
# call. --access-logfile - sends HTTP access logs to stdout for docker logs.
CMD ["gunicorn", "-b", "0.0.0.0:8089", "-w", "1", "--threads", "12", "--timeout", "300", "--access-logfile", "-", "bambuddy_manyfold_web:app"]
