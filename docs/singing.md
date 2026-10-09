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
2. `map_vocal_lyrics` attaches one token per note. Omit the lyrics for a wordless hum ("う" on
   every note), pass Japanese kana (`う う う`), or English words/syllables with `language: "en"`
   (`so- di- um gold`; `+` continues the previous note, `-` is a pause, `br` a breath). English
   tokens are phonemized here as a dry run; unknown words are refused by name. Mismatches produce
   warnings; words are never dropped or invented. `holds` merges a note into the following ones.
3. `render_singing` starts a background job and returns immediately; `get_singing_job` reports
   `queued`, `running`, `done` (with the vocal file) or `failed` (with the error). Repeating the
   same request reuses the finished job instead of rendering twice.
4. `mix_song_with_vocals` renders the backing **without the guide track**, mixes the vocal in at
   soft levels with gentle reverb, and corrects the gain if the mix would clip.
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
| `prepare_vocal_score` | Melody MIDI track → vocal score (score id, notes, rests, frames) |
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

**Settings** (inside `settings`): `velocity` (0.5–2.0, singing speed), `gender` (−1..1 formant
shift), `expr` (0–1 pitch expressiveness), `steps` (diffusion steps, default 20) and `depth`
(≤ 0.6). `energy` works as output gain; `breathiness` is refused (this voicebank has none).
Rendering takes tens of seconds on CPU for a full song — it runs in the same background job flow
as VOICEVOX.

**Licence layers** (for Hanami ~AI❤dol~): the voicebank models are under the Team L❤VE Voicebank
License (any creative use including commercial, credit required); the bundled AI❤dolGAN vocoder
is CC BY-NC-SA 4.0 (**non-commercial**) — get written permission or swap the vocoder before a
commercial release. Always check each voicebank's own terms.

## Mixing and export

`mix_song_with_vocals` defaults: vocal `0.9`, backing `0.55`, gentle reverb on. The guide track
(usually `Melody`) is left out of the backing by default. The mix is measured with ffmpeg
(`volumedetect`) and, if the peak would clip, re-rendered with a corrective gain so exports stay
clean (`clipping: false`). Exports: mix `.wav` + `.mp3`, the vocal `.wav`, and the original
`.mid`, all via base64 or signed links.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CHORDSMITH_VOICEVOX_URL` | `http://127.0.0.1:50021` | Where the VOICEVOX engine listens (Compose sets `http://voicevox:50021`) |
| `CHORDSMITH_DIFFSINGER_VOICE` | `./voicebank` (auto-scan) | DiffSinger voicebank folder containing `dsconfig.yaml` |

## Limits and licence notes

- One note at a time: `prepare_vocal_score` fails on overlapping notes (pick a monophonic track).
- VOICEVOX sings Japanese kana or a wordless hum; English words need the DiffSinger voicebank.
- English phonemization uses the voicebank's own dictionary first, then CMUdict; words that are
  in neither are refused by name (never silently skipped).
- Scores, mappings and jobs live in memory and expire after 24 hours.
- Voicebanks are never committed or built into the image (there is a test that enforces this).
- VOICEVOX engine is LGPL-3.0 and every character has its own terms of use. DiffSinger voicebanks
  each have their own licence layers (see above); check them before publishing rendered audio.
  `list_singing_voices` includes the licence notes with every voice.
