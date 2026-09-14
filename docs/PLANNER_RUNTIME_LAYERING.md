# Planner / Runtime Layering

Status: implementation boundary for Wave 2. This document does not create route authority.

## Invariant

`nexus-runtime` executes and coordinates an explicitly bound planner family; it does not become the route/capability authority merely because it contains a packaged planner implementation.

The public `nexus_runtime.build_runtime_exports()` exposes `planner_binding` with schema `nexus.runtime.planner_binding.v1`.

Two modes are valid:

- `PACKAGED_COMPATIBILITY` — standalone qualification uses the packaged `nexus_planning_candidate` implementation family. This preserves standalone behavior and compatibility evidence only.
- `EXTERNAL_COMPLETE_OVERRIDE` — a host supplies the complete planner-domain family: Planner, canonical planning bundle, task context, replan authorization, plan function, and replan function.

In both modes:

```text
runtime_is_planner_authority = false
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
