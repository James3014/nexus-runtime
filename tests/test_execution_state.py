import json

from nexus_runtime.execution_state import ExecutionStateStore


def test_atomic_roundtrip_and_restart(tmp_path):
    s = ExecutionStateStore(tmp_path / "state")
    assert (
        s.write("t", {"task_id": "t", "status": "RUNNING", "updated_at": "1"})["status"]
        == "RUNNING"
    )
    assert ExecutionStateStore(tmp_path / "state").read_snapshot("t")["task_id"] == "t"


def test_archive_selection(tmp_path):
    root = tmp_path / "state"
    (tmp_path / "nexus-state-archive").mkdir()
    (tmp_path / "nexus-state-archive" / "t--attempt-a.json").write_text(
        json.dumps({"task_id": "t", "status": "DONE", "updated_at": "2"})
    )
    assert ExecutionStateStore(root).read_snapshot("t")["updated_at"] == "2"


def test_corrupt_state_denied(tmp_path):
    p = tmp_path / "state"
    p.mkdir()
    (p / "t.json").write_text("{")
    assert ExecutionStateStore(p).read_snapshot("t")["state_valid"] is False


def test_mutation_is_locked_and_atomic(tmp_path):
    s = ExecutionStateStore(tmp_path / "state")
    s.write("t", {"task_id": "t", "status": "RUNNING"})
    assert s.mutate("t", {"status": "DONE"})["status"] == "DONE"
    assert not list((tmp_path / "state").glob("*.tmp"))


def test_mutation_missing_active_does_not_restore_archive(tmp_path):
    root = tmp_path / "state"
    archive = tmp_path / "nexus-state-archive"
    archive.mkdir()
    (archive / "t.json").write_text(json.dumps({"task_id": "t", "status": "DONE"}))
    assert ExecutionStateStore(root).mutate("t", {"status": "NEW"}) is None


def test_corrupt_mutation_denies_without_replacing_file(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    path = root / "t.json"
    path.write_text("{")
    try:
        ExecutionStateStore(root).mutate("t", {"status": "DONE"})
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("corrupt state must deny")
    assert path.read_text() == "{"


def test_mutate_uses_in_place_callback(tmp_path):
    s = ExecutionStateStore(tmp_path / "state")
    s.write("t", {"task_id": "t", "status": "A"})
    s.mutate("t", lambda value: value.update(status="B"))
    assert s.read_snapshot("t")["status"] == "B"


def test_mutator_return_value_is_rejected(tmp_path):
    s = ExecutionStateStore(tmp_path / "state")
    s.write("t", {"task_id": "t", "status": "A"})
    try:
        s.mutate("t", lambda value: {"status": "B"})
    except TypeError:
        pass
    else:
        raise AssertionError("returning replacement must be rejected")


def test_before_write_rejection_has_no_effect(tmp_path):
    def reject(task_id, value):
        raise RuntimeError("owner denied")

    root = tmp_path / "state"
    s = ExecutionStateStore(root, before_write=reject)
    try:
        s.write("t", {"task_id": "t", "status": "A"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("callback rejection must propagate")
    assert not (root / "t.json").exists()


def test_concurrent_mutations_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    s = ExecutionStateStore(tmp_path / "state")
    s.write("t", {"task_id": "t", "status": "A", "count": 0})

    def bump(_):
        s.mutate("t", lambda value: value.update(count=value["count"] + 1))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(bump, range(20)))
    assert s.read_snapshot("t")["count"] == 20


def test_locked_api_does_not_reenter_flock(tmp_path):
    store = ExecutionStateStore(tmp_path / "state")
    with store.lock():

        def denied():
            raise AssertionError(
                "already-locked operations must not acquire a second lock"
            )

        store.lock = denied
        store.write_locked("t", {"task_id": "t", "status": "RUNNING"})
        store.mutate_locked("t", lambda state: state.update(status="DONE"))
    assert store.read_snapshot("t")["status"] == "DONE"
