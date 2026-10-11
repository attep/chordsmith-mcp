import pytest

from chordsmith import server
from chordsmith.storage import FileStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    fs = FileStore(tmp_path / "out")
    monkeypatch.setattr(server, "store", fs)
    return fs


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Keep every test's persisted registry out of the real state directory."""
    monkeypatch.setenv("CHORDSMITH_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"
