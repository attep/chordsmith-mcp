---
name: chordsmith
description: Make music through the ChordSmith MCP server — compose MIDI (chord progressions, melody, bass, drums, sections, automation), render audio with soundfonts, sing lyrics (Japanese/wordless or English), and mix/export finished songs. Use when asked to create or edit MIDI songs, chord progressions, backing tracks, or AI-sung vocals, or to render, mix, analyze or export music files. Requires the ChordSmith MCP server to be connected.
license: This skill is MIT (matching ChordSmith). Voicebanks and engines have their own terms — check them before publishing audio.
---

# ChordSmith

ChordSmith is an MCP server that makes music files: it writes `.mid` files, renders them to
`.wav`/`.mp3` with a soundfont, and sings melodies with VOICEVOX (Japanese kana or a wordless hum)
or DiffSinger (English lyrics with a user-supplied voicebank). Everything lands in the output
folder (`~/ChordSmith`; Docker uses `/data`), directly or in a project folder per song —
address files as `"my_song/verse.mid"` and derived files stay in the same project.

If the `chordsmith` tools are not in the tool list, the server is not connected. Tell the user how
to add it, then retry:

```json
{ "mcpServers": { "chordsmith": { "command": "uvx",
  "args": ["--from", "git+https://github.com/attep/chordsmith-mcp", "chordsmith-mcp"] } } }
```

Hosted servers use a streamable-HTTP URL with OAuth instead — see the repo's
`docs/getting-started.md` for every client. Never invent tool results: if the server is missing,
say so.

## Workflow 1 — an instrumental song

1. **Start a file**: `create_chord_progression` (e.g. `["Am","F","C","G"]`, tempo, filename) or
   `create_progression_from_roman` (numerals + key). `list_chord_types` shows what symbols work.
2. **Add tracks**: `add_track` writes a copy with one more track.
   - Notes: `{"pitch": "E6" or 76, "start_beat": 0, "beats": 0.5, "velocity": 80}` — place with
     `start_beat` (absolute) or with `section`+`bar`+`beat` once a section map exists.
   - Patterns: `{"pitch": 36, "steps": "x---x---"}` with `step_beats` (0.25 = sixteenth) — `x`
     hits, `X` accents, `-`/`.` rests; `loop: {"length_beats": 4, "times": 36}` repeats a short
     groove across the song (write a one-bar drum or bass pattern once).
   - Bass and drums without writing notes: `add_bassline` (styles `root`, `root_fifth`,
     `eighth_pulse`, `octave`, `walking`; chords come from the file's markers or analysis) and
     `add_drums` (grooves `four_on_floor`, `backbeat`, `rock`, `metal`, `trap`, `boom_bap`,
     `bossa_nova`, `shuffle`, `waltz`). Both write a copy and stay in the file's project.
   - `automation`: linear CC ramps, e.g. CC11 expression or CC7 fade.
   - `instrument`: a GM program number (e.g. 48 strings); `channel` 10 for drums.
3. **Sections** (verse/chorus songs): `set_song_sections` writes names, bars, quarter-note BPM and
   meter into the conductor; a `form` template (`pop`, `rock`, `ballad`, `jazz`, `blues`, `edm`,
   `cinematic`) lays out a whole structure in one call. Later notes can use section-relative
   positions.
4. **Instruments**: `set_track_instrument` for soundfont/GM program/bank, `gain_db`, `mute`,
   effect sends (CC91/CC93) and per-section overrides; `list_instruments` shows each track's
   `effective` resolution and the preset catalog; `bake_instruments` writes the settings into the
   MIDI itself.
5. **Render**: `render_audio` for short pieces, or `render_audio_job` for long songs (poll
   `get_render_job`; per-track progress, retry). `stems: true` renders per-track files;
   `normalize_peak_db` (default `null` = raw engine level; set −1 for a listening-ready peak)
   sets the export peak; `reverb`/`chorus` switch the engine's effects on.
6. **Verify**: `analyze_midi` reports key, chords, tempo changes, sections and duration.

## Workflow 2 — a sung song (the flagship)

1. `prepare_vocal_score` — extracts a monophonic melody (an exact `Vocal`/`Voice`/`Melody` track
   name wins; pass `track` when the file has several). `transpose` defaults to −12 (soft register);
   `legato` (default 0.25 beats) joins small articulation gaps.
2. `map_vocal_lyrics` — `language: "en"` for DiffSinger English (hyphen-joined syllables like
   `"so- di- um gold"`), wordless otherwise (VOICEVOX hums `"う"`); `words` overrides individual
   words; `language: "phonemes"` takes exact phonemes.
3. `render_singing` — pick `voice_id` from `list_singing_voices` (`voicevox:<id>` or
   `diffsinger:hanami/<mode>`), then poll `get_singing_job`. A `seed` in `settings` makes
   DiffSinger renders byte-identical; repeating an identical request reuses the finished job.
4. `mix_song_with_vocals` — renders the backing without the guide track, measures both stems over
   the sung parts and places the vocal `vocal_level_db` (default 6) above the band **as heard**.
   Useful options: `ducking` (+`duck_db`), `backing_levels` (per-track trims), `vocal_chain`
   (EQ → de-esser → saturation → compressor), `vocal_sends` + `returns` (shared reverb/delay),
   `vocal_bus` (gain, glue compressor, limiter, breathy `air_layer`), `master` (true-peak limiter
   and/or `loudness_target_lufs`). Read back `balance_check`, `processing` and `warnings`.
   `save_chain: true` writes a `<source>.vocal_chain.json` sidecar that later mixes auto-load.
5. `export_vocal_song` — mix WAV + MP3 + vocal + source MIDI as base64 or signed links.

## Tool map

- **Files**: `list_generated_files`, `get_midi_file`, `get_file`, `rename_midi_file`,
  `delete_midi_file`, `create_empty_file`, `remove_track`, `rename_track`, `import_midi`,
  `import_audio`, `import_tab`, `export_musicxml`
- **Compose**: `create_chord_progression`, `create_progression_from_roman`, `add_track`,
  `add_bassline`, `add_drums`, `transpose_midi`, `set_song_sections`, `list_chord_types`
- **Instruments & render**: `set_track_instrument`, `list_instruments`, `bake_instruments`,
  `render_audio`, `render_audio_job`, `get_render_job`, `list_render_jobs`
- **Singing**: `list_singing_voices`, `prepare_vocal_score`, `map_vocal_lyrics`,
  `render_singing`, `get_singing_job`, `list_singing_jobs`, `mix_song_with_vocals`,
  `export_vocal_song`
- **Analysis**: `analyze_midi` (chords, key and Roman numerals per bar), `explain_progression`
  (deterministic numerals/functions/cadences/suggestions for a chord list)

## Conventions and gotchas

- **Files**: default names come from the source (`<name>_mix`, `<name>_backing`); pass
  `overwrite: true` to replace an existing file, and `output_filename` to name a result.
- **Copy tools**: `add_track`, `remove_track`, `rename_track`, `transpose_midi` and
  `bake_instruments` never touch their source — `output_filename` must differ from it, even
  with `overwrite: true`.
- **Track names**: unique and exact; `add_track` rejects a name that already exists and
  by-name tools (`set_track_instrument`, `copy_from`, `remove_track`) match exactly. Rename a
  track with `rename_track` (a copy; set the copy's instrument specs again).
- **DAW handoff**: `export_vocal_song` ships the untouched MIDI; call `bake_instruments` first
  so another player hears the intended programs (a MIDI cannot carry a soundfont or samples).
  Standard MIDI has 15 melodic channels, so large scores share channels — `list_instruments`
  flags it, and it is fine for per-track DAWs but can collapse sounds in strict GM players.
- **Levels**: `vocal_level_db` is the vocal's level over the band as heard (measured over the
  singing). A softer take does **not** sit lower in the mix — lower `vocal_level_db` for that;
  engine softness (`energy`, DiffSinger `expr`) changes the raw file, not the mix position.
  Trim the band with `backing_levels` or a spec's `gain_db`.
- **Guide track**: when a file has several melody-like tracks (e.g. `Lead` and `Vocal`), pass
  `guide_track` explicitly; the guide is left out of the backing, never doubled.
- **Sections**: `bpm` is always quarter-note; per-section `gain_db`/instrument overrides are
  baked per section; held notes keep their preset across program changes.
- **Played feel**: `add_track` takes `swing` (fraction of `step_beats` on offbeat steps; 0.33 =
  triplet) and `humanize` (seeded `timing_ms`/`velocity_range`, the same object as the chord
  tools' rhythm). Loops tile identically, so humanize after tiling is what makes repeats breathe;
  a fixed seed stays byte-reproducible.
- **Sampled instruments**: `set_track_instrument` takes `engine: "sfizz"` + `sfz` (a path under
  the server's SFZ folder; `list_instruments` lists them) for realistic multisampled drums/bass.
  sfizz tracks only render with `render_audio(stems: true)` (or `mix_song_with_vocals`), and
  reject FluidSynth-only options (preset/bank/program/sends).
- **VST plugins**: `set_track_instrument` takes `engine: "vst"` + `plugin` (+ `plugin_preset`
  from `list_plugins` with `detail`) for `.vst3` instruments, and `effects` chains `.vst3`
  effects over any track's rendered stem (latency compensated). `plugin_params` values are
  numbers or choice **names** (e.g. `{"A Osc 1 Type": "Classic"}`): many VST3 choice parameters
  report no discrete steps, and `list_plugins` `parameter_values` scans those and lists the
  names — a wrong name fails the render with the options. Needs the VST host — on Windows the
  released `vst_host.exe`, in Docker the Linux plugins under `/vst`. vst tracks render only
  with `render_audio(stems: true)`. Amp plugins are effects: sampled guitar (sfizz) + amp in
  `effects`. **GUI-only plugins** (TONEX tones, Guitar Rig rigs, SampleTank/Syntronik patches)
  need a one-time **state capture**: `open_plugin_state_editor` opens their UI on a desktop
  server; loading the sound and closing the window saves a state file to pass as `plugin_state`
  (instrument) or effect `state` — renders then load it headless, and TONEX/Guitar Rig captures
  are verified working (they ignore their standalone apps when hosted). A server can also run
  **without** the plugins: with the Windows bridge worker (`scripts/vst_bridge.py` +
  `CHORDSMITH_VST_BRIDGE_URL`) one endpoint lists and renders them over the network. AmpliTube
  CS demo gear adds white-noise bursts — prefer licensed T-RackS v6 modules or a built-in
  `chain` otherwise. Sound recipe: programs → `plugin_preset`; named parameters → `plugin_params`
  (numbers or choice names); placeholder presets or silence → capture a state with
  `open_plugin_state_editor`; audio processors → the `effects` chain; plugin-free reverb →
  `chain.reverb`; captured states are name-addressable and a plugin can have a default state
  (`make_default`, `'none'` opts out). Full flow: `docs/vst-workflow.md`.
- **Long renders**: DiffSinger is slow on CPU (tens of seconds per minute) and runs in background
  jobs chunked at rests; long FluidSynth songs should use `render_audio_job`.
- **Long mixes**: `mix_song_with_vocals` can outrun a client's tool timeout on stems sessions —
  start `mix_song_with_vocals_job` and poll `get_mix_job` (same pattern as render jobs).
- **Audible lows**: a 30–50 Hz sine disappears on phones. Layer it: `add_track(copy_from=...,
  transpose_semitones=12)` for an octave-up layer, then again with a square/saw preset
  (`instrument` 80/81) plus a spec `chain` (`saturation`) for grit — never ship a bare sub sine.
- **Grounding on a tab**: `import_tab` (Guitar Pro/MusicXML via MuseScore — locally with
  `CHORDSMITH_MUSESCORE`, or the host worker started with `--musescore`) turns a written
  arrangement into the bed; then `copy_from` layers, per-track specs and the state library
  build the parts on top. Read the imported `tempo_changes` first: stepped "loose belt"
  automation needs smoothing before building, and vocals must be re-rendered after a tempo
  fix. Full recipe: `docs/guitarpro.md`.
- **Voices**: VOICEVOX sings Japanese kana or a hum; its whisper styles are Japanese-only and
  mostly air (they drift off pitch). English and exact phonemes need DiffSinger voicebanks the
  user provides — several may be configured at once (`./voicebank` is scanned); ids are
  `diffsinger:<bank>[/<variant>]` (short words like `hanami/nectar` still resolve) and
  `list_singing_voices` shows each bank's variants, languages and licence. Pass `voice_id` to
  `map_vocal_lyrics` so phonemes validate against the target bank (sets differ: TIGER has `aa`,
  no bare `a`). DiffSinger soft controls: `velocity`, `gender` (0 = the bank's own character),
  `expr`, `steps`, `depth`, `seed`; `breathiness`/`voicing`/`tension` work only when the
  voicebank declares the matching `use_*_embed` (the server's error names what the bank lacks).
  No mixer setting adds
  breath to a voice model that has none — for an airy feel use a soft take plus
  `vocal_bus.air_layer` (band-shaped noise gated by the voice: air, not words).
- **Licences**: never bundle or redistribute voicebanks; VOICEVOX characters and the DiffSinger
  vocoder have their own terms — check them before publishing audio.
- **Verification**: trust the measurements the server returns (`balance_check`, `peak_db`,
  `loudness`, `gain_reduction_db`, `warnings`) and `analyze_midi` for note-level facts. The user's
  ears decide the music; offer an export link when a listening check matters.

## Musical defaults that work

- Verse/chorus songs: 8-bar sections, a rest at phrase ends (breaths), cymbals kept out from under
  the vocal (or `ducking: true`).
- Soft singing: `diffsinger:hanami/nectar`, `gender: 0`, `seed` fixed for fair A/B comparisons.
- For a lullaby-quiet mix: `loudness_target_lufs` around −18 and a reverb `returns` room of a few
  seconds; keep `vocal_level_db` modest (0–3) instead of pushing the voice forward.

## More

Repository and full docs: <https://github.com/attep/chordsmith-mcp> — `docs/tools.md` (every
option), `docs/singing.md` (voices, lyrics, mixing, processing), `docs/music-basics.md` (chord
symbols), `docs/troubleshooting.md`.
