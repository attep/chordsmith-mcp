# Grounding a song on a Guitar Pro file

How to take a real, written arrangement (a `.gpx`/`.gp` tab) into ChordSmith with `import_tab`
and build on top of it. Files stay local: `downloads/` is gitignored and derived audio lives in
the (gitignored) output folder. Treat tabs as private references — don't publish 1:1
transcriptions.

## 1. Import the tab

`import_tab` accepts Guitar Pro 3–7 (`.gp3`/`.gp4`/`.gp5`/`.gp`/`.gpx`) and MusicXML/MSCZ
(`.musicxml`/`.xml`/`.mxl`/`.mscz`), converts with MuseScore, stores the derived `<name>.mid`
and returns the same report as `import_midi` (tracks with channels/programs/note counts, the
tempo map, time signatures, markers, warnings) plus `converted_from` and the converter used.

The converter must exist on a machine the server can reach:

- **Local** — install MuseScore and set `CHORDSMITH_MUSESCORE` to the executable before
  starting the server (the Windows-native setup):

  ```powershell
  winget install Musescore.Musescore   # once
  $env:CHORDSMITH_MUSESCORE = "C:\Program Files\MuseScore 4\bin\MuseScore4.exe"
  ```

- **Host worker** — on a Windows machine that has MuseScore, start the same worker that runs
  Windows VSTs with a `--musescore` option, and point the server at it (the Docker setup):

  ```powershell
  python scripts/vst_bridge.py --token <secret> `
    --musescore "C:\Program Files\MuseScore 4\bin\MuseScore4.exe"
  # server: CHORDSMITH_VST_BRIDGE_URL + CHORDSMITH_VST_BRIDGE_TOKEN (already set for VSTs)
  ```

- With neither configured, `import_tab` says so and names both setups.

MuseScore is also the **manual fallback** (any machine, no ChordSmith tool): convert headless
and import the MIDI:

```powershell
& "C:\Program Files\MuseScore 4\bin\MuseScore4.exe" -o song.mid "song.gpx"   # headless
# then import_midi with the resulting .mid
```

GP6 `.gpx` files are BCFZ/BCFS containers — not ZIPs and not always plain zlib — so a real
Guitar Pro–capable tool is the reliable route rather than hand-parsing. (The slundi/guitarpro
Rust toolkit reads GPX but its MIDI export is still on its roadmap; alphaTab via Node is
another alternative.)

## 2. Figure it out

The import report already listed every track, tempo change and marker; `analyze_midi` then
guesses the key and the chords per window (default one bar).

Worked example — *Kevin Penkin – Hanezeve Caradhina* (a fingerstyle nylon-guitar arrangement):

- one track, Nylon Guitar (GM program 24), 521 notes, 3:07, 4/4
- base tempo 80 BPM with expressive shifts (80 ↔ 106/110); key guess **G major / E minor**
- markers: Intro · Solo · Chorus · Solo · Chorus · Outro (beats 0/32/64/96/160/224)
- chord windows: Em · D · G · Am9 · C · A7sus4/G · Dadd9 · D5/G · C#dim/E · A#sus2 …

## 3. Ground, then build on top

Keep the converted track as the bed and layer with `set_track_instrument`:

1. Formalize the markers as ChordSmith sections (`set_song_sections`) so per-section levels
   and mutes work; then mute/keep/gain the reference track as wanted. Skip this when the file
   carries expressive tempo automation you want to keep — `set_song_sections` replaces the
   tempo map with one tempo per section.
2. Add parts as usual: MODO DRUM / MODO BASS 2, sfizz instruments, synth layers (Surge XT),
   captured TONEX/Guitar Rig states — `copy_from` duplicates the reference's phrases for a new
   layer (e.g. an octave-up doubling or a synth pad following the guitar).
3. Render with `stems: true` and balance with the measured per-stem peaks.

## 4. Tempo sanity (do this before building)

MuseScore writes the tab's own tempo automation into the MIDI, and tab authors often encode
the original performance's rubato as stepped tempo events. Hanezeve carried a "loose belt"
pattern — 80 → 94 → 80 → 84 BPM flutter plus hard 60/57 BPM dips at the chorus phrase ends —
that broke immersion until it was smoothed. Read `tempo_changes` in the import report first:

- Uniform, breathing shifts (e.g. the same rubato every bar) are usually the performance.
- Irregular mid-phrase jumps and hard dips are usually automation artifacts: flatten or ramp
  them (a local mido edit works; keep the section-start tempos and any gentle final rall) and
  re-render — in a sung song the vocal must be re-rendered against the fixed map so it stays
  in sync.
