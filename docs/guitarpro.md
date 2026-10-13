# Grounding a song on a Guitar Pro file

How to take a real arrangement (a `.gpx`/`.gp` tab) into ChordSmith and build on top of it.
Files stay local: `downloads/` is gitignored and derived audio lives in the (gitignored)
output folder. Treat tabs as private references — don't publish 1:1 transcriptions.

## 1. Convert the tab to MIDI

MuseScore Studio reads GP3–GP7/GPX (plus MusicXML/MSCZ) and converts headlessly on Windows:

```powershell
winget install Musescore.Musescore   # once
& "C:\Program Files\MuseScore 4\bin\MuseScore4.exe" -o song.mid "song.gpx"
```

(The slundi/guitarpro Rust toolkit also reads GPX and writes MusicXML, but its MIDI export is
still on its roadmap and needs a Rust toolchain; alphaTab via Node is another alternative.)

GP6 `.gpx` files are BCFZ/BCFS containers — not ZIPs and not always plain zlib — so a real
Guitar Pro–capable tool is the reliable route rather than hand-parsing.

## 2. Import and figure it out

`import_midi` stores the bytes verbatim and reports every track (channel, program, note count),
the tempo map, time signatures, markers and warnings; `analyze_midi` then guesses the key and
the chords per window (default one bar).

Worked example — *Kevin Penkin – Hanezeve Caradhina* (a fingerstyle nylon-guitar arrangement):

- one track, Nylon Guitar (GM program 24), 521 notes, 3:07, 4/4
- base tempo 80 BPM with expressive shifts (80 ↔ 106/110); key guess **G major / E minor**
- markers: Intro · Solo · Chorus · Solo · Chorus · Outro (beats 0/32/64/96/160/224)
- chord windows: Em · D · G · Am9 · C · A7sus4/G · Dadd9 · D5/G · C#dim/E · A#sus2 …

## 3. Ground, then build on top

Keep the converted track as the bed and layer with `set_track_instrument`:

1. Formalize the markers as ChordSmith sections (`set_song_sections`) so per-section levels
   and mutes work; then mute/keep/gain the reference track as wanted.
2. Add parts as usual: MODO DRUM / MODO BASS 2, sfizz instruments, synth layers (Surge XT),
   captured TONEX/Guitar Rig states — `copy_from` duplicates the reference's phrases for a new
   layer (e.g. an octave-up doubling or a synth pad following the guitar).
3. Render with `stems: true` and balance with the measured per-stem peaks.

Keep the reference's tempo map in mind (expressive shifts are part of the performance) and
check `analyze_midi` again after transposing or re-sectioning.
