# Planner / Runtime Layering

Status: implementation boundary for Wave 2. This document does not create route authority.

## Invariant

`nexus-runtime` executes and coordinates an explicitly bound planner family; it is the planner *source* owner under owner decision D2 (2026-10-08), but route/capability *decision* authority stays with Nexus-new through issue binding (planner behavior changes require a Nexus-new issue reference in the PR body).

The public `nexus_runtime.build_runtime_exports()` exposes `planner_binding` with schema `nexus.runtime.planner_binding.v1`.

Two modes are valid:

- `PACKAGED_COMPATIBILITY` — standalone qualification uses the packaged `nexus_planning_candidate` implementation family. This preserves standalone behavior; no cross-repository source-parity check is run (retired in Phase 4a).
- `EXTERNAL_COMPLETE_OVERRIDE` — a host supplies the complete planner-domain family: Planner, canonical planning bundle, task context, replan authorization, plan function, and replan function.

In both modes:

```text
runtime_is_planner_authority = false   # decision authority, not source location
```

A partial host override is rejected before Runtime construction. Runtime must not combine a host Planner class with packaged plan/replan/context contracts implicitly.

## Nexus-new consumer

The current Nexus-new integration already supplies the complete six-symbol planner family through `nexus.services.runtime_compat`. Therefore this Runtime contract makes the existing ownership explicit and fail-closed; it does not transfer Planner authority or change route/capability selection.

## Non-goals

This change does not:

- change `CapabilityPlanner.plan()` behavior;
- make packaged cognitive strategies mandatory architecture;
- remove the standalone packaged planner;
- alter Workforce Admission;
- alter provider/model selection;
- alter verifier, Completion, merge, release, or deployment authority.
