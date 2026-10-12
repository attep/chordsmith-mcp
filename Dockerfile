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

FROM python:3.12-slim AS vst

# The VST host is a separate process (GPL-3.0-or-later, host/ in this repo — see
# docs/architecture.md). JUCE is fetched at configure time; the tiny ToneSynth/GainFx test
# plugins are built here too and baked in as test fixtures (/opt/vst-test).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake git pkg-config \
        libx11-dev libxext-dev libxinerama-dev libxrandr-dev libxcursor-dev \
        libfreetype6-dev libfontconfig1-dev \
    && rm -rf /var/lib/apt/lists/*
COPY host /host
RUN cmake -S /host -B /host/build -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST_PLUGINS=ON \
    && cmake --build /host/build --parallel "$(nproc)" \
    && mkdir -p /fixtures \
    && cp -r /host/build/ToneSynth_artefacts/Release/VST3/ToneSynth.vst3 /fixtures/ \
    && cp -r /host/build/GainFx_artefacts/Release/VST3/GainFx.vst3 /fixtures/ \
    && /host/build/vst_host_artefacts/Release/vst_host --list --plugin /fixtures/ToneSynth.vst3 >/dev/null

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHORDSMITH_OUTPUT_DIR=/data \
    CHORDSMITH_STATE_DIR=/state \
    CHORDSMITH_SOUNDFONT=/usr/share/sounds/sf2/FluidR3_GM.sf2 \
    CHORDSMITH_SFZ_DIR=/sfz \
    CHORDSMITH_VST_DIR=/vst \
    CHORDSMITH_VST_TEST_DIR=/opt/vst-test

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN apt-get update \
    && apt-get install -y --no-install-recommends fluidsynth fluid-soundfont-gm ffmpeg libstdc++6 \
        libx11-6 libxext6 libxinerama1 libxrandr2 libxcursor1 libfreetype6 libfontconfig1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir ".[diffsinger]" \
    && useradd --create-home --uid 1000 chordsmith \
    && mkdir -p /data /state /sfz /vst && chown chordsmith /data /state
COPY --from=sfizz /usr/local/bin/sfizz_render /usr/local/bin/sfizz_render
RUN sfizz_render --help >/dev/null
COPY --from=vst /host/build/vst_host_artefacts/Release/vst_host /usr/local/bin/vst_host
COPY --from=vst /fixtures /opt/vst-test
RUN vst_host --list --plugin /opt/vst-test/ToneSynth.vst3 >/dev/null

USER chordsmith
VOLUME ["/data", "/state", "/sfz", "/vst"]
EXPOSE 8000
ENTRYPOINT ["chordsmith-mcp"]
