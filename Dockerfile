FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHORDSMITH_OUTPUT_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 chordsmith \
    && mkdir -p /data && chown chordsmith /data

USER chordsmith
VOLUME ["/data"]
ENTRYPOINT ["chordsmith-mcp"]
