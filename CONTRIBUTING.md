# Contributing to ChordSmith

Thanks for helping! This project is an MCP server that turns musical choices into MIDI and
audio files — deterministic, tested and documented, with no AI calls inside the server.

## Setup

```bash
git clone https://github.com/attep/chordsmith-mcp.git
cd chordsmith-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Everyday commands

```bash
pytest -q                   # the full suite (some tests skip without ffmpeg/fluidsynth)
ruff check src tests        # lint
ruff format src tests       # format
mypy                        # typing for the curated module set (pyproject.toml)
```

Optional: `pip install pre-commit && pre-commit install` runs ruff and mypy on every commit.

## What CI checks

GitHub Actions runs lint, format check and the full test suite on Python 3.10 and 3.12 on tags
and pull requests, plus a dependency audit and the VST host build (Linux and Windows).

## Guidelines

- **Keep the LLM out of the server.** Music decisions come from the assistant; the server
  validates them and writes files deterministically. The same input must produce the same bytes.
- **Every tool needs**: a title, MCP annotations, a description that says what it returns, an
  input schema with every option described and defaulted, and a typed output schema in
  `results.py` (`tests/test_tool_docs.py` enforces this and budgets the tools/list payload).
- **Tests first for behaviour**: golden-file tests exist for note output; add a regression test
  with every bug fix, and an end-to-end tool test for new tools.
- **Docs move with the code**: a new tool or option updates `docs/tools.md`, the README table
  and `skills/chordsmith/SKILL.md`; user-visible changes get a `CHANGELOG.md` entry.
- **Never bundle third-party assets**: voicebanks, soundfonts, SFZ libraries and plugins are
  user-supplied and not committed. Keep licence notes accurate.
- **Versioning**: semver; features bump the minor, fixes the patch. Releases are tags (`v*`)
  and the CI release job builds the host binaries.

## Reporting problems

Open an issue with the tool call (options included), the result or error text, and what you
expected. For security issues, follow [SECURITY.md](SECURITY.md) instead of opening an issue.
