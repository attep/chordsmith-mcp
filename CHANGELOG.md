# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Planned: rate limits and quotas (bars, tracks, file size, storage, TTL), genre presets
(jazz/pop/bossa/trap), bass-line and drum-pattern helpers, section-based song building, MusicXML
export, Roman-numeral analysis, an `explain_progression` tool, and contributor hygiene
(CONTRIBUTING, SECURITY, pre-commit, mypy).

## [0.24.7] - 2026-10-13

### Added

- vst tracks take **gain-only section overrides**, like sfizz: `sections: {"Drop 1": {"gain_db": 6}}`
  shapes a plugin layer's level per section, applied to the rendered stem, so plugin basses can
  follow a song's drops. Program/bank/preset overrides stay fluidsynth-only and are rejected with
  a clear message.

## [0.24.6] - 2026-10-13

### Fixed

- `list_plugins` with `detail` (and its `parameters`/`parameter_values` dumps) now runs against
  the plugin's own host: local Linux plugins are described by this server, Windows plugins by
  the bridge. Before, a bridged server sent local plugins to the bridge and got a bogus
  "does not exist".

## [0.24.5] - 2026-10-13

Both plugin worlds in one session: VST routing is per plugin, not per server.

### Fixed

- With a VST bridge configured, this server's own plugins still work: existence checks, plugin
  scans, renders and effect chains pick the host **per plugin path** — Windows paths go to the
  bridge, local paths to the server's own `vst_host`. `list_plugins` now scans both folders and
  reports them together (e.g. `/vst + C:\Program Files\Common Files\VST3`), and an effect chain
  may mix both worlds (consecutive effects that share a host run in one pass, the stem moves
  between passes).

## [0.24.4] - 2026-10-13

One endpoint for everything: the remote VST bridge.

### Added

- `scripts/vst_bridge.py` — a small token-protected worker that runs `vst_host` jobs (plugin
  scans, renders, state loads/saves, editor windows) for a server on another machine.
- **Remote VST bridge support**: with `CHORDSMITH_VST_BRIDGE_URL` (plus `_TOKEN`, `_VST_DIR`,
  `_SHARED_FROM`/`_SHARED_TO`), a server without Windows plugins lists and renders them anyway —
  `list_plugins`, vst/sfizz renders and `open_plugin_state_editor` route to the bridge, upload
  job inputs and write outputs back, while GM/FluidSynth and the vocals stay local. One MCP
  endpoint can therefore serve MODO DRUM, TONEX, Guitar Rig and T-RackS from a Linux box.
  Verified live: the public Docker server rendered the metal demo's MODO DRUM and MODO BASS 2
  stems through the Windows bridge.

### Changed

- Windows-style plugin paths get clean file names in labels and error messages when handled on
  a Linux server.

## [0.24.3] - 2026-10-12

Plugin state capture: GUI-only VST3s become usable in headless renders.

### Added

- **`open_plugin_state_editor`** — opens a plugin's own UI in a window on the server's desktop
  and returns immediately with the path its state will be written to; closing the window saves
  the plugin state there. The host command behind it is
  `vst_host --editor --plugin X.vst3 --save-state out.state`; `vst_host --dump-state FILE`
  writes the same snapshot without a GUI (parameter-driven plugins, tests).
- `set_track_instrument` specs gain **`plugin_state`** and VST effects gain **`state`**: renders
  load the captured state before presets/parameters — a TONEX tone, a Guitar Rig rig or a
  SampleTank/Syntronik patch renders headlessly from one capture. The host side is
  `--state FILE` on `--render`/`--process`/`--editor`.
- ToneSynth's test plugin now saves/restores its parameters, so the host smoke tests prove a
  full state round trip (`--dump-state` then `--state` reproduces the parameter-set render).

### Notes

- The editor needs a desktop session, so the tool refuses on the headless Linux Docker server;
  capture on a native/desktop server and the state file renders anywhere. GUI-only plugins
  (TONEX, Guitar Rig, SampleTank, Syntronik) ignore their standalone apps when hosted — this
  capture is the only way their sound reaches a render. Tool count is 36; the budgets stand.

## [0.24.2] - 2026-10-12

VST parameters by name: choice parameters that VST3 exposes as continuous values are settable now.

### Added

- `plugin_params` and a VST effect's `params` accept **value names** beside numbers, e.g.
  `{"A Osc 1 Type": "Classic"}` — the host resolves a name against the plugin (its own
  text-to-value mapping, then a scan of its states) and **fails the render with the available
  names** when nothing matches, instead of silently skipping the setting.
- `list_plugins` gains `name_contains` to filter the plugin scan (case-insensitive).
- The host's `--param-values` scans continuous parameters and lists the distinct texts they
  produce, so choice parameters with no discrete steps (all of Surge XT's, for example) yield a
  usable value list; toggles list their two states.
- A "Wave" choice parameter on the ToneSynth test plugin exercises the text-to-value path in the
  host smoke tests, and `scripts/run-windows-server.ps1` starts the native Windows server with
  the local VST3/SFZ paths.

### Fixed

- `list_plugins` no longer lists a Windows VST3 bundle and the `.vst3` file inside it as two
  plugins.

### Documentation

- `docs/vst.md`: the guitar-through-an-amp recipe records what was verified live — TONEX renders
  silence headless (the standalone's tone is **not** inherited by the plugin), AmpliTube CS demo
  gear mixes in white-noise bursts, and a built-in chain always gives a driven-DI tone.

## [0.24.0] - 2026-10-12

Audible lows and long mixes: layered tracks, built-in instrument chains, and background mix jobs.

### Added

- **Track layering**: `add_track` gains `copy_from` + `transpose_semitones` — copy an existing
  track's notes (durations paired, velocities kept) and shift them, e.g. an octave-up sub in
  one call; swing/humanize still apply.
- **Built-in instrument chains**: `set_track_instrument` specs gain `chain` — the vocal-chain
  stages (EQ → de-esser → saturation → compressor) applied to any track's rendered stem, before
  VST effects. Instrument grit/EQ/glue with no plugin host; the EQ high-pass now reaches
  2000 Hz for effect layers.
- **Background mix jobs**: `mix_song_with_vocals_job` + `get_mix_job` + `list_mix_jobs` — the
  full mix pipeline on a worker thread with per-track progress (each backing track, then the
  mix phase), restart-safe records in `mix_jobs.json`, 24-hour TTL. The synchronous
  `mix_song_with_vocals` stays for short mixes.
- `docs/tools.md` documents a layered-sub recipe (sine + octave layer + saturated grit layer)
  that survives phone speakers.

### Changed

- Budgets raised deliberately: payload 164 → 232 KB, descriptions 20 → 22 KB — the mix job
  tool duplicates the mix schema by design, matching the render job pattern.

## [0.23.0] - 2026-10-12

The VST engine: a third renderer (a separate GPL process) for `.vst3` instruments, per-track
VST effect chains, and `list_plugins` — built for both Windows and Linux servers.

### Added

- **A third render engine: VST** (`set_track_instrument` `engine: "vst"` + `plugin`): `.vst3`
  instruments render as stems at 44.1 kHz through a separate-process host (`host/`,
  GPL-3.0-or-later, JUCE; never linked into the server). Presets are the plugin's VST3
  programs (`plugin_preset`); the host is built for Linux (in the Docker image) and Windows
  (release artifacts from CI) and is found via `CHORDSMITH_VST_HOST` or `PATH`. VST3 only;
  plugins are user-supplied and never bundled; vst tracks need `stems: true` renders and are
  labelled `source: "vst"` in `effective`.
- **Per-track VST effect chains** (`effects` on any spec): effect plugins run over the rendered
  stem, in order, with the plugins' reported latency compensated, so stems stay aligned.
- **`plugin_params`**: parameter values for vst instruments and effects — how plugins without
  VST3 programs pick a patch (IK's SampleTank and Syntronik expose their patch selectors as
  parameters). `list_plugins` gains `parameters`, `parameter_search` and `parameter_values` to
  discover names and value indexes; the host gains `--params`, `--param-values` and `--set`.
- **`list_plugins`**: the `.vst3` plugins under `CHORDSMITH_VST_DIR` (Docker: `/vst`), with
  `detail` loading one plugin for vendor, category, programs, parameters and latency.
- `docs/vst.md`: Windows (IK suite) and Docker (Linux VST3) setup, free plugin suggestions and
  caveats; the Docker image mounts `/vst`; `bake_instruments` lists a vst track's plugin as
  companion metadata.

### Fixed

- Stem renders are written to a scratch file and copied into the output folder only when the
  stem is finished, so a stem never appears as a 0-byte file in listings or download links
  while a slow engine (a big sample library, say) is still working.

## [0.22.1] - 2026-10-12

From the hip-hop demo: sfizz stems are padded to the song's own length, and the docs say where
sustain comes from in an `.sfz`.

### Fixed

- sfizz stems are trimmed and then padded to the MIDI file's length, so every stem lasts as long
  as the song — `sfizz_render` stops at the last MIDI event, so a melody track that ends early
  used to leave a short stem.

### Changed

- `docs/tools.md` explains that sustain comes from the `.sfz` instrument itself (loop points,
  `ampeg_release`): a sample with no loop plays once per note and then falls silent.

## [0.22.0] - 2026-10-12

Swing/humanize on `add_track` and a second render engine (sfizz) for sampled instruments.

### Added

- `add_track` gains `swing` and `humanize`: notes on offbeat steps can be delayed by a fraction
  of `step_beats` (0.33 = triplet feel), and the chord tools' seeded `humanize`
  (timing/velocity) now applies to added tracks after `loop` tiling — repeated loops breathe
  while a fixed seed stays byte-reproducible.
- **A second render engine: sfizz** (`set_track_instrument` `engine: "sfizz"` + `sfz`): sampled
  `.sfz` instruments render as stems at 44.1 kHz through the `sfizz_render` CLI (a separate
  process, never imported; BSD-2-Clause). User-supplied `.sfz` libraries are never bundled — the
  Docker image mounts `CHORDSMITH_SFZ_DIR` (`/sfz`) and `list_instruments` lists what is there.
  sfizz tracks reject FluidSynth-only options (preset/bank/program/sends), allow section
  `gain_db` overrides, are labelled `source: "sfz"` in `effective` (null bank/program), ship
  their `.sfz` path in `bake_instruments` metadata, and single-pass renders refuse files that
  use them (stems render each track's own engine).

## [0.21.1] - 2026-10-12

Agent skill and handoff guidance: the orchestra tryout's gap list turned into the skill, plus the
rules agents kept rediscovering written into the tool descriptions and docs.

### Added

- An agent skill for Copilot and other skill-aware agents
  ([`skills/chordsmith/SKILL.md`](skills/chordsmith/SKILL.md)): the two golden workflows
  (instrumental and sung), the tool map, and the conventions that matter (levels, guide tracks,
  sections, soft controls, licences). Installable with
  `gh skill install attep/chordsmith-mcp chordsmith`.

### Changed

- The copy tools (`add_track`, `remove_track`, `transpose_midi`, `bake_instruments`) say in their
  descriptions and docs that `output_filename` must differ from the source, even with
  `overwrite: true`.
- `export_vocal_song` points to `bake_instruments` for a DAW-ready MIDI (the export is the
  untouched source); the skill's gotchas cover the 15 melodic channels of standard MIDI.

## [0.21.0] - 2026-10-12

From the whisper tryout: voicebank-driven soft controls, an air layer for the vocal bus, and the
de-esser and saturation enabled (they move up from the stage-2/3 plan).

### Added

- **Voicebank-driven soft controls**: `render_singing` now passes `breathiness`, `voicing` and
  `tension` (0-1) through to the acoustic model when the configured voicebank's `dsconfig.yaml`
  declares the matching `use_*_embed` — a voicebank with breathiness support gets a real
  breathiness control, fed as a per-frame conditioning input only when the loaded ONNX model
  declares it (Hanami declares none, so its renders are byte-identical to before). The refusal
  for a bank without them now names the voicebank and the missing flag, and
  `list_singing_voices` reports each bank's real `soft_controls` instead of a hardcoded false.
- **An air layer on the vocal bus** (`vocal_bus.air_layer`): band-shaped noise gated by the
  voice, sitting `level_db` under the vocal's measured level — air under the singing that
  follows the words (noise colour, band corners, gate threshold/attack/release; it rides with
  the vocal and never extends the mix). It is air, not a whisper.
- **The de-esser and saturation are wired** (vocal chain): `deesser` maps to ffmpeg's deesser
  (`intensity`/`frequency`/`makeup`) and `saturation` (`drive_db`) is a compensated tanh soft
  clip; both sit between the EQ and the compressor. The "planned for the next stage" errors are
  gone.

### Changed

- Tool payload budget 148 → 152 KB (the new schemas); descriptions stay within 19 KB.

## [0.20.1] - 2026-10-12

Fixes from the Under the Birch Moon vocal test (v0.20.0).

### Fixed

- **The limiters no longer delay the mix**: `alimiter`'s lookahead was uncompensated, so a mix
  with the master limiter landed ~5 ms late against the backing (and lost that much off the
  end), and a bus limiter shifted the voices against the band inside the mix. Both now run with
  `latency=true`: sample-aligned, same ceiling, same length. (The test measured 219 samples of
  offset on the processed mix.)
- **Compressor gain reduction is now the raw reduction, before makeup** (like a DAW's meter):
  the result used to report the net figure, so a compressor with 2 dB makeup could show a
  negative "reduction" (−0.16 dB on the test's bus compressor; its raw reduction was 1.84 dB).
  The 6 dB "working hard" warning uses the raw figure too.
- **The delay now repeats**: ffmpeg's `aecho` is a single-tap filter, so however high
  `feedback` was, the delay return produced exactly one echo (and the Under the Birch Moon test
  could not confirm any echo at the dotted-eighth spacing). The return now builds real repeats
  — delayed copies scaled `feedback`, `feedback²`, …, kept while above −40 dB (up to 8).

### Added

- **`processing.resolved_delay_ms`**: the time a tempo-synced delay actually uses (the test's
  dotted-eighth at 66 BPM is 681.8 ms), so a synced delay can be checked without guessing.

## [0.20.0] - 2026-10-11

Vocal processing, stage 1: per-voice chains, a duet bus, shared reverb/delay returns, and a
master stage. Every part is off by default, so a plain mix is untouched.

### Added

- **Per-voice processing chains on `mix_song_with_vocals`**: `vocal_chain` (single vocal) or a
  `chain` per `vocals` segment — EQ (high-pass, up to six bells, low/high shelf) and a
  compressor with real settings (threshold, ratio, attack, release, makeup, knee; the defaults
  mirror the legacy fixed chain). Each voice also takes `gain_db`, `pan` and `reverb_mix` (an
  insert reverb on that voice alone). The result reports each compressor's net gain reduction
  (measured on the rendered files) and warns when one works hard.
- **The bus** (`vocal_bus`): one gain for the summed voices plus an optional glue compressor
  and limiter, with measured reduction (a bus limiter taking off more than 2 dB warns).
- **Shared returns** (`returns`): a convolution reverb — a deterministic generated impulse
  response with `decay_s`, `predelay_ms` and `damping`, normalized to unit energy so the wet
  amount means what it says — and a delay (`delay_ms`, or `tempo_sync` note values at the
  song's starting tempo, with `feedback`), fed by per-voice `sends`
  (`reverb_db`/`delay_db`).
- **The master stage** (`master`): a true-peak limiter that sits 0.3 dB under its ceiling
  (inter-sample room; the finished mix's true peak is verified and warned about) and/or
  `loudness_target_lufs` — one static gain from an EBU R128 measurement of the mix
  (reproducible, no pumping) that supersedes the peak normalization. The new `processing`
  result block echoes the effective settings, per-stage gain reduction, the measured loudness
  (integrated LUFS, range, true peak, short-term max), the limiter's reduction, and warnings.
- **Chain sidecar**: `save_chain: true` writes `<source>.vocal_chain.json`; later mixes of the
  song load it automatically when no chain option is passed (`processing.chain_source` reports
  whether the settings came from the parameters or the sidecar).

### Changed

- `mix_song_with_vocals` payload/description budgets deliberately raised (136 → 148 KB
  payload, 17 → 19 KB descriptions) for the new schemas.

## [0.19.1] - 2026-10-10

### Added

- **An `effective` block in `list_instruments` and `set_track_instrument`**: what every note
  track will actually play. Resolution order: a spec's program/bank first, then the file's own
  `program_change`/CC0 on that channel (scanned across **all** tracks, since
  `override_program` writes into the conductor), then the default (program 0; bank 128 on
  channel 10). Each entry carries the 1-based channel, bank, program, the resolved preset
  label, its `source` (`spec`/`file`/`default`), the soundfont it was looked up in, and
  warnings for shared channels, a later program change on the channel (the starting value is
  reported), or a preset missing from the SoundFont. This answers questions like "is the
  Cymbals track really the Orchestra kit?" per track — `128-048 Orchestra Kit` — without
  touching the existing `resolved` contract (still null when a spec sets no program).

## [0.19.0] - 2026-10-10

Composition additions (patch brief milestone 3: the abyssal editing issues).

### Added

- **CC automation on `add_track`**: `automation` writes linear controller ramps — e.g.
  `{"cc": 11, "from_value": 40, "to_value": 110, "start_beat": 0, "end_beat": 12}` for a held
  note that swells (CC11 expression) or a track that fades (CC7 volume) — one value every
  sixteenth note, so crescendos stop needing re-struck notes.
- **Tuplets**: a note can take an exact `tuplet` slot, e.g. `{"count": 3, "in_beats": 2,
  "start_beat": 138, "slot": 0}` for 3:2 or `{"count": 2, "in_beats": 3, ...}` for 2:3.
  Positions are computed in MIDI ticks, so they are exact — no more hand-rounded beats.
- **`create_empty_file`** (a blank conductor-only song, optionally with a section map) and
  **`remove_track`** (a copy without one named track) — songs no longer have to start from a
  chord progression, and guide tracks can be removed.
- **Per-track `mute`** in instrument specs: muted tracks are left out of stem renders and
  vocal mixes, and `bake_instruments` writes CC7=0 so external players hear them silent too.
- **Per-section `gain_db`** in section overrides: a track can be pulled back for the quiet
  sections and pushed in the climax. Applied **exactly** to each rendered stem with an ffmpeg
  volume envelope (stems and mixes carry it), and baked as CC11 expression at the section
  starts (attenuation-focused: CC11 cannot boost above 0 dB).
- **Lyric freedom**: `map_vocal_lyrics` takes a custom `words` map (word → voicebank phonemes,
  e.g. `{"lah": "l aa", "vey": "v ey"}`) checked before the dictionaries, and a
  `language="phonemes"` mode where each token is one note's raw voicebank phonemes
  (`l-aa v-ey n-ah`). Made-up languages no longer need English stand-ins, and unknown phonemes
  are listed in one error with the voicebank's own set.

### Changed

- The tools/list payload ceiling was raised from 128 KB to 136 KB (two new tools).

## [0.18.0] - 2026-10-10

The abyssal-piece fixes: long vocal renders, restart recovery and the file/listing tools.

### Fixed

- **Full-length vocal renders no longer crash the server.** The acoustic model's attention is
  O(n²) in frames — a 195-second score needed gigabytes in one inference and took the whole
  server down (five restarts, exit code 0, no OOM kill: the VM went under it). DiffSinger now
  renders in **phrase-sized chunks split at rests** (never inside a note; a single long note is
  its own chunk) and stitches them at exact offsets on a silence canvas. Each job also runs in
  a **worker process**, so a native crash kills only the worker and the job reports the real
  error instead of "Session terminated".
- **No more 0-byte "results".** Renders write to a `.part` file and rename only when complete;
  the server also sweeps stale empty media files at startup, and the listing flags them.

### Added

- **Scores, mappings and jobs persist** (singing and render jobs) to the state directory and
  reload at startup; a job that was running when the server went down comes back **failed with
  the reason**. The "kept for 24 hours" promise is now real.
- **`list_singing_jobs` and `list_render_jobs`** — find a job whose reply was lost.
- **`mix_song_with_vocals` accepts `vocals`**: a list of pieces with `start_seconds`, to join
  takes or a vocal rendered in parts (the issue-3 workaround, now native).
- **`import_audio`** (wav/mp3, with duration/sample-rate/channels/peak report) and **`get_file`**
  (fetch any output file; `get_midi_file`'s not-found error now points there).
- **`list_generated_files` filters** (`name_contains`, `kind`, `limit`) and an `empty` flag.
- **`prepare_vocal_score` returns the section and tempo map** (`sections`, `tempo_changes`)
  alongside the starting `tempo_bpm`, so a nine-tempo song is not mistaken for one tempo.
- Job progress: `get_singing_job` reports `progress` ("rendering phrase 3/7"), plus `source`
  and `created_at`.

### Changed

- The tools/list payload ceiling was raised from 116 KB to 128 KB and the description budget
  from 15 KB to 17 KB (four new tools).

## [0.17.0] - 2026-10-10

Mixing improvements from the Lanterns Below wet-stems test (items 1–2).

### Fixed

- **The mix tool now renders the backing wet.** `mix_song_with_vocals` renders the backing dry
  no matter what — a mix could never carry the instrumental reverb/chorus that a stems render
  did. It now takes the same `reverb`/`chorus` settings as `render_audio` (off by default, so
  existing mixes stay byte-identical), applied to the backing in both the single-pass and the
  per-track paths, and echoes the effective settings in `effects`.
- **The balance measurement stays honest with a wet backing**: the band's energy is now read
  from the backing file over the same voiced blocks as the mix probe, instead of a whole-file
  gated-RMS estimate that drifts once reverb fills the gaps (a wet mix measured 3.1 dB against
  a 6.0 target before this; it reads 5.8 and `ok` now).
- **The vocal echo no longer attenuates the dry vocal**: `aecho`'s default in-gain pulled the
  vocal ~1.9 dB below its measured level whenever `vocal_reverb` was on; the dry signal now
  stays at unity and the echo rides on top (the same convention the Lanterns Below test used).

### Added

- **Per-track effect sends** (`reverb_send`/`chorus_send`, CC91/CC93, 0–127) in
  `set_track_instrument` specs: FluidSynth applies its reverb/chorus by send amount, so this is
  how a track gets a bigger room than another (the Lanterns Below test hand-wrote these sends;
  a no-send track gets the GM default — a subtle reverb — and send 0 is fully dry). The sends
  are written into stem renders and baked into the MIDI by `bake_instruments`; omitting them
  leaves the file's own sends untouched.

### Changed

- **The mix tool's vocal reverb flag is renamed `reverb` → `vocal_reverb`** so `reverb`/`chorus`
  mean the backing instruments everywhere, matching `render_audio`.
- The balance measurement now also runs with the vocal reverb on: the reverb rides with the
  vocal, so the measured ratio reflects what the listener hears (the earlier `unavailable`
  behavior is gone).

## [0.16.0] - 2026-10-10

Long-render support (patch brief milestone 6, part 1).

### Added

- **Background render jobs**: `render_audio_job` starts any render (same options as
  `render_audio`, plus `tracks`) on a worker thread and returns immediately;
  `get_render_job(job_id)` polls it with **per-track progress** (`pending`/`running`/`done`/
  `failed`, with the error message per failed track) and the full render result when done. This
  removes the client-timeout ceiling that made three-minute stems renders impossible — the
  work no longer depends on how long a client is willing to wait.
- **Partial failures are usable and retryable**: in job mode a failed track no longer aborts
  the render — the mix combines the successful stems, the result lists `failed` tracks, and
  `render_audio_job(..., tracks: ["Drums"])` retries just those tracks (successful stems are
  not re-rendered).

### Changed

- The tools/list payload ceiling was raised from 96 KB to 108 KB (the job tool mirrors
  render_audio's options).

### Verified

- A ~3-minute anime-style song (176.15 s: six sections including a 7/8 bridge and
  100→128→140→100 BPM changes, 992 section-relative drum hits plus bass and guide) ran the full
  pipeline: the vocal's first climax note landed at 119.925 s — exactly the tempo map's
  integrated time; the stems job finished in 58 s with per-track progress and a mix of 176.15 s
  (the map's length, ending complete); mix and export followed (vocal 6 dB over the dipped band,
  balance ok). No request timeouts — job mode removes the client-wait ceiling entirely
  (FluidSynth itself has no duration limit).

## [0.15.0] - 2026-10-10

Section-based instrument selection (patch brief milestone 5).

### Added

- **Per-section program/bank overrides** in `set_track_instrument` specs:
  `{"sections": {"Climax": {"program": 30}}}` switches a track's sound at that section's start
  without splitting the part into separate tracks. Bank select lands before the program change;
  sections not listed keep the track's own program/bank; section names are validated against
  the file's section map and every section's program is validated against the SoundFont.
- **Held voices keep their preset.** Program changes only affect notes that begin after them
  (FluidSynth assigns the preset per voice) — verified by a byte-identical render test where a
  note sustains across the boundary and a differing-render test where a new note starts under
  the new sound.
- **`bake_instruments`** writes a copy of a file with the specs' bank/program timeline
  materialized as real MIDI events (per-section changes at their exact boundaries), with the
  required `.sf2` per track returned as companion metadata. Shared channels keep one timeline
  (a MIDI limitation) and are reported in `warnings`.

### Limitations (documented)

- Per-section **SoundFont switching** (different `.sf2` files across sections) is not supported
  yet; a section preset must be the same file as the track's preset, and a mismatch is rejected
  with a clear message.

## [0.14.0] - 2026-10-10

Instrumental reverb and chorus (patch brief milestone 4).

### Added

- **FluidSynth reverb and chorus on `render_audio`** (`reverb`, `chorus`), off by default so
  existing renders stay byte-identical. Defaults and ranges are the installed engine's own
  (verified against FluidSynth 2.4.4: reverb room 0.5/damp 0.3/width 0.8/level 0.7; chorus
  3 voices/0.6/0.2 Hz/4.25 ms — this build has no chorus waveform setting, so none is offered).
  With effects on, the engine's natural tail past the last note (~2.7 s) is kept instead of
  being trimmed. The result echoes the effective settings in `effects`.
- Scope is documented and honest: effects are **engine-wide per render instance, not per
  track** — with `stems: true` every stem renders wet, and summing wet stems is not identical
  to one wet pass. The vocal mix's `reverb` stays a separate vocal effect and is never applied
  to instruments.

### Changed

- The tools/list payload ceiling was raised from 88 KB to 96 KB for the effect schemas
  (deliberate, with a note in the test).

## [0.13.0] - 2026-10-10

MIDI import and bulk pattern ingestion (patch brief milestone 3).

### Added

- **`import_midi`** takes a MIDI file from anywhere else (a DAW, a generator, another
  assistant) as base64 and stores the bytes **verbatim** — nothing is transformed or discarded.
  The result is a structured report: tracks with channels/programs/note counts, every tempo and
  time-signature change, markers, the SysEx count, and warnings for layouts the tools care
  about (type 0 files, tempo events in several tracks, missing tempo/meter, no notes). The
  imported file works with the whole workflow, and a round-trip through `get_midi_file` returns
  the exact bytes.
- **Step patterns in `add_track`**: `patterns: [{"pitch": 36, "steps": "x---x---"}]` writes
  drum-style grids compactly — one row per voice, `x` hits, `X` accents (+25), `-`/`.` rests,
  `|` and spaces are bar lines that do not consume a step, `step_beats` sets the grid (default a
  sixteenth). Patterns combine with `notes` and tile with `loop`; `notes` is now optional.

## [0.12.0] - 2026-10-10

Shared sections with tempo and meter (patch brief milestone 2).

### Added

- **`set_song_sections`** writes a song-level section map into the MIDI conductor: each section's
  start gets a real `time_signature`, a `set_tempo` and a `section: <name>` marker, so renders,
  exports, `analyze_midi` and the vocal score follow one authoritative timing map. Tempo is
  always quarter-note BPM; a section carries bars, BPM, numerator/denominator and an optional
  accent `grouping` (e.g. `2+2+3`, stored in a small sidecar that follows rename/delete). The
  result reports each section's start beat, total beats and the duration integrated over the
  whole map — not the initial BPM applied to the song. This is the one tool that edits a file's
  timing in place; notes are never touched, and old single-tempo files keep working unchanged.
- **Section-relative note positions** in `add_track`: each note can use `section` + `bar` +
  `beat` (1-based, e.g. 7/8 bar 2 beat 3.5) instead of absolute `start_beat`. Notes may run past
  a section's end (sustains across boundaries are intentional); starting past the last bar is an
  error that names the section.
- `analyze_midi` now reports the full `tempo_changes` and `time_signature_changes` lists and the
  file's `sections`; `tempo_bpm`/`time_signature` are the values the file starts with.

## [0.11.0] - 2026-10-10

Preset verification and level diagnostics (patch brief milestone 1).

### Added

- **Real SoundFont preset inventory.** A new `pdta`/`phdr` reader (`soundfont.py`) reports the
  actual preset table of a `.sf2` — name, bank and program, both 0-based. `list_instruments`
  returns the inventory of the loaded (or a given) soundfont instead of the static GM reference
  (which is still the fallback when no soundfont file is readable); FluidR3 reports 189 presets
  with its drum kits in bank 128.
- **Requested presets are verified, never assumed.** `set_track_instrument` checks the requested
  bank/program against the soundfont it will play from (percussion tracks resolve against bank
  128) and rejects a miss with the list of what that bank actually has; the result echoes the
  resolved preset label (e.g. `128-016 Power`).
- **Level diagnostics and an explicit mix normalization.** `render_audio` gains
  `normalize_peak_db` (default `null` = raw FluidSynth level, so nothing changes unless asked)
  and reports `peak_db`, `gain_correction_db` and `clipping`; every stem entry carries its own
  `peak_db`. Stems are never normalized — their balance must sum back to the unnormalized mix.
- `list_generated_files` lists rendered audio (`kind`: midi/wav/mp3) so a stem or mix can be
  confirmed on disk.

### Changed

- The tools/list payload ceiling was deliberately raised from 72 KB to 88 KB to make room for
  the new params and result fields (and the upcoming section/import/render-job milestones).

## [0.10.1] - 2026-10-10

### Fixed

- **The guide track default is deterministic.** With no `guide_track`, an exact `Vocal`,
  `Voice` or `Melody` track name now wins (in that order) before falling back to the first
  melody-like name — a file holding both `Lead` and `Vocal` used to resolve to whichever came
  first in the file. Documented in the singing guide and the tool descriptions.

## [0.10.0] - 2026-10-10

### Fixed

- **Vocal level with ducking.** `vocal_level_db` is again the level you hear over the band
  during phrases: the vocal gain is reduced by `duck_db` against the original backing (v0.8.0
  measured the original backing instead, so the vocal landed `duck_db` louder than requested).
  The result now reports `vocal_to_original_backing_db` next to `vocal_to_backing_db`.
- **The server stays responsive during renders.** FastMCP runs synchronous tool bodies on the
  event loop, so a long stem render froze every other request (status checks failed with
  "unhandled errors in a TaskGroup" while it ran); `render_audio` and `mix_song_with_vocals` are
  now executed on worker threads.

### Changed

- **Stem renders run several at a time** (a bounded pool): a nine-track, 99-second song took
  ~2m20s sequentially — past any client's tool timeout — and renders in a fraction of that now;
  the per-track backing renders in `mix_song_with_vocals` are parallel too.
- **Stems are delivered as signed URLs whenever `CHORDSMITH_PUBLIC_URL` is configured**: a
  nine-track song inlined ~230 MB of base64 wavs, which no client can take in time; the mix
  still follows `return_as`.
- **`mix_song_with_vocals` honors `set_track_instrument` specs for the backing** (soundfont, GM
  program, gain — `backing_levels` overrides a spec's gain) and reports the `instruments` used,
  so a mix and a stems render of the same file agree.

## [0.9.0] - 2026-10-10

### Added

- **Per-track General MIDI instruments.** `set_track_instrument` specs gain `program` (0–127)
  and `bank` (CC0 bank select), so each track can play a different GM instrument from the same
  soundfont — or a drum kit on a drum track — next to the existing per-track `preset` (`.sf2`)
  and `gain_db`. The override replaces the stem's own program change and applies to that stem
  only; `render_audio` with `stems: true` renders and combines it like any other spec.
- `list_instruments` now also returns the instrument catalog: the 128 GM melodic program names
  (index = `program` number) and the standard drum kits (name → kit number), so the numbers
  `set_track_instrument` takes are discoverable from the API itself.

## [0.8.0] - 2026-10-10

### Added

- **Per-track rendering and stems.** `render_audio` with `stems: true` renders every track on
  its own and sums the stems into the mix; the result lists each stem with track name, gain,
  sha256 and bytes or a signed link. `set_track_instrument` stores per-track specs (a `.sf2`
  soundfont per track, a trim) in a JSON sidecar that follows the file through rename and
  delete; `list_instruments` reports the engines this server can run. Engines are invoked as
  **separate processes**, so a crashing or differently licensed (GPL) host can never take the
  server down or enter this MIT codebase.
- **Ducking depth (`duck_db`, 0–12 dB, default 4).** With ducking on, the vocal now sits
  `vocal_level_db + duck_db` above the band while singing: the dip is applied *after* the gain
  is computed on the original backing. The previous version measured the ducked backing, so the
  vocal was turned down by the same amount and the dip cancelled itself out.
- **True-peak clipping guard.** The peak is measured on a float render before the 16-bit
  export, so a hot mix is turned down from its true peak and a clipped file is never written —
  even with `normalize_peak_db: null`.

### Changed

- `compress` now defaults to **false**: it halves the voice's level swings (measured ~±4.5 dB
  to ~±2.5) but costs diction on dense, loud mixes (89/102 against 95/102 on a death-metal
  test), so it is opt-in. It is not universally worse: on a quiet vocal over a loud backing it
  can help a lot (61/88 against 82/88 at vocal +6 dB), so judge each song by ear.
- The `overwrite` option's description now mentions the numbered-name fallback (`song_2.wav`).

## [0.7.1] - 2026-10-10

### Fixed

- **Removed the mix-bus safety limiter added in v0.7.0.** On loud, dense mixes the always-on
  `alimiter` does heavy gain reduction and smears consonants: on *The Old Ones Call* it cost
  8 of 102 words (87 with the limiter, 95 without, measured with the pinned Whisper setup). The
  existing peak-normalization loop already prevents clipping, so the limiter was redundant.

## [0.7.0] - 2026-10-10

### Added

- **Vocal dynamics (`compress`, default on)**: the vocal gets a gentle high-pass and compressor
  before the balance is measured, because DiffSinger can swing about 6 dB between notes —
  measured in half-second windows. `compress: false` mixes the raw voice.
- **Ducking (`ducking`, default off)**: the vocal's envelope dips the backing about 4 dB with a
  fast attack and a slow release, so the band steps back while the voice sings. The dip is
  computed in Python before the balance measurement (ffmpeg's `sidechaincompress` truncates its
  output unpredictably), so the reported balance stays honest.
- A gentle safety limiter on the mix bus under the -1 dBFS export level, so transient peaks are
  caught before the export normalization.
- The mix result echoes `compress` and `ducking`.

## [0.6.1] - 2026-10-10

### Fixed

- **Concurrent mixes no longer share a backing file.** The backing wav/midi are internal
  intermediates, but they were claimed with the caller's `overwrite` flag, so two mixes of the
  same song running at once with `overwrite: true` both wrote `song_backing.wav` — one could
  read a half-written file. The backing is now always claimed under a unique name; `overwrite`
  applies only to the final mix. A regression test runs two concurrent overwriting mixes and
  asserts distinct backings.

## [0.6.0] - 2026-10-10

### Added

- **Loop patterns in `add_track`**: `loop: {"length_beats": 4, "times": 48}` writes a one-bar
  drum or bass groove once and tiles it across the song, instead of hundreds of hand-written
  notes (up to 20 000 notes after tiling; notes must fit inside the pattern window).
- **Chord hit positions**: `rhythm.hits` places the chord at explicit beats inside each chord,
  e.g. `[0, 0.75, 1.5, 2.25]` for off-beat stabs, chugs and gallops. Overrides
  pattern/subdivision/swing; gate, velocity and humanize still apply.
- **Per-stem backing levels**: `mix_song_with_vocals` accepts `backing_levels`, dB trims keyed
  by track name (`{"Drums": -4, "Pad": 2}`). Each backing track is rendered separately and mixed
  with its trim, so drums, bass and pads can be balanced against each other; it costs one render
  per track. The result echoes the trims.
- **Rate limiting** on the MCP endpoint, per the specification's security requirements: requests
  are capped per client (`CHORDSMITH_RATE_LIMIT`, default 120 per minute, `0` disables) and
  over-limit calls get `429` with `Retry-After`. Clients are told apart by a hash of their bearer
  token, so the limit works behind the tunnel; stdio is unaffected.
- Docs: `add_track` loop example, `rhythm.hits` reference, per-stem mixing notes, and the
  `CHORDSMITH_RATE_LIMIT` setting.

## [0.5.4] - 2026-10-10

### Fixed

- **`list_singing_voices` with the default engine works again.** With `engine: "all"` the tool
  failed output validation (`None is not of type 'array'`) because the top-level `voices` field
  of its result schema was not nullable; single-engine calls were unaffected. A regression test
  now calls the default.

### Added

- Docs: the singing guide covers the voicebank's tested range (F4–Eb5) and style character, the
  "leave a breath before a consonant cluster" tip, and scoring long songs in halves (Whisper can
  repeat the first half after an instrumental drop); the tools reference states the General MIDI
  rendering ceiling and the mix's lack of ducking, EQ and per-track levels.

## [0.5.3] - 2026-10-10

### Fixed

- **Lyric tokens: one per sung note.** The `map_vocal_lyrics` description and the singing guide
  now say that rests take no token, instead of the ambiguous "one token per note" — building a
  song with rests previously required guessing.

## [0.5.2] - 2026-10-10

### Fixed

- **Final consonants are no longer cut off.** The linguistic encoder was given one-frame word
  durations, so the duration model predicted closing consonants at 12–35 ms — "glass" sang as
  "gla", "haze" as "hey", "car" as "ca". Word durations are now real (vowel-anchored spans, the
  voicebank's `ph_num` convention): on the diagnostic line /z/ goes 35 → 186 ms, /s/ 35 → 244 ms
  and /r/ 12 → 337 ms, and the consonants are audible.
- **Onsets no longer eat the previous word's coda.** With realistic onset lengths, the
  beat-ahead placement was consuming the previous note's closing consonants to make room;
  onsets now anticipate into the previous vowel first, and codas keep their full length.
- Seeded renders stay reproducible, but their bytes differ from pre-fix renders — re-render
  stored seeds before comparing across the fix.

### Added

- Docs: "How tools appear to MCP clients" (titles, behaviour hints, output schemas, and the MCP
  tools specification), linked from the README.
- The singing tools now state their retention: scores, mappings and job records are kept for
  24 hours (rendered files stay in the output folder).

## [0.5.1] - 2026-10-10

### Added

- **Typed results for every tool**: all 18 tools declare an output schema, so clients receive
  machine-checkable `structuredContent` (field names, types and nullability) next to the text.
- **Agent metadata**: every tool has a human title and MCP behaviour annotations
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`), so agents can tell safe reads from file
  creation and destructive calls before calling.
- **`align_stress`** (opt-in) in `map_vocal_lyrics`: when a stressed syllable gets a much shorter
  note than a weak one in the same word, the two note lengths are swapped (pitch order and total
  length unchanged) — fixes the classic "SO-di-um" mismatch without hand-editing the melody.
- The `seed` option is now documented in the `create_chord_progression` reference (it was already
  supported).

### Fixed

- **Overlapping calls can no longer race to the same output file.** Filenames are claimed
  atomically before writing (`O_CREAT|O_EXCL`) and the claim is cleaned up when a render fails,
  so two jobs that used to pick `song_vocal.wav` now always get distinct files.
- **The mix measures the vocal at the mix rate.** The resampler's anti-alias filter costs 24 kHz
  VOICEVOX output a couple of dB on the way to the 44.1 kHz mix, so the balance was computed from
  a level the file never had: VOICEVOX mixes landed ~2.7 dB below the requested `vocal_level_db`
  and `balance_check` reported a false `mismatch`. The vocal is now converted first and measured
  as it will be mixed.
- `mix_song_with_vocals` called with a **job id** uses the job's own vocal file, even when a file
  with the same name exists on disk (previously it could mix the wrong take).
- Tool descriptions and schemas tightened: every option has a description and its default in the
  input schema, every description says what the tool returns, and the tools/list payload is
  budgeted by a test.

## [0.5.0] - 2026-10-10

### Added

- **Reproducible renders (`seed`)**: DiffSinger sampling is stochastic; setting `seed` in
  `render_singing` patches the diffusion noise in the ONNX graphs (in memory) so the same input
  renders **identical bytes** — fair A/B tests. `null` (default) keeps fresh noise per run.
  VOICEVOX has no seed control and may also vary slightly; identical requests reuse the cached job.
- **Measured balance check**: `mix_song_with_vocals` reports
  `vocal_to_backing_measured_db`, computed from the finished mix (band energy subtracted over the
  sung blocks), next to the calculated value, with a `balance_check` flag.
- **Full settings echo**: render jobs record the complete settings including defaults (`gender 0`,
  `steps 20`, ...), not just the overrides.
- **Whole-word English lookup**: hyphen-joined syllables ("so- di- um", "yel- low") are looked up
  as one word and its sounds are split across the word's notes (one vowel per syllable, maximal
  onset principle), fixing fragments, double consonants and weak vowels; vowel-count mismatches
  fall back to per-piece lookup with a warning that names the word.
- **Stress warnings**: when a stressed syllable lands on a much shorter note than a weak one in
  the same word, the mapping warns and suggests swapping them.
- `legato` now defaults to `0.25`, so syllables connect without asking (pass `0` to keep gaps).
- Mix normalization: the exported mix is normalized to `normalize_peak_db` (default −1 dBFS) so
  quiet mixes are not left 19 dB down; set null to keep the raw level.
- `/healthz` reports the running version (useful when auditing which build is deployed).
- Closing consonants may use up to half of a note (was 40%), so dense codas stay intelligible.

### Fixed

- **Mixing balances levels by measurement.** `mix_song_with_vocals` measures the active level of
  both stems **over the blocks where the voice is singing, across all channels** and places the
  vocal `vocal_level_db` (default 6 dB) above the band; the mono vocal is panned to stereo first,
  so the measured balance is real (previously a hidden ~3 dB mono-to-stereo loss made 6 dB read
  as ~3.8 dB, and a one-sided stereo band was judged by its left channel alone).
  `vocal_to_backing_db` includes the `backing_volume` trim, reverb defaults to off, and the result
  reports the levels, gains and the final `peak_db`.
- **Slurs no longer close and reopen a syllable**: `+` moves the previous syllable's closing
  consonants to the last note of the slur ("still +" sings s-t-ih then ih-l, not "stil-i").
- **English lyric mapping no longer crashes on fewer syllables than notes** (the extra notes
  become rests with a warning), and phonemes are assigned to the notes that remain after holds,
  so text and sounds stay aligned.
- **DiffSinger `gender` defaults to 0** (the voicebank's own character) instead of the extreme
  −1 shift.
- **Vocal scores spell notes to match the key** (Eb/Ab/Bb in flat keys, like the chord tools,
  instead of D#/G#/A#).
- Rests created from missing syllables carry no pitch, and render jobs echo their settings.

## [0.4.0] - 2026-10-10

### Added

- DiffSinger English singing (Sodium Gold slice 2): `diffsinger:hanami/root|fragrance|nectar`
  voices via an in-process ONNX pipeline (linguistic, duration, pitch, acoustic, AI-dolGAN
  vocoder). Voicebanks are mounted read-only and never bundled; `list_singing_voices` reports
  each licence layer and the commercial status.
- Engine-neutral vocal scores: notes carry seconds plus a full tempo map; VOICEVOX frames are
  derived at render time.
- English lyric mapping (`language: "en"`): the voicebank dictionary first, then CMUdict; unknown
  words are refused by name. `+` continues the previous note, `-` is a pause, `br` a breath.
- `seed` parameter on both create tools, for byte-reproducible humanized renders.
- "Did you mean ...?" suggestions for unknown chord qualities, modes and voice ids.
- Golden-file tests (exact notes, timing, tempo and markers), create → analyze round trips for
  all 30 chord types, slash/extended/enharmonic/time-signature edge cases, path-safety tests and
  a stdio CI smoke test.
- CHANGELOG, README badges and roadmap, docs/architecture.md, Dependabot and pip-audit in CI.

### Fixed

- VOICEVOX decode-only voices (including the whisper styles) now sing: the frame query is
  prepared by the engine's teacher style and the chosen voice's timbre synthesizes it.
- Correct scientific octave for enharmonic spellings in tool responses (B#4, Cb4, Bbb4).

## [0.3.0] - 2026-10-09

### Added

- Singing pipeline (Sodium Gold slice 1): a wordless hum or Japanese kana with VOICEVOX, exposed
  as seven tools (voices, score, lyrics, background render job, mix, export).
- Chord tones are spelled with correct letters and accidentals everywhere (C7 = C E G Bb,
  Cdim7 = C Eb Gb Bbb, Abm = Ab Cb Eb).
- `analyze_midi` uses the file's own chord-name markers when their notes match the window, so
  inversions, voice leading and mixed melody+chord files read back correctly.

### Fixed

- Humanized timing is clamped to each chord's window, so notes never drift into the next chord.

## [0.2.0] - 2026-10-09

### Added

- OAuth 2.1 for HTTP transports: sign-in page, dynamic client registration and Client ID
  Metadata Documents, PKCE S256, well-known discovery aliases, persistent clients and tokens.
- Remote deployment: Docker Compose with Caddy (automatic HTTPS) or a Cloudflare Tunnel,
  `/healthz`, bind-mounted `./data` and `./state`.
- New tools: `get_midi_file` (base64 or signed expiring download URL), `add_track`,
  `render_audio` (wav/mp3 via FluidSynth), `delete_midi_file`, `rename_midi_file`.
- `midi_type` 0/1 for players that ignore tempo, rhythm `swing`, seeded `humanize` and the
  `lofi` preset.

## [0.1.0] - 2026-10-09

### Added

- Initial release: chord progressions to MIDI (`create_chord_progression`,
  `create_progression_from_roman`, `list_chord_types`, `transpose_midi`, `analyze_midi`,
  `list_generated_files`).

[Unreleased]: https://github.com/attep/chordsmith-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/attep/chordsmith-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/attep/chordsmith-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/attep/chordsmith-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/attep/chordsmith-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/attep/chordsmith-mcp/releases/tag/v0.1.0
