# ChordSmith VST host

`vst_host` renders VST3 plugins offline as a **separate process**, so ChordSmith (MIT) never
links or imports a plugin or a plugin host. It is the third engine beside FluidSynth and
sfizz: `set_track_instrument` `engine: "vst"` renders a track through a `.vst3` instrument,
and a spec's `effects` chain runs the rendered stem through effect plugins (with the reported
latency compensated).

**Licence: GPL-3.0-or-later** (see `LICENSE`). The rest of the repository is MIT; this
directory is a standalone program that the server only spawns and reads a wav from.

## Build

Needs CMake 3.22+, a C++17 compiler and git (JUCE is fetched at configure time).

```bash
cmake -S host -B host/build -DCMAKE_BUILD_TYPE=Release
cmake --build host/build --parallel
# with the tiny test plugins used by the test suite:
cmake -S host -B host/build -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST_PLUGINS=ON
cmake --build host/build --parallel
python host/tests/smoke.py host/build
```

Windows binaries are built by CI (`.github/workflows/ci.yml`, job `vst-host`) and attached to
releases; the Docker image builds the Linux binary. Point ChordSmith at the binary with
`CHORDSMITH_VST_HOST` or put `vst_host` on `PATH`.

## Usage

```
vst_host --list --plugin X.vst3
vst_host --params --plugin X.vst3 [--param-values INDEX] [--param-limit N]
vst_host --render --plugin X.vst3 [--preset NAME|INDEX] [--set INDEX=VALUE]... [--tail SECONDS] --midi in.mid --wav out.wav
vst_host --process [--plugin X.vst3 [--preset NAME|INDEX] [--set INDEX=VALUE]...]... --in in.wav --out out.wav
```

Every mode prints one JSON line on stdout (`{"ok": true, ...}` or `{"ok": false, "error": ...}`)
and exits non-zero on failure. Audio is 44.1 kHz stereo; `--render` renders the MIDI file plus
`--tail` seconds (default 2.0) so ChordSmith can trim and pad the stem to the song's length.

`--list` loads the plugin and reports each type's vendor, category, whether it is an
instrument, its programs (the values `plugin_preset` takes), parameter count and latency.

`--params` lists the plugin's parameters (index, name, steps, current value and text);
`--param-values INDEX` dumps a discrete parameter's value strings (a patch list, for example).
`--set INDEX=VALUE` applies a parameter before rendering — for discrete parameters VALUE is the
plain index (see `--param-values`), for toggles 0/1, for continuous parameters the 0-1 value.
Plugins that expose no VST3 programs (IK's SampleTank, Syntronik) pick patches through such
parameters.

## Notes and limits

- `--render` runs in non-realtime mode and waits `--load-wait` seconds (default 15) before the
  first block, so sample-based plugins (Kontakt libraries and friends) finish loading their
  patch — without it, faster-than-realtime renders started before samples were ready (whole
  instruments silent, or lines coming in late). Raise it for huge patches, lower it when only
  quick synths render.
- VST3 only (VST2 is not supported); plugins are user-supplied and never bundled.
- Presets are the plugin's VST3 programs; `.vstpreset` state files are not supported yet.
- Plugins run headless: ones that insist on a GUI may fail to instantiate, and that error is
  reported instead of crashing the server.
- Renders are not guaranteed bit-identical (plugins may be non-deterministic).
