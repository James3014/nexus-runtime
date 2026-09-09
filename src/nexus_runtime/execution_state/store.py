"""Durable JSON execution-state storage with atomic, locked mutation."""
from __future__ import annotations
import fcntl, json, os, tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

class ExecutionStateStore:
    def __init__(self, state_dir: Path, *, validator: Callable[[str, Mapping[str, Any], Path], dict[str, Any] | None] | None = None):
        self.state_dir = Path(state_dir); self.validator = validator or self._validate
    def state_path(self, task_id: str) -> Path: return self.state_dir / f"{task_id}.json"
    def archive_candidates(self, task_id: str) -> list[Path]:
        root = self.state_dir.parent / "nexus-state-archive"
        return [p for p in [root / f"{task_id}.json", *sorted(root.glob(f"{task_id}--attempt-*.json"))] if p.exists()]
    def _load(self, path: Path, task_id: str):
        try: payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError: return None
        except (OSError, json.JSONDecodeError): return {"task_id":task_id,"status":"BLOCKED_INVALID_STATE","state_valid":False,"source_path":str(path)}
        if not isinstance(payload, Mapping): return {"task_id":task_id,"status":"BLOCKED_INVALID_STATE","state_valid":False,"source_path":str(path)}
        value = self.validator(task_id, payload, path)
        return value if value is not None else dict(payload)
    def read_snapshot(self, task_id: str):
        value = self._load(self.state_path(task_id), task_id)
        if value is not None: return value
        values=[]
        for path in self.archive_candidates(task_id):
            loaded = self._load(path, task_id)
            if loaded is not None:
                values.append((str(loaded.get("updated_at") or ""), path.stat().st_mtime_ns, loaded))
        return max(values, key=lambda x:(x[0],x[1]))[2] if values else None
    @contextmanager
    def _lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir/".state.lock").open("a+") as h:
            fcntl.flock(h.fileno(), fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(h.fileno(), fcntl.LOCK_UN)
    def write(self, task_id: str, state: Mapping[str, Any]):
        normalized=json.loads(json.dumps(dict(state), default=str)); self.state_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=self.state_dir,prefix=f".{task_id}.",suffix=".tmp",delete=False) as h:
            json.dump(normalized,h,sort_keys=True,indent=2); h.write("\n"); h.flush(); os.fsync(h.fileno()); tmp=Path(h.name)
        tmp.replace(self.state_path(task_id)); fd=os.open(self.state_dir,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        return normalized
    def mutate(self, task_id: str, update: Mapping[str, Any] | Callable[[dict[str, Any]], Mapping[str, Any]]):
        with self._lock():
            current=self.read_snapshot(task_id) or {"task_id":task_id}
            value=update(dict(current)) if callable(update) else {**current,**dict(update)}
            return self.write(task_id,value)
    @staticmethod
    def _validate(task_id, payload, path):
        if payload.get("task_id") != task_id or not isinstance(payload.get("status"),str) or not payload["status"].strip():
            return {"task_id":task_id,"status":"BLOCKED_INVALID_STATE","state_valid":False,"source_path":str(path)}
        return dict(payload)
