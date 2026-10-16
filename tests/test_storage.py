import json

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


# ------------------------------------------------------------------ projects


def test_project_paths_create_the_folder_and_manifest(tmp_path):
    fs = FileStore(tmp_path / "out")
    path = fs.new_path("my_song/verse", "x")
    assert path == fs.ensure_root() / "my_song" / "verse.mid"
    manifest = json.loads((path.parent / "project.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "my_song" and manifest["created"] and manifest["updated"]
    path.write_bytes(b"x")
    assert fs.relative_name(path) == "my_song/verse.mid"
    assert fs.project_of(path) == "my_song"
    assert fs.project_of_name("my_song/verse.mid") == "my_song"
    assert fs.project_of_name("verse.mid") is None
    assert fs.existing_path("my_song/verse").read_bytes() == b"x"


def test_project_names_are_validated(tmp_path):
    fs = FileStore(tmp_path / "out")
    with pytest.raises(StorageError, match="reserved"):
        fs.new_path("states/notes", "x")  # the plugin state library is not a project
    with pytest.raises(StorageError, match="one folder deep"):
        fs.new_path("a/b/c", "x")
    with pytest.raises(StorageError, match="usable project name"):
        fs.new_path("../escape", "x")
    with pytest.raises(StorageError, match="one folder deep"):
        fs.new_path("my song//verse", "x")
    with pytest.raises(StorageError, match="reserved"):
        FileStore.safe_project("CON")


def test_rename_stays_in_the_project_and_can_move(tmp_path):
    fs = FileStore(tmp_path / "out")
    first = fs.new_path("song", "x")
    first.write_bytes(b"x")
    moved = fs.rename("song.mid", "other_song/verse")
    assert fs.relative_name(moved) == "other_song/verse.mid"
    assert not first.exists()
    renamed = fs.rename("other_song/verse.mid", "chorus")
    assert fs.relative_name(renamed) == "other_song/chorus.mid"  # keeps the folder
    fs.new_path("other_song/verse", "x").write_bytes(b"y")
    with pytest.raises(StorageError, match="already exists"):
        fs.rename("other_song/chorus.mid", "other_song/verse")


def test_list_files_filters_projects(tmp_path):
    fs = FileStore(tmp_path / "out")
    (fs.ensure_root() / "loose.mid").write_bytes(b"x")
    fs.new_path("alpha/one", "x").write_bytes(b"x")
    fs.new_path("beta/two", "x").write_bytes(b"x")
    assert {entry["filename"] for entry in fs.list_files()} == {
        "loose.mid",
        "alpha/one.mid",
        "beta/two.mid",
    }
    assert {entry["project"] for entry in fs.list_files()} == {None, "alpha", "beta"}
    assert [entry["filename"] for entry in fs.list_files("alpha")] == ["alpha/one.mid"]
    assert [entry["filename"] for entry in fs.list_files(".")] == ["loose.mid"]
    projects = {entry["name"]: entry for entry in fs.list_projects()}
    assert set(projects) == {"alpha", "beta"}
    assert projects["alpha"]["files"] == 1 and projects["alpha"]["midi"] == 1
    assert projects["alpha"]["audio"] == 0 and projects["alpha"]["created"]


def test_remove_empty_media_sweeps_projects(tmp_path):
    fs = FileStore(tmp_path / "out")
    (fs.ensure_root() / "empty.mid").write_bytes(b"")
    fs.new_path("proj/empty", "x").write_bytes(b"")
    kept = fs.new_path("proj/song", "x")
    kept.write_bytes(b"x")
    removed = fs.remove_empty_media()
    assert set(removed) == {"empty.mid", "proj/empty.mid"}
    assert kept.is_file()
