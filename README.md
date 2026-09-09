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

Install the wheel with the pinned `nexus-learning` dependency, then assemble
the runtime through explicit public contracts:

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
