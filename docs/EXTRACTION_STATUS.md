# Extraction status

This repository contains the current ten-module runtime transformation generated
from frozen source revision `471a281badda342ccab26606e0c46cbca6867cbb`, plus the
accepted planner/admission and support candidates and the context checkpoint,
search, and read store prototype.

Planner/support candidates retain their own historical provenance. Their API
parity with the frozen runtime source remains an integration gate. Core,
Learning, Open SWE, and repository intelligence remain external owners.

The public package also exports ContextHub and task-context assembly contracts.
Memory retrieval provides an explicit local JSONL backend plus optional injected
Findings and Repository read ports through the composition helper.

## Current ownership map

`docs/current-source-ownership.json` records every Python source module in this
repository, its current hash, canonical owner, and donor lineage. The runtime
public entrypoints are `nexus_runtime.build_runtime_exports`,
`nexus_runtime.ContextHub`, `nexus_runtime.ContextHubDependencies`, and the
explicit memory builder in `nexus_runtime_support_candidate`. Planner/admission,
context, memory, and external Core/Learning/Open SWE/repository services retain
separate ownership boundaries. The compatibility namespace forwards to the
current p6c runtime implementation and is not a second algorithm source.
