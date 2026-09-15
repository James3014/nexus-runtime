# Nexus Runtime — Agent Contract

This file is the repository-local operating contract for AI coding agents and automated contributors. It applies to the whole repository unless a deeper `AGENTS.md` narrows rules for a subtree.

## Repository authority

`nexus-runtime` owns the independent runtime/package contracts for:

- planning/admission composition;
- execution coordination;
- retry/replan state;
- writer/event/effect boundaries;
- durable execution/context state;
- explicit host/backend composition ports.

It does not implicitly own or select:

- Nexus Core Evidence Trust or Completion Certification;
- Nexus Learning policy authority;
- Open SWE execution implementation;
- Repository Intelligence decisions;
- provider/model availability;
- arbitrary host activation or deployment;
- protected merge, release, or production authority.

A packaged planner implementation inside this repository does not make Runtime the route/capability authority. Preserve `runtime_is_planner_authority = false` unless an explicit repository-local architecture decision changes that boundary.

## Required reading before mutation

Read the smallest relevant set:

- `README.md` for ownership, source/evidence clocks, compatibility, and current package boundaries;
- `docs/PLANNER_RUNTIME_LAYERING.md` for planner/runtime ownership and override rules;
- `docs/EXTRACTION_STATUS.md` and `docs/current-source-ownership.json` when work touches extraction, ownership, package boundaries, or compatibility retirement;
- the relevant provenance/binding document under `docs/` when changing an extracted subsystem or public binding.

Do not import `Nexus-new` governance wholesale. Cross-repository policy is not mutation authority here unless this repository explicitly adopts it.

## Change rules

1. Bind the exact repository root, current default branch, HEAD, and dirty state before editing. Historical SHAs in docs are provenance, not standing current truth.
2. Keep repository source, accepted package/pin, installed artifact, loaded service, runtime witness, and production claim as separate evidence clocks.
3. Preserve explicit composition. Do not introduce implicit provider, host, deployment target, backend, or owner selection as a shortcut.
4. Preserve single-owner boundaries. Do not create a second/third route authority, verifier, Completion authority, Learning authority, Repository Intelligence implementation, or external execution owner.
5. Compatibility namespaces and extracted lineage may only be removed or collapsed with current caller evidence and explicit scope. Do not delete compatibility code merely because a newer canonical name exists.
6. A host override of planner/runtime contracts must remain complete and fail closed. Do not silently combine incompatible partial owner families.
7. Cross-repository mutation requires separate explicit authority in the target repository and must follow that repository's own `AGENTS.md` or equivalent contract.
8. Do not self-authorize merge, release, deployment, host activation, or production claims.

## Verification

Run the smallest meaningful verification for the changed surface and report only checks actually executed.

- Documentation-only: inspect the physical diff and run `git diff --check` when a local checkout is available.
- Runtime/package logic: run targeted tests for the affected subsystem first.
- Planner/runtime composition: include tests that prove invalid partial overrides fail closed and that ownership does not silently transfer.
- Standalone packaging or import-boundary changes: run the applicable wheel/install checks and verify public imports from the built artifact when the environment supports it.
- Owner workflow integration: `tests/integration/test_owner_workflow.py` only counts when it runs unskipped in the intended owner-package environment. A skip is not PASS.
- Retry/replan/effect changes: verify durable identity, idempotency/reconciliation, and negative paths. `OUTCOME_UNKNOWN` must never become blind retry permission.

Unavailable or skipped verification must be reported as a gap, not normalized into success.

## Completion and stop boundary

Implementation and tests prove only the exact source/package behavior they exercise. They do not automatically prove loaded-host identity, runtime readiness, release, deployment, or production state.

Stop and rebind authority when a task would:

- transfer route/capability authority into Runtime;
- create duplicate Core/Learning/Open SWE/Repository Intelligence authority;
- remove compatibility without current caller evidence;
- change cross-repository ownership semantics beyond the bounded task;
- mutate another repository without separate target-repo authority;
- merge, release, deploy, activate a host, or make a production claim without explicit authorization.
