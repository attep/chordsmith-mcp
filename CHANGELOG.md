# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `legato` option on `prepare_vocal_score`: closes gaps between notes shorter than the given
  number of beats, so syllables connect instead of being broken apart by instrumental
  articulations (recommended: `0.25`).
- `/healthz` reports the running version (useful when auditing which build is deployed).
- Closing consonants may use up to half of a note (was 40%), so dense codas stay intelligible.

### Fixed

- **Mixing balances levels by measurement.** `mix_song_with_vocals` measures the active level
  (gated RMS) of both stems and places the vocal `vocal_level_db` (default 6 dB) above the
  backing, instead of multiplying each stem blindly. Reverb defaults to off for clarity, and the
  result reports `backing_rms_db`, `vocal_rms_db`, `vocal_gain_db` and `vocal_to_backing_db`.
- **English lyric mapping no longer crashes on fewer syllables than notes** (the extra notes
  become rests with a warning), and phonemes are assigned to the notes that remain after holds,
  so text and sounds stay aligned.
- **`+` now carries only the vowel** onto the next note (a slur: "gold +" sings "g-old", not
  "gold gold").
- **DiffSinger `gender` defaults to 0** (the voicebank's own character) instead of the extreme
  −1 shift.
- **Vocal scores spell notes to match the key** (Eb/Ab/Bb in flat keys, like the chord tools,
  instead of D#/G#/A#).

Planned: rate limits and quotas (bars, tracks, file size, storage, TTL), genre presets
(jazz/pop/bossa/trap), bass-line and drum-pattern helpers, section-based song building, MusicXML
export, Roman-numeral analysis, an `explain_progression` tool, and contributor hygiene
(CONTRIBUTING, SECURITY, pre-commit, mypy).

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

[Unreleased]: https://github.com/attep/chordsmith-mcp/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/attep/chordsmith-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/attep/chordsmith-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/attep/chordsmith-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/attep/chordsmith-mcp/releases/tag/v0.1.0
