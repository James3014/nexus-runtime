"""A provider CLI timeout kills the whole process group, not only the direct child.

Nexus-new online A/B 20261013b: ``subprocess.run(timeout=...)`` killed only the agy shim;
the real agy worker (a grandchild) was orphaned and kept its account lease. A harmless
``/bin/sh`` stub stands in for the provider; no real Agy binary or model is invoked.
"""

from __future__ import annotations

import os
import stat
import time

from nexus_runtime import build_runtime_exports

MODEL = "gemini-3.8-flash-medium"


def _context():
    from nexus_runtime.support.services.online_execution_policy import OnlineExecutionDecision

    decision = OnlineExecutionDecision(
        online_policy="require",
        online_execution_requested=True,
        online_execution_authorized=True,
        online_authorization_source="workspace_policy",
        approved_online_providers=("agy",),
        preflight_status="ok",
        requested_provider="agy",
        physical_invocation_allowed=True,
    )
    return {"task_id": "timeout-pg", "online_prompt": "hi", "online_execution_decision": decision}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat an exited-but-unreaped child as dead.
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return waited == 0


def test_provider_timeout_kills_grandchild_worker(tmp_path):
    pidfile = tmp_path / "worker.pid"
    script = tmp_path / "fakeagy.sh"
    script.write_text(
        "#!/bin/sh\n"
        # The "real worker": a grandchild that outlives a kill of this shim.
        f'sleep 60 > /dev/null 2>&1 < /dev/null &\necho $! > "{pidfile}"\n'
        "sleep 60\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    invoker = build_runtime_exports().build_registered_online_invoker(
        "agy",
        environ={"NEXUS_AGY_BIN": str(script)},
        model_name=MODEL,
        timeout_sec=1.0,
        include_local_context=False,
    )
    started = time.monotonic()
    result = invoker(_context())
    assert result["error"] == "provider_timeout"
    assert time.monotonic() - started < 30

    worker_pid = int(pidfile.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 5
    while _alive(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    alive = _alive(worker_pid)
    if alive:
        os.kill(worker_pid, 9)  # do not leak the stub worker from a failing run
    assert not alive, "provider timeout orphaned the grandchild worker"
