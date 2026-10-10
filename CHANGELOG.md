# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Planned: rate limits and quotas (bars, tracks, file size, storage, TTL), genre presets
(jazz/pop/bossa/trap), bass-line and drum-pattern helpers, section-based song building, MusicXML
export, Roman-numeral analysis, an `explain_progression` tool, and contributor hygiene
(CONTRIBUTING, SECURITY, pre-commit, mypy).

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
