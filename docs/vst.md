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
`"patch"`, `"program"` or `"instrument"`) and `parameter_values` to find the patch index, then
pass it in `plugin_params` (e.g. `{"Part 1 Patch": 123}`). MODO DRUM/MODO BASS load their
default kit/model; MixBox's default chain passes audio cleanly, which makes it a safe first
effect to try.

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
