# Singing (Sodium Gold)

ChordSmith can sing: give it a melody track and it renders a soft vocal, then mixes it with the
backing. Two engines are supported:

- **VOICEVOX** (slice 1): a wordless hum on "う" (oo) or Japanese kana lyrics, soft voices,
  including whisper styles.
- **DiffSinger** (slice 2): English lyrics through a voicebank you download and mount yourself
  (Hoshino Hanami ~AI❤dol~ is the tested one). The voicebank is never bundled or committed.

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Tools](#tools)
- [Voices and soft controls](#voices-and-soft-controls)
- [English lyrics (DiffSinger)](#english-lyrics-diffsinger)
- [Mixing and export](#mixing-and-export)
- [Settings](#settings)
- [Limits and licence notes](#limits-and-licence-notes)

## How it works

1. `prepare_vocal_score` reads a **monophonic** melody track from a MIDI file, turns gaps into
   rests and converts the timing to VOICEVOX frames (93.75 per second). Default transposition is
   **-12** (one octave down), which suits a soft, light voice. The source file is never changed.
2. `map_vocal_lyrics` attaches one token per sung note (rests take none). Omit the lyrics for a wordless hum ("う" on
   every note), pass Japanese kana (`う う う`), or English words/syllables with `language: "en"`
   (`so- di- um gold`; `+` continues the previous note, `-` is a pause, `br` a breath). Syllables
   joined by hyphens are looked up as the **whole word** and its sounds are split between the
   word's notes (one vowel per syllable, maximal onset: "yel- low" sings y-eh then l-ow). A `+`
   carries the previous vowel onto the next note and moves the closing consonants to the last
   note of the slur ("still +" sings s-t-ih then ih-l). Mismatches and unknown words are reported
   by name; when a stressed syllable lands on a much shorter note than a weak one in the same
   word, a warning suggests swapping them. `holds` merges a note into the following ones (one
   sustained pitch).
3. `render_singing` starts a background job and returns immediately; `get_singing_job` reports
   `queued`, `running`, `done` (with the vocal file) or `failed` (with the error). Repeating the
   same request reuses the finished job instead of rendering twice.
4. `mix_song_with_vocals` renders the backing **without the guide track** and balances levels by
   measurement: the vocal is placed `vocal_level_db` (default 6 dB) above the backing's active
   level, reverb is off by default, and the mix is turned down if it would clip.
5. `export_vocal_song` returns the mix as WAV and MP3, the vocal alone, and the untouched MIDI —
   as base64 or as signed, expiring links (the same mechanism as `get_midi_file`).

## Quick start

With Docker Compose, start the singing engine alongside ChordSmith:

```bash
docker compose --profile singing up -d --build
# or, behind a Cloudflare Tunnel:
docker compose -f docker-compose.tunnel.yml --profile singing up -d --build
```

Without Compose:

```bash
docker run -d --name voicevox -p 50021:50021 voicevox/voicevox_engine:cpu-ubuntu22.04-latest
# and point ChordSmith at it:
#   -e CHORDSMITH_VOICEVOX_URL=http://host.docker.internal:50021   (Docker)
#   CHORDSMITH_VOICEVOX_URL=http://127.0.0.1:50021                 (local, default)
```

Then, in your AI app:

> Prepare a vocal score from twinkle_twinkle_lofi_piano_72bpm.mid (Melody track, down an octave),
> hum it wordless with a soft voice, mix it with the backing and give me the MP3.

## Tools

| Tool | What it does |
|---|---|
| `list_singing_voices` | Voices with language, licence note and supported soft controls |
| `prepare_vocal_score` | Melody MIDI track → vocal score (score id, notes, rests, frames; `legato` closes small gaps) |
| `map_vocal_lyrics` | Score → lyrics mapping (wordless or kana), with warnings |
| `render_singing` | Mapping + voice → background job (job id) |
| `get_singing_job` | Job status; when done, the vocal file, sample rate, length, offset |
| `mix_song_with_vocals` | Backing without the guide + vocal → mix with levels and clipping check |
| `export_vocal_song` | Mix WAV + MP3, vocal alone, untouched MIDI (base64 or signed links) |

## Voices and soft controls

`list_singing_voices` asks the engine. VOICEVOX styles come in two kinds: `sing` styles (like
波音リツ ノーマル, `voicevox:6000`) can both prepare the frame query and synthesize, while
`frame_decode` styles (most voices) can only synthesize. For a decode-only voice ChordSmith
prepares the query with the engine's teacher style and synthesizes with the chosen voice's
timbre, so the whole list is usable — including the whisper styles that suit the soft brief:
四国めたん ヒソヒソ (`voicevox:3037`), ずんだもん ヒソヒソ (`voicevox:3038`) and
満別花丸 ささやき (`voicevox:3071`). `list_singing_voices` marks each voice with `style_type`
and `query_via_teacher`; the job result reports `query_voice_id` when a teacher was used.

Soft controls (VOICEVOX):

- `energy` (0.1–2.0): overall vocal level; keep it near 1.0 and shape softness with the cap.
- `volume_cap` (0.05–1.0): clamps every frame's volume, so loud notes stay quiet (try `0.6`).
- `breathiness` / `vibrato`: DiffSinger-only; VOICEVOX fails clearly if these are set.

## English lyrics (DiffSinger)

English singing uses a DiffSinger voicebank running in-process (onnxruntime, CPU). ChordSmith
never downloads or redistributes voicebanks — you download one yourself, keep it out of git, and
point ChordSmith at it:

```bash
# download a voicebank (e.g. Hoshino Hanami ~AI❤dol~ for DiffSinger) and extract it, then:
export CHORDSMITH_DIFFSINGER_VOICE="/path/to/voicebank"     # the folder with dsconfig.yaml
# Docker: mount it read-only and set the same variable (compose mounts ./voicebank to /voicebank)
```

Then:

> Prepare a vocal score from my file (Melody track), map the lyrics "so- di- um gold" in English,
> render it with diffsinger:hanami/nectar and mix it with the backing.

Voices: `diffsinger:hanami/root`, `diffsinger:hanami/fragrance` and `diffsinger:hanami/nectar`
(the soft one). `list_singing_voices` reports the credit line and the licence layers with every
voice.

Character and range: **root** is the brightest and holds up best on loud material, **nectar** is
the soft one, **fragrance** sits between. The tested clean range is **F4–Eb5**; C4 worked in one
song but lower notes are untested, and words on the top notes (Eb5) can be fragile — if one word
at the very top keeps failing ("glow" is the known case), lower that note or choose another word.
DiffSinger sings clean: no growl or scream.

**Settings** (inside `settings`): `velocity` (0.5–2.0, singing speed), `gender` (−1..1 formant
shift; **default 0** keeps the voicebank's own character — large values sound unnatural),
`expr` (0–1 pitch expressiveness), `steps` (diffusion steps, default 20), `depth` (≤ 0.6) and
`seed`. DiffSinger sampling is stochastic, so without a seed the same input renders slightly
different audio each time; setting `seed` patches the diffusion noise deterministically and the
same input then renders **identical bytes** (fair A/B tests). VOICEVOX has no seed control and can
also vary slightly between renders — repeat an identical request to get the cached job's file.
`energy` works as output gain; `breathiness` is refused (this voicebank has none). Rendering takes
tens of seconds on CPU for a full song — it runs in the same background job flow as VOICEVOX. The
job record echoes the full settings (defaults included), so it always shows what was used.

**Licence layers** (for Hanami ~AI❤dol~): the voicebank models are under the Team L❤VE Voicebank
License (any creative use including commercial, credit required); the bundled AI❤dolGAN vocoder
is CC BY-NC-SA 4.0 (**non-commercial**) — get written permission or swap the vocoder before a
commercial release. Always check each voicebank's own terms.

## Writing lyrics that stay clear

An instrumental melody leaves 30–50 ms gaps between notes; sung literally, those gaps break words
apart ("so-di ... um"). Practical guidance, drawn from listening tests:

- **Join the notes**: `prepare_vocal_score` closes gaps shorter than a quarter beat by default
  (`legato: 0.25`), so syllables connect; pass `legato: 0` to keep the gaps as rests. Leading
  silence is kept.
- **Fewer syllables, deliberate alignment**: put stressed syllables on the longer notes, and use
  `+` to carry a vowel across several notes (melisma) instead of cramming new text onto every
  instrumental note. `holds` sustains one pitch; `+` follows the melody. The tool warns when a
  stressed syllable gets a much shorter note than a weak one in the same word.
- **Simplify short notes**: avoid dense consonant clusters ("streets", "lights") when a note only
  lasts a quarter second; spell a word differently or give it a longer note.
- **Leave a breath before a cluster**: when a word starts with a consonant cluster right after a
  stop-ending word ("foot steps"), leave a small rest before it — otherwise the stop elides into
  the cluster and the first word disappears ("for steps"). A quarter-beat rest is enough.
- **Clarity first, softness later**: mix with the default `vocal_level_db: 6` and reverb **off**
  to judge diction, then add reverb or lower the vocal if the song needs it.
- **When the melody fights the stress**: if the mapper warns that a stressed syllable got a short
  note (the classic "SO-di-um" case), swap the note lengths in that phrase or respell the word so
  a consonant separates the vowels; no setting fixes this automatically.

**Evaluating clarity fairly** (for A/B tests, human or Whisper): render the **vocal alone** with a
fixed `seed`, keep the same lyric, and run each version **two or three times**, scoring the median
— DiffSinger without a seed varies run to run, and one-run verdicts mostly measure chance. Songs
with a long instrumental section (a drop or breakdown): score the parts **split at the gap**, or
Whisper can repeat the first half after the silence and drag the whole-file number down.

## Mixing and export

`mix_song_with_vocals` measures the active level of both stems **over the blocks where the voice
is singing** (all channels, so a stereo band is judged as a whole) and places the vocal
`vocal_level_db` dB above it (default `6`). The vocal is converted to the mix rate **before**
measuring, so 24 kHz VOICEVOX output is judged as it will be mixed (the resampler costs it a
couple of dB). The mono vocal is panned to stereo before mixing, so
the measured balance is the real one; `backing_volume` trims the backing (default `1.0`) and
`reverb` is **off** by default (it adds a gentle echo, not a room reverb). The finished mix is
normalized to `normalize_peak_db` (default `-1` dBFS; set null to keep the raw level), so exports
are not left very quiet. The result reports `backing_rms_db`, `vocal_rms_db`, `vocal_gain_db`, the
calculated `vocal_to_backing_db`, the **measured** `vocal_to_backing_measured_db` (taken from the
finished mix by subtracting the band's energy) with a `balance_check` flag (`ok` / `mismatch` /
`unavailable` — the measurement assumes dry stems, so reverb makes it unavailable), the applied
`gain_correction_db` and the final `peak_db`. The guide track (usually `Melody`) is left out of
the backing by default. Exports: mix `.wav` + `.mp3`, the vocal `.wav`, and the original `.mid`,
all via base64 or signed links.

The mix has level, per-stem trims, a vocal compressor and ducking, plus normalization — no EQ.
When the band masks the voice (cymbals are the usual culprit), shape it in the arrangement (keep
cymbals out from under the vocal, write softer velocities), raise `vocal_level_db`, trim the
offending track with `backing_levels`, or turn on `ducking`.

**Vocal dynamics (`compress`, default on):** the vocal gets a gentle high-pass and compressor
before the balance is measured, because DiffSinger can swing about 6 dB between notes — measured
in half-second windows, the raw voice moves ±6 dB while the compressed one stays within ~±2. Set
`compress: false` to mix the raw voice.

**Ducking (`ducking`, default off):** the vocal's envelope dips the backing about 4 dB with a
fast attack and a slow release, so the band steps back while the voice sings and recovers
between phrases. The dip is computed from the vocal before the balance measurement, so the
reported balance stays honest.

**Per-stem levels:** `backing_levels` takes per-track dB trims keyed by track name, e.g.
`{"Drums": -4, "Pad": 2}`. Each backing track is rendered separately and mixed with its trim, so
a loud drum track can sit under the pad without touching the arrangement; tracks not listed keep
their level. It costs one render per track, so the mix takes longer.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CHORDSMITH_VOICEVOX_URL` | `http://127.0.0.1:50021` | Where the VOICEVOX engine listens (Compose sets `http://voicevox:50021`) |
| `CHORDSMITH_DIFFSINGER_VOICE` | `./voicebank` (auto-scan) | DiffSinger voicebank folder containing `dsconfig.yaml` |

## Limits and licence notes

- One note at a time: `prepare_vocal_score` fails on overlapping notes (pick a monophonic track).
- VOICEVOX sings Japanese kana or a wordless hum; English words need the DiffSinger voicebank.
- English phonemization looks hyphen-joined syllables up as whole words first (voicebank
  dictionary, then CMUdict), splitting the sounds across the word's notes; when the vowel count
  does not match, it falls back to per-piece lookup with a warning that names the word. Words in
  neither dictionary are refused by name (never silently skipped). Extra syllables are reported
  and ignored, missing ones become rests — the mapping never crashes on a length mismatch.
- Score note names follow the key: flat keys read Eb/Ab/Bb, matching the chord tools.
- Rendering jobs echo their settings, so the server's record is self-contained.
- Scores, mappings and jobs live in memory and expire after 24 hours.
- Voicebanks are never committed or built into the image (there is a test that enforces this).
- VOICEVOX engine is LGPL-3.0 and every character has its own terms of use. DiffSinger voicebanks
  each have their own licence layers (see above); check them before publishing rendered audio.
  `list_singing_voices` includes the licence notes with every voice.
