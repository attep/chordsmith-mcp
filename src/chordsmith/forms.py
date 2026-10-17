"""Song-form templates: named section maps (bars and tempos) for common structures.

The defaults follow the common published shapes — pop/rock verse-chorus and AABA from
Wikipedia's *Song structure* / *Thirty-two-bar form* and Open Music Theory's pop/rock form
chapter, the 12-bar blues from *Twelve-bar blues*, the EDM phrase grid from *Drop* and
production guides. Cinematic music has no single standard, so its arc (themes building to a
climax) is a production convention. Bar counts are starting points the assistant can adjust;
passing ``bpm`` flattens every section onto one tempo.
"""

from __future__ import annotations

from typing import Literal

from chordsmith.models import SongSection

Form = Literal["pop", "rock", "metal", "ballad", "jazz", "blues", "edm", "cinematic"]


def _flat(bpm: float, *entries: tuple[str, int]) -> list[SongSection]:
    return [SongSection(name=name, bars=bars, bpm=bpm) for name, bars in entries]


SONG_FORMS: dict[str, list[SongSection]] = {
    "pop": _flat(
        110,
        ("Intro", 4),
        ("Verse 1", 8),
        ("Pre-Chorus 1", 4),
        ("Chorus 1", 8),
        ("Verse 2", 8),
        ("Pre-Chorus 2", 4),
        ("Chorus 2", 8),
        ("Bridge", 8),
        ("Chorus 3", 8),
        ("Outro", 4),
    ),
    "rock": _flat(
        130,
        ("Intro", 4),
        ("Verse 1", 8),
        ("Chorus 1", 8),
        ("Verse 2", 8),
        ("Chorus 2", 8),
        ("Solo", 8),
        ("Chorus 3", 8),
        ("Outro", 4),
    ),
    "metal": _flat(
        150,
        ("Intro", 4),
        ("Riff", 8),
        ("Verse 1", 8),
        ("Chorus 1", 8),
        ("Breakdown", 8),
        ("Solo", 8),
        ("Chorus 2", 8),
        ("Outro", 4),
    ),
    "ballad": _flat(
        70,
        ("Intro", 4),
        ("Verse 1", 8),
        ("Pre-Chorus", 4),
        ("Chorus 1", 8),
        ("Verse 2", 8),
        ("Chorus 2", 8),
        ("Bridge", 8),
        ("Chorus 3", 8),
        ("Outro", 4),
    ),
    "jazz": _flat(132, ("A1", 8), ("A2", 8), ("B", 8), ("A3", 8)),
    "blues": _flat(100, ("Blues 1", 12), ("Blues 2", 12), ("Blues 3", 12)),
    "edm": _flat(
        128,
        ("Intro", 16),
        ("Build 1", 8),
        ("Drop 1", 16),
        ("Break", 8),
        ("Build 2", 8),
        ("Drop 2", 16),
        ("Outro", 16),
    ),
    "cinematic": [
        SongSection(name="Intro", bars=8, bpm=60),
        SongSection(name="Theme A", bars=16, bpm=70),
        SongSection(name="Theme B", bars=16, bpm=80),
        SongSection(name="Build", bars=8, bpm=100),
        SongSection(name="Climax", bars=16, bpm=120),
        SongSection(name="Outro", bars=8, bpm=60),
    ],
}

FORM_NAMES = tuple(SONG_FORMS)


def form_sections(form: str, bpm: float | None = None) -> list[SongSection]:
    """The template's sections; ``bpm`` overrides every section's own tempo when given."""
    try:
        sections = SONG_FORMS[form]
    except KeyError:
        raise ValueError(f"Unknown song form '{form}'. Forms: {', '.join(FORM_NAMES)}.") from None
    if bpm is None:
        return list(sections)
    return [section.model_copy(update={"bpm": bpm}) for section in sections]
