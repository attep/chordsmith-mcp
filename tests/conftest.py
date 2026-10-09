import pytest

from chordsmith import server
from chordsmith.storage import FileStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    fs = FileStore(tmp_path / "out")
    monkeypatch.setattr(server, "store", fs)
    return fs
