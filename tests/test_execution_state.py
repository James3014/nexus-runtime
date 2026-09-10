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


def test_invalid_utf8_current_state_returns_error_receipt(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    path = root / "t.json"
    bad_bytes = b"\xff\xfe\x00\x00"
    path.write_bytes(bad_bytes)
    store = ExecutionStateStore(root)
    receipt = store.read_snapshot("t")
    assert receipt["task_id"] == "t"
    assert receipt["status"] == "BLOCKED_INVALID_STATE"
    assert receipt["state_valid"] is False
    assert receipt["source_path"] == str(path)
    assert receipt["error"] == "UnicodeDecodeError"
    assert path.read_bytes() == bad_bytes
    try:
        store.mutate("t", {"status": "NEW"})
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("strict mutate must raise UnicodeDecodeError")
    assert path.read_bytes() == bad_bytes


def test_invalid_utf8_archive_returns_error_receipt(tmp_path):
    root = tmp_path / "state"
    archive = tmp_path / "nexus-state-archive"
    archive.mkdir()
    archive_file = archive / "t.json"
    bad_bytes = b"\xff\xfe"
    archive_file.write_bytes(bad_bytes)
    store = ExecutionStateStore(root)
    receipt = store.read_snapshot("t")
    assert receipt["task_id"] == "t"
    assert receipt["status"] == "BLOCKED_INVALID_STATE"
    assert receipt["state_valid"] is False
    assert receipt["source_path"] == str(archive_file)
    assert receipt["error"] == "UnicodeDecodeError"
    assert archive_file.read_bytes() == bad_bytes


def test_valid_unicode_json_roundtrip(tmp_path):
    store = ExecutionStateStore(tmp_path / "state")
    data = {
        "task_id": "unicode_task",
        "status": "RUNNING",
        "title": "繁體中文測試 🚀",
        "metadata": {"tags": ["測試", "unicode"]},
    }
    store.write("unicode_task", data)
    snapshot = store.read_snapshot("unicode_task")
    assert snapshot["title"] == "繁體中文測試 🚀"
    assert snapshot["metadata"]["tags"] == ["測試", "unicode"]
    assert snapshot.get("state_valid") is not False


def test_missing_file_returns_none(tmp_path):
    store = ExecutionStateStore(tmp_path / "state")
    assert store.read_snapshot("missing") is None
    assert store.load_path(store.state_path("missing"), "missing") is None
    assert store.mutate("missing", {"status": "X"}) is None


def test_syntax_corrupt_json_semantics(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    path = root / "t.json"
    raw_corrupt = '{"unclosed": '
    path.write_text(raw_corrupt, encoding="utf-8")
    store = ExecutionStateStore(root)
    receipt = store.read_snapshot("t")
    assert receipt["task_id"] == "t"
    assert receipt["status"] == "BLOCKED_INVALID_STATE"
    assert receipt["state_valid"] is False
    assert receipt["error"] == "JSONDecodeError"
    assert receipt["source_path"] == str(path)
    assert path.read_text(encoding="utf-8") == raw_corrupt


def test_non_object_json_semantics(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    path = root / "t.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    store = ExecutionStateStore(root)
    receipt = store.read_snapshot("t")
    assert receipt["task_id"] == "t"
    assert receipt["status"] == "BLOCKED_INVALID_STATE"
    assert receipt["state_valid"] is False
    assert receipt["error"] == "ValueError"
    assert receipt["source_path"] == str(path)


def test_corrupt_current_state_not_hidden_by_valid_archive(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    current = root / "t.json"
    bad_bytes = b"\x80\x81\x82"
    current.write_bytes(bad_bytes)

    archive = tmp_path / "nexus-state-archive"
    archive.mkdir()
    (archive / "t--attempt-1.json").write_text(
        json.dumps({"task_id": "t", "status": "SUCCEEDED", "updated_at": "999"})
    )

    store = ExecutionStateStore(root)
    receipt = store.read_snapshot("t")
    assert receipt["state_valid"] is False
    assert receipt["status"] == "BLOCKED_INVALID_STATE"
    assert receipt["error"] == "UnicodeDecodeError"
    assert receipt["source_path"] == str(current)
    assert current.read_bytes() == bad_bytes


def test_custom_error_receipt_callback(tmp_path):
    custom_calls = []

    def custom_receipt(task_id, path, error):
        custom_calls.append((task_id, str(path), type(error).__name__))
        return {
            "custom": True,
            "task_id": task_id,
            "error_type": type(error).__name__,
            "state_valid": False,
        }

    root = tmp_path / "state"
    root.mkdir()
    (root / "t.json").write_bytes(b"\xfe\xff")

    store = ExecutionStateStore(root, error_receipt=custom_receipt)
    res = store.read_snapshot("t")
    assert res["custom"] is True
    assert res["error_type"] == "UnicodeDecodeError"
    assert len(custom_calls) == 1
    assert custom_calls[0][0] == "t"
    assert custom_calls[0][2] == "UnicodeDecodeError"
