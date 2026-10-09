import pytest

from chordsmith.storage import FileStore, StorageError


def test_safe_name():
    assert FileStore.safe_name("my song") == "my_song.mid"
    assert FileStore.safe_name("../../etc/passwd") == "passwd.mid"
    assert FileStore.safe_name("tune.MIDI") == "tune.mid"
    with pytest.raises(StorageError):
        FileStore.safe_name("../..")


def test_new_path_does_not_overwrite(tmp_path):
    fs = FileStore(tmp_path)
    first = fs.new_path("a", "x")
    first.write_bytes(b"x")
    assert fs.new_path("a", "x").name == "a_2.mid"
    assert fs.new_path("a", "x", overwrite=True) == first


def test_existing_path_stays_inside(tmp_path):
    fs = FileStore(tmp_path / "out")
    (tmp_path / "secret.mid").write_bytes(b"x")
    with pytest.raises(StorageError):
        fs.existing_path("../secret.mid")
