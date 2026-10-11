"""Typed tool results.

These TypedDicts are the tools' return annotations; the MCP SDK turns them into each tool's
``outputSchema`` and validates the ``structuredContent`` of every result against it, so agents
get machine-checkable results instead of loose dictionaries.
"""

from __future__ import annotations

from typing_extensions import TypedDict


class ChordSummary(TypedDict):
    chord: str
    beats: float
    notes: list[str]


class ProgressionResult(TypedDict):
    filename: str
    path: str
    uri: str
    tempo: float
    time_signature: str
    midi_type: int
    total_beats: float
    bars: float
    duration_seconds: float
    chords: list[ChordSummary]


class RomanProgressionResult(ProgressionResult):
    key: str
    numerals: list[str]
    resolved_chords: list[str]


class ChordTypeRow(TypedDict):
    quality: str
    symbols: list[str]
    example: str
    intervals_semitones: list[int]
    notes_in_C: list[str]
    description: str


class TransposeResult(TypedDict):
    source: str
    filename: str
    path: str
    uri: str
    semitones: int
    notes_transposed: int


class MarkerEntry(TypedDict):
    beat: float
    text: str


class TempoChangeEntry(TypedDict):
    beat: float
    bpm: float


class TimeSignatureChangeEntry(TypedDict):
    beat: float
    time_signature: str


class SegmentEntry(TypedDict):
    start_beat: float
    beats: float
    chord: str
    notes: list[str]


class AnalyzeResult(TypedDict):
    tempo_bpm: float
    time_signature: str
    tempo_changes: list[TempoChangeEntry]
    time_signature_changes: list[TimeSignatureChangeEntry]
    markers: list[MarkerEntry]
    sections: list[SectionEntry]
    ticks_per_beat: int
    total_beats: float
    window_beats: float
    key_signature_guess: str | None
    progression: list[str]
    segments: list[SegmentEntry]


class FileEntry(TypedDict):
    filename: str
    uri: str
    kind: str
    size_bytes: int
    empty: bool
    modified: str


class ListFilesResult(TypedDict):
    output_dir: str
    total: int
    files: list[FileEntry]


class AddTrackResult(TypedDict):
    source: str
    filename: str
    path: str
    uri: str
    track_name: str
    channel: int
    notes_added: int
    automation_events: int
    total_tracks: int
    track_index: int


class EmptyFileResult(TypedDict):
    filename: str
    path: str
    uri: str
    midi_type: int
    total_tracks: int
    sections: list[SectionEntry]


class RemoveTrackResult(TypedDict):
    source: str
    filename: str
    path: str
    uri: str
    removed: str
    total_tracks: int


class DeleteResult(TypedDict):
    deleted: str


class RenameResult(TypedDict):
    filename: str
    path: str
    uri: str


class DeliveryFields(TypedDict, total=False):
    # absent fields are reported as null in structuredContent (the SDK dumps every declared
    # field), so optional fields must be nullable to stay valid against the output schema
    data_base64: str | None
    download_url: str | None
    expires_at: int | None


class _FileIdentity(TypedDict):
    filename: str
    size_bytes: int
    sha256: str
    mime_type: str


class GetMidiFileResult(_FileIdentity, DeliveryFields):
    pass


class StemEntry(_FileIdentity, DeliveryFields):
    track: str
    gain_db: float
    peak_db: float


class ReverbEntry(TypedDict):
    room_size: float
    damping: float
    width: float
    level: float


class ChorusEntry(TypedDict):
    voices: int
    level: float
    speed: float
    depth: float


class EffectsEntry(TypedDict):
    reverb: ReverbEntry | None
    chorus: ChorusEntry | None


class FailedTrack(TypedDict):
    track: str
    error: str


class RenderAudioResult(_FileIdentity, DeliveryFields, total=False):
    source: str | None
    duration_seconds: float | None
    peak_db: float | None
    gain_correction_db: float
    clipping: bool
    effects: EffectsEntry | None
    failed: list[FailedTrack] | None
    stems: list[StemEntry] | None


class RenderJobTrack(TypedDict):
    track: str
    state: str
    error: str | None


class RenderJobHandle(TypedDict):
    job_id: str
    status: str
    tracks: list[str] | None


class RenderJobResult(TypedDict):
    job_id: str
    status: str
    progress: list[RenderJobTrack] | None
    result: RenderAudioResult | None
    error: str | None


class RenderJobEntry(TypedDict):
    job_id: str
    status: str
    created_at: str
    tracks: list[str] | None
    error: str | None


class RenderJobsResult(TypedDict):
    jobs: list[RenderJobEntry]


class EngineEntry(TypedDict):
    name: str
    available: bool
    default_soundfont: str | None
    notes: str


class SectionOverride(TypedDict):
    preset: str | None
    bank: int | None
    program: int | None
    gain_db: float | None


class InstrumentTrack(TypedDict):
    engine: str
    preset: str | None
    bank: int | None
    program: int | None
    sections: dict[str, SectionOverride] | None
    reverb_send: int | None
    chorus_send: int | None
    gain_db: float
    mute: bool


class ProgramCatalog(TypedDict):
    melodic: list[str]
    drums: dict[str, int]


class PresetEntry(TypedDict):
    name: str
    bank: int
    program: int


class SoundfontInventory(TypedDict):
    path: str
    presets: list[PresetEntry]


class EffectiveInstrument(TypedDict):
    channel: int
    bank: int
    program: int
    label: str | None
    source: str
    soundfont: str | None
    warning: str | None


class InstrumentsFile(TypedDict):
    filename: str
    tracks: list[str]
    specs: dict[str, InstrumentTrack]
    resolved: dict[str, str | None]
    effective: dict[str, EffectiveInstrument]


class InstrumentsResult(TypedDict):
    engines: list[EngineEntry]
    soundfont: SoundfontInventory | None
    programs: ProgramCatalog | None
    file: InstrumentsFile | None


class InstrumentMapResult(TypedDict):
    filename: str
    path: str
    tracks: dict[str, InstrumentTrack]
    resolved: dict[str, str | None]
    effective: dict[str, EffectiveInstrument]


class BakeResult(TypedDict):
    source: str
    filename: str
    path: str
    uri: str
    tracks: dict[str, InstrumentTrack]
    warnings: list[str]


class SectionEntry(TypedDict):
    name: str
    start_beat: float
    bars: int
    bpm: float
    numerator: int
    denominator: int
    grouping: str | None


class SectionsResult(TypedDict):
    filename: str
    path: str
    sections: list[SectionEntry]
    total_beats: float
    duration_seconds: float


class TrackReport(TypedDict):
    index: int
    name: str
    channel: int | None
    program: int | None
    notes: int


class ImportResult(TypedDict):
    filename: str
    path: str
    uri: str
    size_bytes: int
    sha256: str
    mime_type: str
    midi_type: int
    ticks_per_beat: int
    duration_seconds: float
    total_notes: int
    tracks: list[TrackReport]
    tempo_changes: list[TempoChangeEntry]
    time_signature_changes: list[TimeSignatureChangeEntry]
    markers: list[MarkerEntry]
    sysex_events: int
    warnings: list[str]


class GetFileResult(_FileIdentity, DeliveryFields):
    mime_type: str


class ImportAudioResult(TypedDict):
    filename: str
    path: str
    uri: str
    size_bytes: int
    sha256: str
    mime_type: str
    format: str
    duration_seconds: float
    sample_rate: int
    channels: int
    peak_db: float | None


class ExportEntry(_FileIdentity, DeliveryFields):
    pass


class ExportResult(TypedDict):
    mix: ExportEntry
    mix_mp3: ExportEntry
    vocal: ExportEntry
    midi: ExportEntry


# ------------------------------------------------------------------ singing


class _VoiceBase(TypedDict):
    voice_id: str
    name: str
    engine: str
    language: str


class VoiceEntry(_VoiceBase, total=False):
    style_type: str | None
    query_via_teacher: bool | None
    licence: str | None
    credit: str | None
    commercial_status: str | None
    soft_controls: dict[str, bool] | None


class EngineVoices(TypedDict, total=False):
    engine: str | None
    engine_url: str | None
    configured: bool | None
    voicebank: str | None
    credit: str | None
    licence: str | None
    commercial_status: str | None
    voices: list[VoiceEntry] | None


class VoicesResult(EngineVoices, total=False):
    voicevox: EngineVoices | None
    diffsinger: EngineVoices | None


class TrackInfo(TypedDict):
    index: int
    name: str


class ScoreNoteEntry(TypedDict, total=False):
    note_id: int
    kind: str
    pitch: int | None
    name: str | None
    start_beat: float
    beats: float
    start_seconds: float
    seconds: float
    frames: int | None


class ScoreResult(TypedDict):
    score_id: str
    source: str
    track: TrackInfo
    tempo_bpm: float
    tempo_changes: list[TempoChangeEntry]
    sections: list[SectionEntry]
    transpose: int
    total_seconds: float
    warnings: list[str]
    notes: list[ScoreNoteEntry]


class MappingNoteEntry(TypedDict, total=False):
    note_id: int
    kind: str
    pitch: int | None
    lyric: str
    phonemes: list[str] | None
    start_seconds: float
    seconds: float
    frames: int | None


class MappingResult(TypedDict):
    mapping_id: str
    score_id: str
    version: int
    wordless: bool
    voice_hint: str
    duration_seconds: float
    warnings: list[str]
    notes: list[MappingNoteEntry]


class RenderStartResult(TypedDict):
    job_id: str
    status: str
    reused: bool


class _JobBase(TypedDict):
    job_id: str
    status: str
    voice_id: str
    settings: dict[str, float | int | None]


class JobResult(_JobBase, total=False):
    filename: str | None
    path: str | None
    uri: str | None
    mime_type: str | None
    sample_rate: int | None
    duration_seconds: float | None
    start_offset_seconds: float | None
    mapping_id: str | None
    query_voice_id: str | None
    mode: str | None
    frames: int | None
    peak_db: float | None
    source: str | None
    created_at: str | None
    progress: str | None
    error: str | None


class SingingJobEntry(TypedDict):
    job_id: str
    status: str
    source: str | None
    voice_id: str
    created_at: str
    progress: str | None
    error: str | None


class SingingJobsResult(TypedDict):
    jobs: list[SingingJobEntry]


class MixLevels(TypedDict):
    vocal_level_db: float
    backing_volume: float
    normalize_peak_db: float | None
    duck_db: float | None


class LoudnessInfo(TypedDict, total=False):
    integrated_lufs: float | None
    loudness_range_lu: float | None
    true_peak_db: float | None
    short_term_max_lufs: float | None


class MixProcessing(TypedDict, total=False):
    """The staged vocal pipeline's effective settings and measurements."""

    chain_source: str
    vocal_chain: dict | None
    vocal_sends: dict | None
    vocal_bus: dict | None
    returns: dict | None
    master: dict | None
    gain_reduction_db: dict[str, float]
    loudness: LoudnessInfo | None
    limiter_reduction_db: float | None
    resolved_delay_ms: float | None


class MixResult(TypedDict):
    filename: str
    path: str
    uri: str
    mime_type: str
    source: str
    vocal: str
    backing: str
    levels: MixLevels
    backing_levels: dict[str, float] | None
    instruments: dict[str, InstrumentTrack] | None
    effects: EffectsEntry | None
    vocal_segments: list[dict] | None
    compress: bool | None
    ducking: bool | None
    guide_removed: str
    peak_db: float
    clipping: bool
    gain_correction_db: float
    normalize_peak_db: float | None
    duration_seconds: float
    backing_rms_db: float
    vocal_rms_db: float
    vocal_gain_db: float
    vocal_to_backing_db: float
    vocal_to_original_backing_db: float
    vocal_to_backing_measured_db: float | None
    balance_check: str
    processing: MixProcessing | None
    warnings: list[str]
