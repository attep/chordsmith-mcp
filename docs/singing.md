# Singing (Sodium Gold, slice 1)

ChordSmith can sing: give it a melody track and it renders a soft vocal with the
[VOICEVOX](https://voicevox.hiroshiba.jp/) singing engine, then mixes it with the backing.

This is **slice 1** of the Sodium Gold design: a wordless hum on "う" (oo) with a soft VOICEVOX
voice. English lyrics (DiffSinger adapter) and phrase-level re-rendering are later slices.

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Tools](#tools)
- [Voices and soft controls](#voices-and-soft-controls)
- [Mixing and export](#mixing-and-export)
- [Settings](#settings)
- [Limits and licence notes](#limits-and-licence-notes)

## How it works

1. `prepare_vocal_score` reads a **monophonic** melody track from a MIDI file, turns gaps into
   rests and converts the timing to VOICEVOX frames (93.75 per second). Default transposition is
   **-12** (one octave down), which suits a soft, light voice. The source file is never changed.
2. `map_vocal_lyrics` attaches one lyric per note. Omit the lyrics for a wordless hum ("う" on
   every note), or pass Japanese kana (`う う う`). Mismatches produce warnings; words are never
   dropped or invented. `holds` merges a note into the following ones.
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

`list_singing_voices` asks the engine. The Docker engine image bundles one **song model**:
波音リツ ノーマル (`voicevox:6000`). Other styles may be listed by the engine but fail at render
with "style not found" until their song models are available — ChordSmith rejects the voice
instead of substituting another one.

Soft controls (VOICEVOX):

- `energy` (0.1–2.0): overall vocal level; keep it near 1.0 and shape softness with the cap.
- `volume_cap` (0.05–1.0): clamps every frame's volume, so loud notes stay quiet (try `0.6`).
- `breathiness` / `vibrato`: DiffSinger-only; VOICEVOX fails clearly if these are set.

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

## Limits and licence notes

- One note at a time: `prepare_vocal_score` fails on overlapping notes (pick a monophonic track).
- Japanese kana or wordless only; English lyrics need the DiffSinger adapter (not in slice 1).
- Scores, mappings and jobs live in memory and expire after 24 hours.
- VOICEVOX engine is LGPL-3.0, and **each voice character has its own terms of use** — check the
  character's terms before publishing rendered audio. `list_singing_voices` includes the licence
  note with every voice.
