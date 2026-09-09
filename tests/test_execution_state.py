import json
from pathlib import Path
from nexus_runtime.execution_state import ExecutionStateStore

def test_atomic_roundtrip_and_restart(tmp_path):
 s=ExecutionStateStore(tmp_path/"state"); assert s.write("t",{"task_id":"t","status":"RUNNING","updated_at":"1"})["status"]=="RUNNING"; assert ExecutionStateStore(tmp_path/"state").read_snapshot("t")["task_id"]=="t"
def test_archive_selection(tmp_path):
 root=tmp_path/"state"; (tmp_path/"nexus-state-archive").mkdir(); (tmp_path/"nexus-state-archive"/"t--attempt-a.json").write_text(json.dumps({"task_id":"t","status":"DONE","updated_at":"2"})); assert ExecutionStateStore(root).read_snapshot("t")["updated_at"]=="2"
def test_corrupt_state_denied(tmp_path):
 p=tmp_path/"state"; p.mkdir(); (p/"t.json").write_text("{"); assert ExecutionStateStore(p).read_snapshot("t")["state_valid"] is False
def test_mutation_is_locked_and_atomic(tmp_path):
 s=ExecutionStateStore(tmp_path/"state"); s.write("t",{"task_id":"t","status":"RUNNING"}); assert s.mutate("t",{"status":"DONE"})["status"]=="DONE"; assert not list((tmp_path/"state").glob("*.tmp"))
