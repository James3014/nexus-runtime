# nexus-runtime

Independent source package for task planning/admission, execution coordination,
retry/replan state, writer/event boundaries, and durable context storage.

The runtime is assembled from explicit typed bindings. Core contracts, learning,
Open SWE execution, and repository intelligence remain external package owners.
No provider or native service is selected implicitly.

The reviewed source is frozen at `ced4da6354a11187498dea0fa58d920a8e90e3c1`.
Historical donor lineage remains recorded per file; the integrated execution
state, execution coordination, retry, and local AST modules are pending final
combined acceptance. See `docs/current-source-ownership.json`,
`docs/runtime-source-manifest.json`, and `docs/EXTRACTION_STATUS.md`.

## Local usage

Build and install the wheel with an explicit local `nexus-learning` wheel, then
assemble the runtime through explicit public contracts:

```console
python3 -m venv .venv
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
    ExecutionStatePort, ExecutionContractPort, WorkerAdapterPort,
    TargetExecutionPort, ProcessOwnershipPort, ExecutionFinalizationPort,
)
from nexus_runtime_support_candidate import build_memory_retrieval_adapter

exports = build_runtime_exports()
memory = build_memory_retrieval_adapter("/path/to/project")
invoker = exports.build_local_memory_capability_invoker(
    "/path/to/project", adapter=memory
)
```

`ContextHub` and `ContextHubDependencies` are public assembly contracts. Core,
Learning, Open SWE, and repository backends remain explicitly injected owners;
the runtime does not discover providers, deploy services, or create those stores
implicitly. `ExecutionStateStore` owns durable JSON state reads, atomic writes,
and archive selection. `ExecutionCoordinator` accepts explicit state, contract,
worker, target, process-ownership, and finalization effect ports; host code owns
provider calls, worktrees, and candidate finalization.

For a deterministic end-to-end run/retry/readback example, execute
`tests/test_runtime_operations.py::test_real_planner_run_and_replan_successful_receipts`.
It uses the real Planner and admission composition, writes a temporary
candidate, verifies its bytes, retries once, and checks the resulting receipt
lineage without contacting a provider.

`nexus_runtime_candidate` is retained as a compatibility namespace for older
callers; the public composition binds the current `nexus_runtime_p6c_candidate`
implementation. No source imports the compatibility namespace internally.

## Owner workflow integration

`tests/integration/test_owner_workflow.py` is the full owner deterministic fixture: a
deterministic OpenSWE graph writes an artifact, Repository Intelligence analyzes
the changed file, Runtime emits and reads a receipt, Core certifies hashes from
that artifact, and Learning projects the receipt. Run it with the owner wheels
installed:

```console
.venv/bin/python -I -m pytest -q tests/integration/test_owner_workflow.py
```

The test is skipped when optional owner packages are absent; acceptance requires
the command above to run unskipped. Provider-backed execution remains outside
this deterministic fixture.
