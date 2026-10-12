FROM python:3.12-slim AS sfizz

# sfizz_render is a separate process (never imported; BSD-2-Clause — see docs/architecture.md).
# The upstream project was archived in June 2026, so pin the last release. Not in Debian, so
# it is built from source here; the render client links everything statically.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates cmake git \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --branch 1.2.3 --recursive https://github.com/sfztools/sfizz.git /sfizz \
    && cmake -S /sfizz -B /sfizz/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_LTO=OFF \
        -DSFIZZ_JACK=OFF \
        -DSFIZZ_SHARED=OFF \
        -DSFIZZ_TESTS=OFF \
        -DSFIZZ_DEMOS=OFF \
        -DSFIZZ_DEVTOOLS=OFF \
    && cmake --build /sfizz/build --target sfizz_render -j"$(nproc)" \
    && cmake --install /sfizz/build \
    && sfizz_render --help >/dev/null

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHORDSMITH_OUTPUT_DIR=/data \
    CHORDSMITH_STATE_DIR=/state \
    CHORDSMITH_SOUNDFONT=/usr/share/sounds/sf2/FluidR3_GM.sf2 \
    CHORDSMITH_SFZ_DIR=/sfz

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN apt-get update \
    && apt-get install -y --no-install-recommends fluidsynth fluid-soundfont-gm ffmpeg libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ".[diffsinger]" \
    && useradd --create-home --uid 1000 chordsmith \
    && mkdir -p /data /state /sfz && chown chordsmith /data /state
COPY --from=sfizz /usr/local/bin/sfizz_render /usr/local/bin/sfizz_render
RUN sfizz_render --help >/dev/null

USER chordsmith
VOLUME ["/data", "/state", "/sfz"]
EXPOSE 8000
ENTRYPOINT ["chordsmith-mcp"]
