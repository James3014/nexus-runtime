# Extraction status

This repository contains the integrated runtime source reviewed at frozen
revision `ced4da6354a11187498dea0fa58d920a8e90e3c1`, plus accepted
planner/admission and support candidates and the context checkpoint, search,
read store, execution state, execution coordination, retry, and local AST
modules. Historical donor provenance remains preserved per file.

Planner/support candidates retain their own historical provenance. Their API
parity with the frozen runtime source remains an integration gate. Core,
Learning, Open SWE, and repository intelligence remain external owners.

The public package also exports ContextHub, task-context assembly,
`ExecutionStateStore`, and `ExecutionCoordinator` contracts.
Memory retrieval provides an explicit local JSONL backend plus optional injected
Findings and Repository read ports through the composition helper.

The integrated source is pending final combined acceptance. Remaining evidence
gates cover the ContextHub donor differential, execution-state caller wiring,
execution-coordination parity and host wiring, and clean combined installation;
no provider selection or deployment is implied.

## Current ownership map

`docs/current-source-ownership.json` records every Python source module in this
repository, its current hash, canonical owner, and donor lineage. The runtime
public entrypoints are `nexus_runtime.build_runtime_exports`,
`nexus_runtime.ContextHub`, `nexus_runtime.ContextHubDependencies`, and the
explicit memory builder in `nexus_runtime_support_candidate`. Planner/admission,
context, memory, and external Core/Learning/Open SWE/repository services retain
separate ownership boundaries. The compatibility namespace forwards to the
current p6c runtime implementation and is not a second algorithm source.
