# Verified official Python 3.14 slim multi-platform index; update deliberately.
FROM python:3.14-slim@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
COPY pyproject.toml ./
COPY src/ ./src/
# Runtime requirements only: no dev extras, editable install, or host venv.
RUN /opt/venv/bin/pip install --no-cache-dir .

FROM python:3.14-slim@sha256:0514bc76712d64f8bb48adcb95b1ab0d88c69fee95d62fe4c18ee948bee9b1e1

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POSTER_BOT_DB_PATH=/app/data/poster_bot.sqlite3 \
    POSTER_BOT_LOCK_PATH=/app/data/poster_bot.lock

# ZoneInfo and the timezone picker need the full IANA database.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && install -d -m 0700 -o app -g app /app/data

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["python", "-m", "poster_bot"]
CMD ["run"]
