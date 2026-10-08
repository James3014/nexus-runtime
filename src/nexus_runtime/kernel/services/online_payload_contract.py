"""Dependency-light Online payload helpers and their single definition patchpoint.

``nexus.services.unified_runtime`` re-exports these objects for legacy imports;
the definitions and their globals live here, so tests and adapters that patch
the predicate should patch this module's definition point.
"""

from __future__ import annotations

from typing import Any, Mapping


_ONLINE_NON_DELIVERY_MARKERS = (
    "online_execution_not_authorized",
    "IneligibleTierError",
    "Error authenticating",
    "authentication failed",
    "UNAUTHENTICATED",
    "HTTP Error 401",
    "HTTP Error 403",
    "HTTP Error 404",
    "HTTP Error 429",
    "provider_not_configured",
)


def online_payload_indicates_non_delivery(payload: Mapping[str, Any] | None) -> bool:
    """True when Online response is auth/error theater, not a delivered provider result.

    Auth failures often return non-empty stdout/stderr; they must never be classified
    as ``output_delivered=true`` / stage SUCCEEDED.
    """
    if not isinstance(payload, Mapping):
        return True
    err = str(payload.get("error") or "")
    raw = str(payload.get("raw_response") or "")
    status = str(payload.get("status") or "")
    resp = payload.get("response")
    if isinstance(resp, Mapping):
        err = err or str(resp.get("error") or "")
        status = status or str(resp.get("status") or "")
        nested_raw = resp.get("raw_response")
        if nested_raw:
            raw = raw or str(nested_raw)
    blob = "\n".join([err, raw, status])
    if any(marker in blob for marker in _ONLINE_NON_DELIVERY_MARKERS):
        return True
    if status.upper() in {"FAIL", "FAILED", "ERROR"}:
        return True
    return False


def normalize_online_invoker_payload(
    *,
    provider: str,
    task_id: str,
    invoked: bool,
    output_delivered: bool,
    gate_passed: bool,
    provider_call_count: int,
    response: Any = "",
    raw_response: str = "",
    usage: Mapping[str, Any] | None = None,
    error: str = "",
    evidence_refs: list[str] | None = None,
    transport: str = "",
    selection_source: str = "",
    execution_role: str = "online",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable Online invoker payload contract for all transports.

    Required fields:
      provider, task_id, invoked, output_delivered, gate_passed,
      provider_call_count, response, raw_response, usage, error, evidence_refs

    Optional binding metadata:
      execution_role, transport, selection_source
    """
    payload: dict[str, Any] = {
        "provider": str(provider or ""),
        "task_id": str(task_id or ""),
        "invoked": bool(invoked),
        "output_delivered": bool(output_delivered),
        "gate_passed": bool(gate_passed),
        "provider_call_count": int(provider_call_count or 0),
        "response": response,
        "raw_response": str(raw_response or ""),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "error": str(error or ""),
        "evidence_refs": [str(ref) for ref in (evidence_refs or [])],
        "execution_role": str(execution_role or "online"),
        "transport": str(transport or ""),
        "selection_source": str(selection_source or ""),
    }
    if isinstance(extra, Mapping):
        for key, value in extra.items():
            if key not in payload:
                payload[str(key)] = value
    # Fail closed: auth/error body is never a successful Online delivery.
    if online_payload_indicates_non_delivery(payload):
        payload["output_delivered"] = False
        payload["gate_passed"] = False
        if not payload["error"]:
            payload["error"] = "online_non_delivery_detected"
    return payload
