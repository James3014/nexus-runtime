# Extraction status

The historical extraction/source-bound baseline is revision
`a3fd8006d61d4eb14637050c7da5a95e5e28d157`. That revision records the
original extraction evidence; it is not rewritten as evidence produced at the
current repository revision. Historical donor provenance remains preserved per
file.

The current Python source-ownership snapshot is revision
`1fc22650f3d4f268df9e68ae16903751a042abfe`. This is a source-scope anchor for
the `src/**/*.py` hashes in `docs/current-source-ownership.json`, not a claim
that the live repository HEAD will remain at that SHA. Later non-`src` commits
do not invalidate this snapshot; any `src` mutation requires refreshing the
ownership map. The snapshot contains the integrated runtime source plus accepted
planner/admission and support candidates and the context checkpoint, search,
read store, execution state, execution coordination, retry, local AST, and
Planner-binding compatibility modules.

Planner/support candidates retain their own historical provenance. Their API
parity with the frozen runtime source remains an integration gate. Core,
Learning, Open SWE, and repository intelligence remain external owners.

The public package also exports ContextHub, task-context assembly,
`ExecutionStateStore`, and `ExecutionCoordinator` contracts.
Memory retrieval provides an explicit local JSONL backend plus optional injected
Findings and Repository read ports through the composition helper.

PR #10 records current consumer acceptance. The post-merge hosted standalone
wheel run `34540980622` and runtime test run `34540980592` both succeeded at
`39515b73`. The earlier representative caller artifact
`/private/tmp/nexus-final-1ae-root.xml`, associated with caller
`1ae08059513ded50a2caa23c9586cd3d46b386f1`, is historical evidence only and
is not a current physical witness. Host memory uses an explicit adapter.

These source, consumer, and hosted-run facts do not imply deployment, release,
or production readiness.

## Current ownership map

`docs/current-source-ownership.json` records every Python source module at its
`source_head` snapshot, with source hash, canonical owner, and donor lineage. The
runtime public entrypoints are `nexus_runtime.build_runtime_exports`,
`nexus_runtime.ContextHub`, `nexus_runtime.ContextHubDependencies`, and the
explicit memory builder in `nexus_runtime_support_candidate`. Planner/admission,
context, memory, and external Core/Learning/Open SWE/repository services retain
separate ownership boundaries. The compatibility namespace forwards to the
current p6c runtime implementation and is not a second algorithm source.
