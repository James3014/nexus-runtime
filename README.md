# nexus-runtime

Independent source package for task planning/admission, execution coordination,
retry/replan state, writer/event boundaries, and durable context storage.

The runtime is assembled from explicit typed bindings. Core contracts, learning,
Open SWE execution, and repository intelligence remain external package owners.
No provider or native service is selected implicitly.

The ten-module runtime spine is generated from frozen source revision
`471a281badda342ccab26606e0c46cbca6867cbb`. Planner/admission, support, and
context packages retain their donor provenance and are not relabeled as that
revision. See `docs/runtime-source-manifest.json` and
`docs/EXTRACTION_STATUS.md`.

## Local usage

Build and install the wheel with the pinned `nexus-learning` dependency, then assemble
the runtime through explicit public contracts:

```console
python -m venv .venv
python -m pip install /path/to/nexus_learning-0.1.0-py3-none-any.whl
python -m pip wheel --no-deps --wheel-dir dist .
python -m pip install dist/nexus_runtime-0.1.0.dev0-py3-none-any.whl
python examples/local_run.py
```

```python
from nexus_runtime import ContextHub, ContextHubDependencies, build_runtime_exports
from nexus_runtime_support_candidate import build_memory_retrieval_adapter

exports = build_runtime_exports()
memory = build_memory_retrieval_adapter("/path/to/project")
invoker = exports.build_local_memory_capability_invoker(
    "/path/to/project", adapter=memory
)
```

`ContextHub` and `ContextHubDependencies` are public assembly contracts. Core,
Learning, Open SWE, and repository backends remain explicitly injected owners;
the runtime does not discover providers or create those stores implicitly.

For a deterministic end-to-end run/retry/readback example, execute
`tests/test_runtime_operations.py::test_real_planner_run_and_replan_successful_receipts`.
It uses the real Planner and admission composition, writes a temporary
candidate, verifies its bytes, retries once, and checks the resulting receipt
lineage without contacting a provider.

`nexus_runtime_candidate` is retained as a compatibility namespace for older
callers; the public composition binds the current `nexus_runtime_p6c_candidate`
implementation. No source imports the compatibility namespace internally.

## Owner workflow integration

`tests/integration/test_owner_workflow.py` runs the full linked workflow: a
deterministic OpenSWE graph writes an artifact, Repository Intelligence analyzes
the changed file, Runtime emits and reads a receipt, Core certifies hashes from
that artifact, and Learning projects the receipt. Run it with the owner wheels
installed:

```console
/private/tmp/nexus-six-repo-integration-20260909/venv/bin/python -I -m pytest -q tests/integration/test_owner_workflow.py
```

The test is skipped when optional owner packages are absent; acceptance requires
the command above to run unskipped. Provider-backed execution remains outside
this deterministic fixture.
