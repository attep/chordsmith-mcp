FROM python:3.12-slim

# Build environment for the ChordSmith VST host (host/) — used by CI and local iteration.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cmake git pkg-config \
        libx11-dev libxext-dev libxinerama-dev libxrandr-dev libxcursor-dev \
        libfreetype6-dev libfontconfig1-dev \
    && rm -rf /var/lib/apt/lists/*
