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


class SegmentEntry(TypedDict):
    start_beat: float
    beats: float
    chord: str
    notes: list[str]


class AnalyzeResult(TypedDict):
    tempo_bpm: float
    time_signature: str
    markers: list[MarkerEntry]
    ticks_per_beat: int
    total_beats: float
    window_beats: float
    key_signature_guess: str | None
    progression: list[str]
    segments: list[SegmentEntry]


class FileEntry(TypedDict):
    filename: str
    uri: str
    size_bytes: int
    modified: str


class ListFilesResult(TypedDict):
    output_dir: str
    files: list[FileEntry]


class AddTrackResult(TypedDict):
    source: str
    filename: str
    path: str
    uri: str
    track_name: str
    channel: int
    notes_added: int
    total_tracks: int
    track_index: int


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


class RenderAudioResult(_FileIdentity, DeliveryFields, total=False):
    source: str | None
    duration_seconds: float | None


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
    error: str | None


class MixLevels(TypedDict):
    vocal_level_db: float
    backing_volume: float
    normalize_peak_db: float | None


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
    vocal_to_backing_measured_db: float | None
    balance_check: str
