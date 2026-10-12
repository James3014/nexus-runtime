# Planner / Runtime Layering

Status: implementation boundary for Wave 2. This document does not create route authority.

## Invariant

`nexus-runtime` executes and coordinates an explicitly bound planner family; it is the planner *source* owner under owner decision D2 (2026-10-08), but route/capability *decision* authority stays with Nexus-new through issue binding (planner behavior changes require a Nexus-new issue reference in the PR body).

The public `nexus_runtime.build_runtime_exports()` exposes `planner_binding` with schema `nexus.runtime.planner_binding.v1`.

Two modes are valid:

- `PACKAGED_COMPATIBILITY` — standalone qualification uses the packaged `nexus_runtime.planning` implementation family (formerly `nexus_planning_candidate`; import alias removed in Phase 2.5). This preserves standalone behavior; no cross-repository source-parity check is run (retired in Phase 4a).
- `EXTERNAL_COMPLETE_OVERRIDE` — a host supplies the complete planner-domain family: Planner, canonical planning bundle, task context, replan authorization, plan function, and replan function.

In both modes:

```text
runtime_is_planner_authority = false   # decision authority, not source location
```

A partial host override is rejected before Runtime construction. Runtime must not combine a host Planner class with packaged plan/replan/context contracts implicitly.

## Nexus-new consumer

The current Nexus-new integration already supplies the complete six-symbol planner family through `nexus.services.runtime_compat`. Therefore this Runtime contract makes the existing ownership explicit and fail-closed; it does not transfer Planner authority or change route/capability selection.

## Capability executor binding

`support/services/capability_registry.py` never imports a host executor registry. It resolves physical capability executors only through `nexus_runtime.support.services.executor_binding` (`register_executor_resolver`, `resolve_executor`, `clear_executor_resolver`). The consumer (Nexus-new) registers its real `get_executor` as the resolver at bootstrap. With no resolver registered, `build_real_executor_invoker` returns `None` and escalate-gated capabilities (hyper, swarm, nightshift, drone, xray, ddtree, autoreason, metabolism, ultra_review) report `BLOCKED_EXECUTOR_UNAVAILABLE`. This is an explicit composition port; it transfers no route or capability decision authority.

## Retired capabilities

Owner-approved first retirement batch, 2026-10-12 (reason code `RETIRED_2026_10_12_FIRST_BATCH`, Runtime issue #170):

| Node | Why |
| --- | --- |
| `federation` | EXPERIMENTAL_NOT_PROMOTED; Nexus-new implementation is stubs with zero importers |
| `oracle_shadow` | EXPERIMENTAL_NOT_PROMOTED; Nexus-new implementation returns hard-coded numbers |
| `stress_test` | Nexus-new executor trusts `RedTeamAudit.stress_test`, whose sub-tests always return PASS |
| `ui_validator` | Nexus-new executor always returns BLOCKED `no_live_browser_session` and never validates |
| `benchmark` | Contract mapped to Nexus-new `BenchmarkService`, a random simulation (not a real measurement) |

`learning_closure` was approved in the same batch but is not a CapabilityPlanner node or execution contract in this repository (Nexus-new executor only), so nothing is retired for it here.

State model: retired nodes keep their ids so old receipts and aliases still parse. `CapabilityNode.maturity == "retired"` (`RETIRED_MATURITY`) makes the planner treat the node as never selectable: `enable()` records the ignored signal, and a terminal pass after every policy (including a route-oracle "expected capability" demand) forces the node to the existing terminal `forbidden` state with reason `RETIRED_2026_10_12_FIRST_BATCH`. `plan.signal_snapshot["retired_capabilities"]` lists them. In `capability_registry.py` the contracts use `execution_class = RETIRED` (gap class `G_retired`, consumer mode `RETIRED` for Local and Online, no executor key or physical callable). A caller or host that still names a retired capability receives an explicit SKIPPED receipt with `skip_reason = RETIRED_2026_10_12_FIRST_BATCH`; no per-run or host invoker can revive it. The single source of truth is `RETIRED_FIRST_BATCH` in `planning/engine/capability_contracts.py`.

The matching Nexus-new executor retirement is the Nexus-new archive PR, number pending. Runtime does not change route or capability decision authority; the planner stops offering these nodes only once Nexus-new bumps its Runtime pin.

## Non-goals

This change does not:

- change `CapabilityPlanner.plan()` behavior (other than the retired set below);
- make packaged cognitive strategies mandatory architecture;
- remove the standalone packaged planner;
- alter Workforce Admission;
- alter provider/model selection;
- alter verifier, Completion, merge, release, or deployment authority.
