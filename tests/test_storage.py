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


def test_new_path_claims_the_name_atomically(tmp_path):
    # two writers that have not written yet must never get the same path (the render-job race)
    fs = FileStore(tmp_path)
    first = fs.new_path("race", "x")
    second = fs.new_path("race", "x")
    assert first != second
    assert first.name == "race.mid" and second.name == "race_2.mid"


def test_new_path_claim_is_thread_safe(tmp_path):
    import threading

    fs = FileStore(tmp_path)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def claim() -> None:
        barrier.wait()
        results.append(fs.new_path("storm", "x").name)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 8  # every thread got its own name


def test_claimed_path_cleans_up_on_failure(tmp_path):
    fs = FileStore(tmp_path)
    with pytest.raises(RuntimeError):
        with fs.claimed_path("oops", "x") as path:
            raise RuntimeError("render failed")
    assert not path.exists()  # the placeholder is removed again

    with fs.claimed_path("fine", "x") as path:
        path.write_bytes(b"ok")
    assert path.exists()


def test_existing_path_stays_inside(tmp_path):
    fs = FileStore(tmp_path / "out")
    (tmp_path / "secret.mid").write_bytes(b"x")
    with pytest.raises(StorageError):
        fs.existing_path("../secret.mid")
