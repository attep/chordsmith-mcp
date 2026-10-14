# VST plugins

ChordSmith renders `.vst3` plugins offline through a small **separate process** (`vst_host`,
GPL-3.0-or-later, sources under `host/`). It is the third engine beside FluidSynth and sfizz:

- `set_track_instrument` with `engine: "vst"` + `plugin` renders a track through a VST3
  **instrument** (optionally `plugin_preset` — a program name or index).
- A spec's `effects` chain (`[{"plugin": "…"}, …]`) runs a track's **rendered stem** through
  VST3 **effect** plugins, in order; the host compensates each plugin's reported latency, so
  stems stay aligned with the mix.

Plugins are **user-supplied and never bundled**. `list_plugins` shows what the server can see;
`list_plugins` with `detail` loads one plugin and reports its programs, category and latency.
[Working with VST plugins](vst-workflow.md) is the end-to-end flow: picking an endpoint,
classifying a plugin, capturing GUI-only sounds, and recipes per part.

## Windows servers (IK Multimedia and other Windows VST3s)

Most commercial plugins are Windows/macOS-only, so VST rendering is meant for a ChordSmith
server running **natively on Windows**:

1. Download `vst-host-windows-x64.zip` from the latest
   [release](https://github.com/attep/chordsmith-mcp/releases) and extract it (it contains
   `vst_host.exe` and the tiny ToneSynth/GainFx test plugins).
2. Point ChordSmith at it:
   - `CHORDSMITH_VST_HOST` = the full path to `vst_host.exe`
   - `CHORDSMITH_VST_DIR` = the folder to scan for `.vst3` plugins, e.g.
     `C:\Program Files\Common Files\VST3` (or a curated subfolder)
3. Run ChordSmith natively (`uvx --from git+https://github.com/attep/chordsmith-mcp
   chordsmith-mcp` or `chordsmith-mcp` from a checkout) and call `list_plugins`.

IK Multimedia plugins (MODO DRUM, MODO BASS, SampleTank, Syntronik, MixBox, T-RackS) must be
authorized once in IK Product Manager; after that they load headless in the host. Plugins with
VST3 programs (many synths) are picked with `plugin_preset`; the IK synths expose their patch
selectors as **parameters** instead — use `list_plugins` with `parameter_search` (try
`"patch"`, `"program"` or `"instrument"`) and `parameter_values` to find the values, then pass
one in `plugin_params`, by name or index (e.g. `{"Part 1 Patch": "Full Grand"}` or
`{"Part 1 Patch": 123}`). MODO DRUM/MODO BASS load their default kit/model; MixBox's default
chain passes audio cleanly, which makes it a safe first effect to try.

### Parameter values by name

`plugin_params` (and an effect's `params`) values are either numbers — a **plain index** for
discrete parameters, 0–1 for continuous ones — or **value names**, which the host resolves
against the plugin itself: `{"A Osc 1 Type": "Classic"}`. Many VST3 plugins expose their choice
parameters as continuous values with no discrete steps (`steps: 0`), so names are the reliable
way in: `list_plugins` with `parameter_values: <index>` scans such a parameter and lists the
distinct texts it produces (stepped and toggle parameters list theirs directly). A name that
matches nothing **fails the render** with the available texts in the error, rather than
silently rendering the wrong sound.

### Capturing a plugin's state (TONEX, Guitar Rig, SampleTank)

GUI-only plugins keep their sound in the plugin's **state**, not in its parameters: a TONEX tone,
a Guitar Rig rig, a SampleTank/Syntronik patch, an AmpliTube preset. Their standalone apps do
**not** prime the plugin — it boots with its own empty state — so capture it once on a desktop,
then render headlessly:

1. `open_plugin_state_editor` (`plugin`: a path or a name from `list_plugins`) opens the plugin's
   own UI in a window on the server's desktop and returns the path the state will be saved to.
   From a shell: `vst_host --editor --plugin X.vst3 --save-state out.state` (add `--state FILE`
   to start from an earlier capture).
2. Load the tone/rig/patch in that window, then close it — the state is written to the file.
3. Reference it from a vst track's `plugin_state`, or an effect's `state`. It is applied before
   `preset`/`plugin_params`, so parameters can still tweak the captured sound. Renders load it
   with `vst_host --render/--process --state FILE` — no GUI needed on any server that can read
   the file. `--dump-state FILE` writes a state without a GUI (parameter-driven plugins, tests).

The editor needs a desktop session, so the Linux Docker server refuses it; capture on the
Windows/native server and the state file renders anywhere.

### One endpoint, two machines (remote VST bridge)

A server that has no Windows plugins can still use them: run the bridge worker on the Windows
machine and point the server at it.

1. On Windows (the machine with the plugins):
   `python scripts/vst_bridge.py --token <secret>` — it runs `vst_host` jobs on request (port
   18002 by default; `--host-exe`/`--port`/`--bind` to change). Every request needs the token,
   so treat the secret like a password, and let the port through the firewall for Docker.
2. On the server (e.g. the Docker container), set:
   - `CHORDSMITH_VST_BRIDGE_URL` = `http://host.docker.internal:18002`
   - `CHORDSMITH_VST_BRIDGE_TOKEN` = the same secret
   - `CHORDSMITH_VST_BRIDGE_VST_DIR` = `C:\Program Files\Common Files\VST3` (what
     `list_plugins` scans)
   - `CHORDSMITH_VST_BRIDGE_SHARED_FROM` / `CHORDSMITH_VST_BRIDGE_SHARED_TO` = the folder both
     machines see, e.g. `/data` and `D:\ChordSmith\data` — editor captures land there so the
     server can read them
   - `CHORDSMITH_VST_LOAD_WAIT` = optional; seconds to let big libraries (e.g. Kontakt) load
     samples before a render is trusted. Set it on the server (sent with each render) and/or on
     the bridge itself — it reaches `vst_host` as `--load-wait` either way.
3. `list_plugins` now lists the bridge's plugins, vst/sfizz renders route their VST work to the
   bridge, and `open_plugin_state_editor` opens the window **on the bridge's desktop** with the
   saved state appearing in the shared folder.

The bridge executes whatever `vst_host` job the server sends — anyone with the token can too.
GM/FluidSynth and the vocals stay on the server that runs them, and the plugin paths are machine
specific: specs that name Windows plugins only render through a bridge (or on the Windows
server).

The Linux Docker server does **not** load Windows plugins (Wine/yabridge is not supported): it
uses Linux VST3s only (see below). The Docker image still includes `vst_host` and the test
plugins, so the VST tooling can be tried there with Linux plugins.

## Docker servers (Linux VST3s)

Mount Linux `.vst3` bundles read-only and they are picked up by `list_plugins`:

```yaml
    volumes:
      - ./vst:/vst:ro        # CHORDSMITH_VST_DIR=/vst is set by the image
```

Free Linux VST3 plugins that work headless and are worth trying:

| Plugin | What it is | Licence / notes |
|---|---|---|
| [Surge XT](https://surge-synthesizer.github.io/) (+ Surge XT Effects) | hybrid synth and a full effect rack | GPL-3.0, Windows/macOS/Linux |
| [Vital](https://vital.audio/) | wavetable synth (free tier) | free tier; Ubuntu 18.04+ |
| [sfizz-ui](https://github.com/sfztools/sfizz-ui) | SFZ sample player as VST3/LV2 | BSD-2-Clause (archived upstream) |
| [Cardinal](https://github.com/DISTRHO/Cardinal) | modular synth that can host other plugins | GPL-3.0 |
| [Odin 2](https://www.thewavewarden.com/odin2) | analogue-modelled synth | GPL-3.0 |
| [CHOW Tape Model](https://github.com/jatinchowdhury18/CHOWTapeModel) | tape saturation/distortion | open source |
| [Decent Sampler](https://www.decentsamples.com/product/decent-sampler-plugin/) | sample player (.dspreset libraries) | free; Windows/macOS/Linux |

Free **Windows** additions to the IK suite: Surge XT, Vital, Spitfire LABS, Decent Sampler,
TDR Nova, Valhalla Supermassive, Kilohearts Essentials, Analog Obsession, MeldaProduction's
free bundle — all VST3, install them into the standard VST3 folder and they appear in
`list_plugins`.

### Verified installs (Linux / Docker)

Downloaded, extracted into `./vst` and verified on a live server with `list_plugins`:

| Plugin | Asset | Extract into `./vst` |
|---|---|---|
| Surge XT (+ Surge XT Effects) | `surge-xt-linux-1.3.4-pluginsonly.tar.gz` (GPL-3.0) | the `Surge XT.vst3` and `Surge XT Effects.vst3` bundles |
| Odin 2 | `Odin2.4.1Linux.zip` (GPL-3.0) | the `Odin2.vst3` bundle |
| Cardinal | `Cardinal-linux-x86_64-26.02.tar.gz` (GPL-3.0) | the `Cardinal.vst3` bundle |

Vital's free download is account-gated (`account.vital.audio`), so it is not scripted here;
CHOW Tape Model had no release assets at the time of writing.

## Guitar through an amp plugin (TONEX, AmpliTube, ...)

Amp/cab plugins are **effects** — they process audio and cannot take MIDI. The pattern is a
sampled guitar rendered from MIDI plus the amp as an insert on that track:

1. A sampled guitar instrument: mount an SFZ library (e.g. the CC0 *Emilyguitar* —
   `emily_basic.sfz`, playable A1–F6) and give the track
   `{"engine": "sfizz", "sfz": "<lib>/emily_basic.sfz", "chain": {"eq": {"highpass_hz": 70}}}`.
   Write power chords as dyads; palm mutes are short low-velocity notes, ringing chords long ones.
2. The amp in the same track's `effects` chain, e.g.
   `{"plugin": "C:\\Program Files\\Common Files\\VST3\\AmpliTube 5.vst3"}` — the host compensates
   the plugin's reported latency, and the stem carries the amp sound.
3. Render with `stems: true` on a **Windows** server: IK amp plugins are Windows-only, so the
   Linux Docker server cannot run them (the sampled guitar itself works on both).

**TONEX heads-up:** it loads and reports as an effect, but its VST3 exposes no programs and only
placeholder parameters ("Knob 01"…), and its default plugin state has an empty tone slot — so
headless it renders **silence** while its Bypass toggle passes the dry signal. Loading a tone in
the TONEX **standalone** does *not* help: the plugin keeps its own state. The fix is the state
capture above: `open_plugin_state_editor` on TONEX, load a licensed tone in its UI, close the
window, and reference the saved file as the effect's `state` — a 53 KB state renders the tone
headlessly (verified: crest 20.1 → 15.9 dB on a test riff). Guitar Rig 7 behaves the same way
(its standalone cannot prime the plugin; a captured rig state renders). AmpliTube's demo gear
(unlicensed models in AmpliTube CS) adds periodic white-noise bursts to the render; licensed gear
is clean, and MixBox's default chain passes audio cleanly out of the box. For an amp-ish tone
with no plugin at all, a built-in `chain` (`{"eq": {"highpass_hz": 80, "bells": [...]},
"saturation": {"enabled": true, ...}}`) gives a driven-DI sound that always works.

## Caveats

- **VST3 only.** The older VST2 `.dll`s are ignored (the format is discontinued and cannot be
  redistributed).
- Presets are the plugin's **VST3 programs**; `.vstpreset` state files are not supported yet.
- Renders are not guaranteed byte-identical — plugins may be non-deterministic. The
  byte-identical default only applies when no VST is configured.
- Plugins run **headless**: a plugin that insists on opening a GUI may fail to load, and the
  error is reported instead of crashing the server.
- A plugin is native code running with the server's privileges; only point
  `CHORDSMITH_VST_DIR` at plugins you trust (in Docker, the container is the sandbox).
