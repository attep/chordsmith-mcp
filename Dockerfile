FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHORDSMITH_OUTPUT_DIR=/data \
    CHORDSMITH_STATE_DIR=/state \
    CHORDSMITH_SOUNDFONT=/usr/share/sounds/sf2/FluidR3_GM.sf2

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN apt-get update \
    && apt-get install -y --no-install-recommends fluidsynth fluid-soundfont-gm ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 chordsmith \
    && mkdir -p /data /state && chown chordsmith /data /state

USER chordsmith
VOLUME ["/data", "/state"]
EXPOSE 8000
ENTRYPOINT ["chordsmith-mcp"]
