# Working with VST plugins — the flow

How to turn a MIDI part into a sound with a VST plugin: which endpoint to use, how to find a
plugin's controls, how to capture a sound that has no controls, and how to iterate. The
engine details live in [VST plugins](vst.md); this page is the workflow.

## Where the plugins run

| Endpoint | Loads | Does not load |
|---|---|---|
| **Docker / public server** (+ VST bridge) | FluidSynth GM, sfizz, this image's Linux `.vst3`s (Surge XT, Odin2, Cardinal, Vital, DecentSampler) **and** — through the bridge — every Windows plugin | — |
| **Windows-native server** | sfizz + all Windows `.vst3`s (MODO DRUM/BASS, TONEX, Guitar Rig, T-RackS, MixBox…) | no FluidSynth (GM parts render on the Docker server) |
| **Bridge worker** (`scripts/vst_bridge.py` on Windows) | Executes `vst_host` jobs (scans, renders, states, editor windows) and converts Guitar Pro/MusicXML tabs to MIDI (`--musescore`) for any server that sets `CHORDSMITH_VST_BRIDGE_URL` | — |

Routing is **per plugin path**: a Windows path (`C:\…\X.vst3`) goes to the bridge; a local path
(`/vst/X.vst3`) runs on the server itself. One song can use both; an effect chain may even mix
them (consecutive effects that share a host run in one pass).

## The decision path

1. **Find it** — `list_plugins` (optionally `name_contains`), then `detail` for its shape:
   programs, parameters, latency.
2. **Classify it**, and pick the branch:

| What `detail` shows | What it is | How to drive it |
|---|---|---|
| VST3 **programs** with real names | preset-based plugin | `plugin_preset: "Name"` (or index) |
| **Named parameters** (often hundreds; choices list names) | parameter-driven synth/effect | `plugin_params: {"Name": 0.5}` or `{"Choice": "Value"}` — numbers and choice names both work |
| Presets are placeholders ("no sound", "Knob 01"…) or a render comes out silent | GUI-only plugin (TONEX, Guitar Rig, SampleTank, Syntronik) | **Capture a state** (below), then `plugin_state` (instrument) / effect `state` |
| It processes audio (amp, EQ, tape, comp) | effect | `effects: [{"plugin": …}]` on any track's stem |

3. **Render** — `render_audio` with `stems: true` (required for vst/sfizz tracks) and
   `normalize_peak_db` for the mix; long songs go through `render_audio_job` + `get_render_job`.
   Balance with each spec's `gain_db` and check the per-stem `peak_db` in the result.

## Capturing a GUI sound (one time per tone/rig/patch)

```
open_plugin_state_editor(plugin="Guitar Rig 7", state_name="metal_rhythm")
```

A window opens **on the Windows desktop** (through the bridge) running the plugin's own UI.
Load the tone/rig/patch there, then close the window: the state is written to
`states/<state_name>.state` in the output folder, which both machines share. Reference it by
**name** or path:

```json
{"plugin": "C:/Program Files/Common Files/VST3/TONEX.vst3", "state": "tonex_metal"}
```

### The state library

- Every capture lands in the shared `states/` folder with a small metadata sidecar naming the
  plugin; `list_plugin_states` lists the whole library, and `list_plugins` with `detail` lists
  one plugin's states and its default.
- **Defaults**: capture with `make_default: true` (or call `set_plugin_default_state`) and a
  spec that names the plugin but gives **no** state uses it automatically — a TONEX track just
  works. `"plugin_state": "none"` opts a spec out; an explicit state always wins.
- The state **is** the sound: a new tone = a new capture (same name overwrites, or use a new
  name for a library of tones).
- `vst_host --editor --plugin X.vst3 --save-state out.state` does the same from a shell;
  `--dump-state` snapshots a parameter-driven plugin without a GUI.
- States apply **before** `preset`/`params`, so parameters can still tweak a captured sound.
- Linux plugins on the headless Docker server cannot be captured (no desktop); use their
  Windows builds through the bridge when a GUI pick is needed.

## Reverb on any track (no plugin needed)

Any stem can take a convolution reverb from the built-in chain — every engine, no plugin host:

```json
{"chain": {"reverb": {"decay_s": 2.5, "damping": 0.4, "wet": 0.18}}}
```

`decay_s` (0.1–8), `predelay_ms` (0–200) and `damping` (0 = bright, 1 = dark) are the same room
model the vocal-mix returns use; `wet` is the share mixed in (0 = dry). The tail rings past the
stem's last note, so the combined mix runs slightly longer (like the vocal returns).
FluidSynth tracks can also use `reverb_send`/`chorus_send` (engine effects); vst/sfizz tracks
cannot — use the chain reverb above or a VST effect with a dry/wet (e.g. Fame Studio Reverb,
CSR Hall's `Mix`).

## Recipes

**Drum kit** (MODO DRUM via the bridge):

```json
{"engine": "vst", "plugin": "C:/Program Files/Common Files/VST3/MODO DRUM.vst3"}
```
Write GM-style drum notes (36 kick, 38 snare, 42 hats, 49 crash). The default kit loads.

**Bass** (MODO BASS 2): `{"engine": "vst", "plugin": "…/MODO BASS 2.vst3"}`, notes at the
instrument's range (E1 = 28).

**Synth pad** (Surge XT, local Linux plugin, parameters by name):

```json
{"engine": "vst", "plugin": "/vst/Surge XT.vst3",
 "plugin_params": {"A Osc 1 Type": "Sine"}}
```
`list_plugins detail=/vst/Surge XT.vst3 parameters=… parameter_search="Osc 1 Type"` finds the
parameter; `parameter_values` lists its names.

**Rhythm guitar**: sfizz + captured amp:
```json
{"engine": "sfizz", "sfz": "/sfz/emilyguitar/emily_basic.sfz",
 "effects": [{"plugin": "…/TONEX.vst3", "state": "/data/states/tonex_metal.state"}],
 "gain_db": -4.0}
```

**Lead guitar**: the same shape with the lead capture (`guitar_rig_7_solo.state`).

**Effect dressing on another track** (drums through tape, via the bridge):
```json
"effects": [{"plugin": "…/Tape Machine 80 v6.vst3", "params": {"Tape Type": "456"}}]
```
Effect chains are applied to the rendered stem, in order, with latency compensated; a wrong
parameter *name* fails the render and lists the options.

## Worked examples

- `metal_demo.wav` — 32 bars: MODO DRUM, MODO BASS 2, TONEX rhythm, GR7 solo.
- `vst_flow_demo.wav` — one song with every branch at once:

| Track | Branch | Spec |
|---|---|---|
| Drums | instrument + effect | MODO DRUM (bridge) → Tape Machine 80 v6 (bridge), `Tape Type: "456"` |
| Bass | instrument | MODO BASS 2 (bridge) |
| Pad | params by name | Surge XT (local), `A Osc 1 Type: "Sine"` |
| Gtr Rhythm | captured state | sfizz → TONEX `tonex_metal.state` |
| Gtr Solo | captured state | sfizz → GR7 `guitar_rig_7_solo.state` |

## Pitfalls

- **Cold loads**: every VST operation spawns a fresh host that loads the plugin (~1–3 s each;
  MODO DRUM ~2.6 s). A multi-track song visits each plugin twice (validation, render).
- **The bridge runs jobs serially** — parallel stems queue behind it.
- **Placeholder presets mean the plugin needs a capture** (or is unlicensed — AmpliTube CS demo
  gear adds white-noise bursts; TR5 legacy modules render silence, the T-RackS **6** modules are
  fine).
- **Plugin paths are machine-specific**: a spec that names `C:\…` only renders where the bridge
  or the Windows server can see it. State files travel (shared folder), plugins do not.
- **GM parts need the Docker server** (no FluidSynth on Windows), VST parts need a host.
- Keep the bridge token private; treat it like a password.
