# Musha on Cloud Run: one image serves the API and the built web app.
# Stage 1 builds frontend/ with Node. Stage 2 runs the FastAPI backend on Python 3.12, and
# Litestream keeps the SQLite file replicated to Cloud Storage (deploy/entrypoint.sh).
# Build context: the repo root. deploy/deploy.ps1 builds it with Cloud Build.

FROM node:24.18.0-bookworm-slim AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM python:3.12.13-slim-bookworm

ARG LITESTREAM_VERSION=0.5.17
# sha256 of litestream-0.5.17-linux-x86_64.tar.gz, from the release's checksums
ARG LITESTREAM_SHA256=cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Litestream, checked against its published checksum before it is installed.
RUN python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/litestream.tar.gz')" \
        "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-x86_64.tar.gz" \
 && echo "${LITESTREAM_SHA256}  /tmp/litestream.tar.gz" | sha256sum -c - \
 && mkdir /tmp/litestream \
 && tar -xzf /tmp/litestream.tar.gz -C /tmp/litestream \
 && install -m 0755 "$(find /tmp/litestream -type f -name litestream | head -n 1)" /usr/local/bin/litestream \
 && rm -rf /tmp/litestream /tmp/litestream.tar.gz \
 && litestream version

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt \
 && pip install --no-deps paypal-agent-toolkit==1.11.0

COPY backend/ backend/
COPY --from=web /src/frontend/dist frontend/dist
COPY deploy/litestream.yml /etc/litestream.yml
COPY deploy/entrypoint.sh /usr/local/bin/musha-entrypoint

# A checkout on Windows may carry CRLF line endings; the shell and Litestream read LF.
RUN sed -i 's/\r$//' /usr/local/bin/musha-entrypoint /etc/litestream.yml \
 && chmod 0755 /usr/local/bin/musha-entrypoint \
 && useradd --uid 10001 --no-create-home --home-dir /data musha \
 && mkdir -p /data /replica \
 && chown musha:musha /data /replica

# The database lives in /data. On Cloud Run that disk is memory, and Litestream keeps the copy
# that outlives the instance in Cloud Storage. /replica takes a file replica for a laptop run.
# A Postgres DATABASE_URL leaves all of it aside.
ENV PORT=8080 \
    MUSHA_APP_DIR=/app/backend \
    MUSHA_DB_PATH=/data/musha.db \
    LITESTREAM_CONFIG=/etc/litestream.yml \
    LITESTREAM_SOCKET=/tmp/litestream.sock

USER musha
WORKDIR /app/backend
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/musha-entrypoint"]
