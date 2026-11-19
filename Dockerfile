FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/src \
    HWCLOUD_DIRECTORY=/photos \
    HWCLOUD_CONFIG_DIR=/config \
    HWCLOUD_FOLDER_STRUCTURE="{:%Y/%m/%d}" \
    HWCLOUD_UNTIL_FOUND=50 \
    HWCLOUD_KEEPALIVE_INTERVAL=60 \
    HWCLOUD_DOWNLOAD_CONCURRENCY=2

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN printf '#!/bin/sh\nexec python3 -m hwcloudpd "$@"\n' > /usr/local/bin/hwcloudpd \
    && chmod +x /usr/local/bin/hwcloudpd \
    && mkdir -p /config /photos \
    && chmod 0755 /config /photos

# Run as any UID/GID (compose `user: "${PUID}:${PGID}"`) - nothing in the image needs root.
VOLUME ["/config", "/photos"]

# In one-shot mode (no CRON_SCHEDULE / HWCLOUD_WATCH_INTERVAL) the healthcheck always passes.
# In daemon mode it fails when the loop is stale, the Huawei session is invalid, or a single
# sync runs longer than HWCLOUD_MAX_SYNC_HOURS.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD ["hwcloudpd", "--healthcheck"]

ENTRYPOINT ["hwcloudpd"]
