# nexus-runtime

Independent source package for task planning/admission, execution coordination,
retry/replan state, writer/event boundaries, and durable context/state.

The runtime is assembled from explicit typed bindings. Core, Learning, Open SWE,
and Repository Intelligence remain external package owners. No provider, host
service, deployment target, or native backend is selected implicitly.

## Current source and evidence boundary

The historical extraction/source-bound baseline is
`a3fd8006d61d4eb14637050c7da5a95e5e28d157`. It is preserved as donor/extraction
provenance, not as the current repository identity.

The repository default branch is `codex/repo-split`. Live repository HEAD is
intentionally not hard-coded here; read Git/GitHub when current revision identity
matters.

The current Python source-ownership snapshot is bound to
`67a37c61561d3608ff622f33bdfbd4c0c7423ecb`. That SHA is a source-scope anchor for
the `src/**/*.py` hashes in `docs/current-source-ownership.json`, not a standing
claim about the live repository HEAD. Later non-`src` commits do not invalidate
the snapshot; any `src` mutation requires refreshing the ownership map.

The accepted runtime source line from PR #10 was merged at
`39515b73a60fdf6322ee7e48a9f87ef681f46a26`, preserving accepted tree
`03f2a6197c1fded3b8c17b6b8db41ef88f7a01b0`. Later Planner-binding and
compatibility-source changes are represented in the current ownership snapshot;
none of these source facts imply deployment or runtime activation.

Post-merge hosted standalone wheel and runtime test runs succeeded for the
accepted PR #10 source line. Nexus-new consumer PR #910 subsequently merged its
standalone-runtime integration. These are source/package/consumer facts only:
they do not by themselves prove installation on a particular host, loaded
service identity, release, deployment, or production readiness.

See:

- `docs/EXTRACTION_STATUS.md`
- `docs/current-source-ownership.json`
- `docs/runtime-source-manifest.json`

Always keep repository source, accepted package/pin, installed artifact, loaded
service, and runtime/canary acceptance as separate evidence clocks.

## Ownership boundary

`nexus-runtime` owns the independent runtime/package contracts for:

- planning/admission composition;
- execution coordination;
- retry/replan state;
- writer/event and effect boundaries;
- durable execution/context state;
- explicit host/backend composition ports.

It does **not** implicitly own or select:

- Nexus Core Evidence Trust or Completion Certification;
- Nexus Learning policy authority;
- Open SWE execution implementation;
- Repository Intelligence decisions;
- provider/model availability;
- arbitrary production host activation;
- protected merge, release, or deployment authority.

The package still contains planner/admission implementation lineage required by
accepted callers. Planner algorithm-source convergence across repositories is a
separate ownership task; this repository must not be treated as permission to
create a second/third route authority or to remove compatibility code without
caller evidence.

## Local usage

Build and install the wheel with an explicit local `nexus-learning` wheel, then
assemble the runtime through explicit public contracts:

```console
/usr/bin/python3 -m venv .venv
.venv/bin/python -m pip install /path/to/nexus_learning-0.1.0-py3-none-any.whl
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
.venv/bin/python -m pip install dist/nexus_runtime-0.1.0.dev0-py3-none-any.whl
.venv/bin/python examples/local_run.py
```

```python
from nexus_runtime import ContextHub, ContextHubDependencies, build_runtime_exports
from nexus_runtime.execution_state import ExecutionStateStore
from nexus_runtime.execution_coordination import ExecutionCoordinator
from nexus_runtime.execution_coordination.ports import (
    ExecutionStatePort,
    ExecutionContractPort,
    WorkerAdapterPort,
    TargetExecutionPort,
    ProcessOwnershipPort,
    ExecutionFinalizationPort,
)
from nexus_runtime_support_candidate import build_memory_retrieval_adapter

exports = build_runtime_exports()
memory = build_memory_retrieval_adapter("/path/to/project")
invoker = exports.build_local_memory_capability_invoker(
    "/path/to/project", adapter=memory
)
```

`ContextHub` and `ContextHubDependencies` are public assembly contracts. Core,
Learning, Open SWE, Repository Intelligence, and host backends remain explicitly
injected owners. The runtime does not discover providers, deploy services, or
create external stores implicitly.

`ExecutionStateStore` owns durable JSON state reads, atomic writes, and archive
selection. `ExecutionCoordinator` accepts explicit state, contract, worker,
target, process-ownership, and finalization effect ports; host code owns provider
calls, worktrees, and candidate finalization.

For a deterministic end-to-end run/retry/readback example, execute:

```console
python -m pytest -q \
  tests/test_runtime_operations.py::test_real_planner_run_and_replan_successful_receipts
```

The test uses the real Planner/admission composition, writes a temporary
candidate, verifies its bytes, retries once, and checks receipt lineage without
contacting a provider.

`nexus_runtime_candidate` remains a compatibility namespace for older callers;
the public composition binds the current `nexus_runtime_p6c_candidate`
implementation. No current source should create a second algorithm merely to
replace that compatibility name.

## Owner workflow integration

`tests/integration/test_owner_workflow.py` is the deterministic owner integration
fixture: a deterministic OpenSWE graph writes an artifact, Repository
Intelligence analyzes the changed file, Runtime emits/reads a receipt, Core
certifies hashes from that artifact, and Learning projects the receipt.

Run it with the owner wheels installed:

```console
.venv/bin/python -I -m pytest -q tests/integration/test_owner_workflow.py
```

The test is skipped when optional owner packages are absent. Acceptance requires
the command to run unskipped in the intended owner-package environment; a skip
must not be reported as owner-integration PASS. Provider-backed execution and
loaded-host acceptance remain separate gates.

## Historical delivery evidence

Earlier caller/worktree artifacts and `/private/tmp/...` XML results remain
historical engineering evidence. They are not current physical witnesses merely
because their paths appear in documentation. Use current Git/source/package and
runtime readback for current claims.
