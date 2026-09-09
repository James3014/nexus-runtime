from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .ports import (
    MissingRetryBindingError,
    RetryContractPort,
    RetryDispatchPort,
    RetryStatePort,
    RetrySubmissionPort,
)

TERMINAL_STATUSES = frozenset(
    {
        "FINAL_BLOCK",
        "RETAINED_FOR_REVIEW",
        "REJECTED",
        "SUPERSEDED",
        "INTEGRATED",
        "INTEGRATION_FAILED",
        "CANCELLED",
        "REHEARSAL_VERIFIED",
        "DIRECT_COMPLETED",
        "DIRECT_RECONCILE_REQUIRED",
        "INTEGRATED_AND_CLEANED",
    }
)
PENDING_CANDIDATE_STATUSES = frozenset(
    {
        "PENDING_HUMAN_APPROVAL",
        "APPROVED",
        "APPROVAL_INVALIDATED",
        "INTEGRATING",
    }
)
INTEGRATION_INTERMEDIATE_STATUSES = frozenset(
    {
        "INTEGRATION_FAILED_PRE_APPLY",
        "INTEGRATION_VERIFY_FAILED_AFTER_APPLY",
        "INTEGRATED_TARGET_RETAINED",
    }
)
RETRYABLE_TASK_STATUSES = frozenset({"FINAL_BLOCK", "CANCELLED"})


@dataclass(frozen=True)
class RetryService:
    state: RetryStatePort
    contract: RetryContractPort
    dispatch: RetryDispatchPort
    submission: RetrySubmissionPort

    def __post_init__(self) -> None:
        for name in ("state", "contract", "dispatch", "submission"):
            if getattr(self, name, None) is None:
                raise MissingRetryBindingError(f"explicit {name} port is required")

    def retry_task(self, task_id: str) -> dict[str, Any]:
        snapshot = self.state.read_snapshot(task_id)
        if snapshot is None:
            raise KeyError(f"unknown task_id: {task_id}")
        state = dict(snapshot)
        if state.get("state_valid") is False:
            return {
                **state,
                "retry": self._meta(
                    task_id,
                    state,
                    "BLOCKED_INVALID_STATE",
                    (state.get("blocker") or {}).get("code"),
                ),
            }
        status = str(state.get("status") or "UNKNOWN")
        meta = self._meta(task_id, state, None, None)
        request = state.get("request")
        try:
            maximum = int(self.contract.maximum_attempts(request or {}))
            if len(state.get("attempts") or ()) >= maximum:
                return {
                    **state,
                    "retry": {
                        **meta,
                        "decision": "BLOCK",
                        "blocker": "ATTEMPT_BUDGET_EXHAUSTED",
                    },
                }
        except Exception:
            pass
        if status == "RETAINED_FOR_REVIEW":
            return {
                **state,
                "retry": {
                    **meta,
                    "decision": "BLOCKED_RETAINED_REVIEW",
                    "blocker": "human disposition or retained-candidate recovery is required before retry; clean no-Candidate retention may retry only after formal cleanup",
                },
            }
        if (
            status in INTEGRATION_INTERMEDIATE_STATUSES
            or status == "INTEGRATION_FAILED"
        ):
            return {
                **state,
                "retry": {
                    **meta,
                    "decision": "BLOCKED_INTEGRATION_FAILURE",
                    "blocker": "integration failure requires dedicated integration retry; generic task retry is forbidden",
                },
            }
        if status in TERMINAL_STATUSES - RETRYABLE_TASK_STATUSES - {
            "RETAINED_FOR_REVIEW",
            "INTEGRATION_FAILED",
        }:
            return {
                **state,
                "retry": {
                    **meta,
                    "decision": "BLOCKED_ABSORBING_STATUS",
                    "blocker": f"task is in absorbing terminal status {status}; same-semantic task retry is forbidden",
                },
            }
        if status not in RETRYABLE_TASK_STATUSES:
            return {
                **state,
                "retry": {
                    **meta,
                    "decision": "NO_DUPLICATE_ACTIVE_TASK",
                    "blocker": f"task is {status}; wait for its existing attempt instead of resubmitting",
                },
            }
        if str(state.get("cleanup_decision") or "") not in {
            "REMOVED",
            "ALREADY_REMOVED",
            "TARGET_CLEANED",
        }:
            return {
                **state,
                "retry": {
                    **meta,
                    "decision": "BLOCKED_TARGET_DISPOSITION",
                    "blocker": "previous Target disposition is not removed/cleaned",
                },
            }
        if not isinstance(request, Mapping):
            return {
                **state,
                "retry": {
                    **meta,
                    "decision": "BLOCKED_MISSING_REQUEST",
                    "blocker": "durable request is missing; cannot safely reconstruct the task",
                },
            }
        for envelope_source in (request, state):
            if (
                "canonical_dispatch_envelope" in envelope_source
                and envelope_source.get("canonical_dispatch_envelope") is not None
                and not isinstance(envelope_source.get("canonical_dispatch_envelope"), Mapping)
            ):
                return {
                    **state,
                    "retry": {
                        **meta,
                        "decision": "BLOCK",
                        "blocker": "WORKFORCE_DISPATCH_ENVELOPE_INVALID",
                    },
                }
        demands, admission = self.dispatch.workforce_inputs(request)
        dispatch_needed = bool(
            request.get("canonical_dispatch_envelope") is not None
            or state.get("canonical_dispatch_envelope") is not None
            or demands is not None
            or admission is not None
            or str(state.get("acceptance_decision") or "") == "REPAIRABLE"
        )
        predecessor = None
        if dispatch_needed:
            try:
                predecessor = self.dispatch.validate_predecessor(request, state)
            except RuntimeError as exc:
                predecessor = self.dispatch.recover_predecessor(state, request, exc)
                if predecessor is None:
                    return {
                        **state,
                        "retry": {**meta, "decision": "BLOCK", "blocker": str(exc)},
                    }
        repair_dispatch = None
        if str(state.get("acceptance_decision") or "") == "REPAIRABLE":
            planner = request.get("planner_output")
            if not isinstance(planner, Mapping):
                return {**state, "retry": {**meta, "decision": "BLOCK", "blocker": "WORKFORCE_ADMISSION_BINDING_MISSING"}}
            try:
                repair_dispatch = self.dispatch.validate_predecessor(request, state)
            except RuntimeError as exc:
                return {**state, "retry": {**meta, "decision": "BLOCK", "blocker": str(exc)}}
            worker_id = str((repair_dispatch or {}).get("worker_id") or "")
            if not worker_id:
                return {**state, "retry": {**meta, "decision": "BLOCK", "blocker": "WORKFORCE_REPAIR_WORKER_MISSING"}}
            request = dict(request)
            request["repair_worker_id"] = worker_id
        retry_request = self.contract.build_retry_request({**state, "request": request})
        if repair_dispatch is not None and predecessor is None:
            predecessor = repair_dispatch
        if predecessor is not None:
            try:
                rebound = self.dispatch.rebind_fresh_attempt(retry_request, predecessor)
            except (TypeError, ValueError) as exc:
                return {
                    **state,
                    "retry": {
                        **meta,
                        "decision": "BLOCK",
                        "blocker": f"WORKFORCE_REBIND_FAILED:{exc}",
                    },
                }
            if not isinstance(rebound, Mapping):
                return {
                    **state,
                    "retry": {
                        **meta,
                        "decision": "BLOCK",
                        "blocker": "WORKFORCE_REBIND_FAILED",
                    },
                }
            fresh = self.dispatch.validate_fresh(rebound, state)
            if not isinstance(fresh, Mapping):
                return {
                    **state,
                    "retry": {
                        **meta,
                        "decision": "BLOCK",
                        "blocker": "WORKFORCE_REBIND_FAILED",
                    },
                }
            retry_request = dict(rebound)
            retry_request.update(
                {
                    "worker": fresh.get("provider"),
                    "provider": fresh.get("provider"),
                    "model": fresh.get("model"),
                    "worker_id": fresh.get("worker_id"),
                    "worker_order": [fresh.get("provider")],
                    "workforce_dispatch": dict(fresh),
                    "canonical_dispatch_envelope": fresh.get("canonical_dispatch_envelope"),
                }
            )
        result = dict(self.submission.submit(retry_request))
        result["retry"] = {
            **meta,
            "decision": "REUSED_TASK_ID",
            "new_attempt_id": result.get("attempt_id"),
            "new_action_id": result.get("action_id"),
            "new_idempotency_key": result.get("idempotency_key"),
            "attempts": len(result.get("attempts") or ()),
        }
        return result

    @staticmethod
    def _meta(
        task_id: str,
        state: Mapping[str, Any],
        decision: str | None,
        blocker: str | None,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "previous_status": state.get("status"),
            "previous_attempt_id": state.get("attempt_id"),
            "decision": decision,
            "blocker": blocker,
        }
