"""Nexus-owned Online execution authorization (product gate).

Environment variable NEXUS_EXTERNAL_RUNTIME_AUTHORIZED remains an emergency
override source only — never the sole product mechanism, and never implied by
provider credentials alone.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

ONLINE_POLICIES = frozenset({"deny", "auto", "require"})
AUTH_SOURCES = frozenset(
    {
        "cli_task_policy",
        "workspace_policy",
        "operator_environment_override",
        "injected_test_transport",
        "fail_closed_default",
    }
)

ONLINE_READY = "ONLINE_READY"
ONLINE_NOT_REQUESTED = "ONLINE_NOT_REQUESTED"
ONLINE_DENIED_BY_POLICY = "ONLINE_DENIED_BY_POLICY"
ONLINE_PROVIDER_UNAVAILABLE = "ONLINE_PROVIDER_UNAVAILABLE"
ONLINE_PROVIDER_UNAUTHENTICATED = "ONLINE_PROVIDER_UNAUTHENTICATED"
ONLINE_CONTEXT_TRANSFER_DENIED = "ONLINE_CONTEXT_TRANSFER_DENIED"
ONLINE_BUDGET_EXCEEDED = "ONLINE_BUDGET_EXCEEDED"
ONLINE_CONFIGURATION_INVALID = "ONLINE_CONFIGURATION_INVALID"

DEFAULT_APPROVED_PROVIDERS = ("gemini", "agy", "grok", "codex", "openai", "opencode")
WORKSPACE_POLICY_RELATIVE = Path(".nexus") / "online_execution_policy.json"
ENV_OVERRIDE = "NEXUS_EXTERNAL_RUNTIME_AUTHORIZED"


@dataclass(frozen=True)
class OnlineExecutionDecision:
    online_policy: str
    online_execution_requested: bool
    online_execution_authorized: bool
    online_authorization_source: str
    approved_online_providers: tuple[str, ...]
    preflight_status: str
    reason: str = ""
    allow_external_context_transfer: bool = True
    maximum_provider_calls: int = 10
    maximum_retry_count: int = 3
    requested_provider: str = ""
    physical_invocation_allowed: bool = False
    claim_boundary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["approved_online_providers"] = list(self.approved_online_providers)
        return payload


def normalize_online_policy(policy: str | None) -> str:
    raw = str(policy or "").strip().lower()
    if not raw:
        return "deny"
    if raw not in ONLINE_POLICIES:
        raise ValueError("invalid_online_policy")
    return raw


def load_workspace_online_policy(project_root: str | Path) -> dict[str, Any]:
    path = Path(project_root).expanduser() / WORKSPACE_POLICY_RELATIVE
    if not path.is_file():
        return {
            "default_online_policy": "deny",
            "approved_online_providers": list(DEFAULT_APPROVED_PROVIDERS),
            "allow_external_context_transfer": True,
            "maximum_provider_calls": 10,
            "maximum_retry_count": 3,
            "policy_path": str(path),
            "policy_present": False,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "default_online_policy": "deny",
            "approved_online_providers": list(DEFAULT_APPROVED_PROVIDERS),
            "allow_external_context_transfer": True,
            "maximum_provider_calls": 10,
            "maximum_retry_count": 3,
            "policy_path": str(path),
            "policy_present": False,
            "policy_error": "invalid_json",
        }
    if not isinstance(data, Mapping):
        data = {}
    providers = data.get("approved_online_providers") or list(DEFAULT_APPROVED_PROVIDERS)
    return {
        "default_online_policy": str(data.get("default_online_policy") or "deny").strip().lower(),
        "approved_online_providers": [str(p).strip().lower() for p in providers if str(p).strip()],
        "allow_external_context_transfer": bool(data.get("allow_external_context_transfer", True)),
        "maximum_provider_calls": int(data.get("maximum_provider_calls", 10) or 10),
        "maximum_retry_count": int(data.get("maximum_retry_count", 3) or 3),
        "policy_path": str(path),
        "policy_present": True,
    }


def build_online_execution_context_fields(
    *,
    online_policy: str = "deny",
    project_root: str | Path = ".",
    task_id: str = "",
    workspace_revision: str = "",
    policy_source: str = "cli",
    requested_provider: str = "",
) -> dict[str, Any]:
    """Fields for TaskRequest.execution_context propagation."""
    policy = normalize_online_policy(online_policy)
    provider = str(requested_provider or "").strip().lower()
    decision = resolve_online_execution_decision(
        task_online_policy=policy,
        project_root=project_root,
        planner_online_needed=policy in {"auto", "require"},
        injected_transport=False,
        requested_provider=provider,
    )
    fields: dict[str, Any] = {
        "online_policy": policy,
        "online_execution_requested": bool(decision.online_execution_requested),
        "online_execution_authorized": bool(decision.online_execution_authorized),
        "online_authorization_source": decision.online_authorization_source,
        "approved_online_providers": list(decision.approved_online_providers),
        "online_preflight_status": decision.preflight_status,
        "online_policy_source": str(policy_source or "cli"),
        "task_id": str(task_id or ""),
        "workspace_revision": str(workspace_revision or ""),
        "online_execution_decision": decision.to_dict(),
    }
    if provider:
        fields["oauth_provider"] = provider
        fields["online_provider"] = provider
    return fields


def resolve_online_execution_decision(
    *,
    task_online_policy: str = "",
    project_root: str | Path = ".",
    workspace_policy: Mapping[str, Any] | None = None,
    planner_online_needed: bool = True,
    injected_transport: bool = False,
    requested_provider: str = "",
    provider_call_count_so_far: int = 0,
    environ: Mapping[str, str] | None = None,
    allow_context_transfer: bool | None = None,
) -> OnlineExecutionDecision:
    """Resolve Online authorization once (fail-closed).

    Precedence:
      explicit task deny
      > injected test transport (non-physical / fixture path only)
      > explicit task auto|require
      > workspace policy
      > emergency environment override
      > fail-closed default
    """
    env = dict(environ or os.environ)
    ws = dict(workspace_policy) if isinstance(workspace_policy, Mapping) else load_workspace_online_policy(project_root)
    approved = tuple(
        str(p).strip().lower()
        for p in (ws.get("approved_online_providers") or DEFAULT_APPROVED_PROVIDERS)
        if str(p).strip()
    )
    transfer_ok = (
        bool(allow_context_transfer)
        if allow_context_transfer is not None
        else bool(ws.get("allow_external_context_transfer", True))
    )
    max_calls = int(ws.get("maximum_provider_calls", 10) or 10)
    max_retries = int(ws.get("maximum_retry_count", 3) or 3)
    provider = str(requested_provider or "").strip().lower()
    claim = {
        "public_claim_allowed": False,
        "production_ready": False,
        "credentials_do_not_imply_authorization": True,
    }

    # 1) Task deny always wins.
    task_raw = str(task_online_policy or "").strip().lower()
    if task_raw == "deny":
        return OnlineExecutionDecision(
            online_policy="deny",
            online_execution_requested=False,
            online_execution_authorized=False,
            online_authorization_source="cli_task_policy",
            approved_online_providers=approved,
            preflight_status=ONLINE_DENIED_BY_POLICY,
            reason="task_online_policy_deny",
            allow_external_context_transfer=transfer_ok,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider,
            physical_invocation_allowed=False,
            claim_boundary=claim,
        )

    # 2) Injected structured/test transport: authorize fixture path only.
    if injected_transport:
        return OnlineExecutionDecision(
            online_policy=task_raw if task_raw in ONLINE_POLICIES else "auto",
            online_execution_requested=True,
            online_execution_authorized=True,
            online_authorization_source="injected_test_transport",
            approved_online_providers=approved,
            preflight_status=ONLINE_READY,
            reason="injected_transport_authorized",
            allow_external_context_transfer=transfer_ok,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider or "injected",
            physical_invocation_allowed=False,  # fixtures only; real CLI still needs non-injected auth
            claim_boundary=claim,
        )

    # Precedence (after task deny and injected fixture handling):
    #   explicit task auto|require
    #   > workspace policy (including workspace deny — env cannot broaden)
    #   > emergency environment override (only when no workspace policy file)
    #   > fail-closed default deny
    env_override = str(env.get(ENV_OVERRIDE, "") or "").strip() == "1"
    source = "fail_closed_default"
    policy = "deny"
    if task_raw in {"auto", "require"}:
        policy = task_raw
        source = "cli_task_policy"
    elif ws.get("policy_present"):
        ws_policy = str(ws.get("default_online_policy") or "deny").strip().lower()
        policy = ws_policy if ws_policy in ONLINE_POLICIES else "deny"
        source = "workspace_policy"
        # Workspace deny is not elevated by env override (narrowing authority only).
    else:
        policy = "deny"
        source = "fail_closed_default"
        if env_override:
            policy = "auto"
            source = "operator_environment_override"

    requested = False
    if policy == "require":
        requested = True
    elif policy == "auto":
        requested = bool(planner_online_needed)
    else:
        requested = False

    if not requested:
        return OnlineExecutionDecision(
            online_policy=policy,
            online_execution_requested=False,
            online_execution_authorized=False,
            online_authorization_source=source,
            approved_online_providers=approved,
            preflight_status=ONLINE_NOT_REQUESTED if policy != "deny" else ONLINE_DENIED_BY_POLICY,
            reason=(
                "online_not_requested_by_policy_or_planner"
                if policy != "deny"
                else "online_denied_by_workspace_or_default"
            ),
            allow_external_context_transfer=transfer_ok,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider,
            physical_invocation_allowed=False,
            claim_boundary=claim,
        )

    if not transfer_ok:
        return OnlineExecutionDecision(
            online_policy=policy,
            online_execution_requested=True,
            online_execution_authorized=False,
            online_authorization_source=source,
            approved_online_providers=approved,
            preflight_status=ONLINE_CONTEXT_TRANSFER_DENIED,
            reason="external_context_transfer_denied",
            allow_external_context_transfer=False,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider,
            physical_invocation_allowed=False,
            claim_boundary=claim,
        )

    if provider_call_count_so_far >= max_calls:
        return OnlineExecutionDecision(
            online_policy=policy,
            online_execution_requested=True,
            online_execution_authorized=False,
            online_authorization_source=source,
            approved_online_providers=approved,
            preflight_status=ONLINE_BUDGET_EXCEEDED,
            reason="maximum_provider_calls_exceeded",
            allow_external_context_transfer=transfer_ok,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider,
            physical_invocation_allowed=False,
            claim_boundary=claim,
        )

    # require without a concrete provider is a configuration error (fail closed).
    if policy == "require" and not provider:
        return OnlineExecutionDecision(
            online_policy=policy,
            online_execution_requested=True,
            online_execution_authorized=False,
            online_authorization_source=source,
            approved_online_providers=approved,
            preflight_status=ONLINE_CONFIGURATION_INVALID,
            reason="require_policy_missing_provider",
            allow_external_context_transfer=transfer_ok,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider,
            physical_invocation_allowed=False,
            claim_boundary=claim,
        )

    if provider and provider not in approved and provider not in {"injected", "gateway", "fixture", "fixture_gateway"}:
        return OnlineExecutionDecision(
            online_policy=policy,
            online_execution_requested=True,
            online_execution_authorized=False,
            online_authorization_source=source,
            approved_online_providers=approved,
            preflight_status=ONLINE_PROVIDER_UNAVAILABLE,
            reason=f"provider_not_approved:{provider}",
            allow_external_context_transfer=transfer_ok,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider,
            physical_invocation_allowed=False,
            claim_boundary=claim,
        )

    # Physical real-provider invocation still requires a non-credential authorization
    # signal: task auto/require, workspace auto/require, or env override (no workspace file).
    physical_ok = source in {
        "cli_task_policy",
        "workspace_policy",
        "operator_environment_override",
    } and policy in {"auto", "require"}

    if not physical_ok:
        return OnlineExecutionDecision(
            online_policy=policy,
            online_execution_requested=True,
            online_execution_authorized=False,
            online_authorization_source=source,
            approved_online_providers=approved,
            preflight_status=ONLINE_DENIED_BY_POLICY,
            reason="no_online_authorization_signal",
            allow_external_context_transfer=transfer_ok,
            maximum_provider_calls=max_calls,
            maximum_retry_count=max_retries,
            requested_provider=provider,
            physical_invocation_allowed=False,
            claim_boundary=claim,
        )

    # Credential absence is not authorization. API-key providers need env
    # credentials for require. CLI-session providers (grok/agy/codex/gemini)
    # authenticate via local OAuth/login sessions — binary presence is required
    # but API env keys are optional; unusable sessions fail at probe/runtime.
    # Binary presence alone is never treated as product authorization.
    CLI_SESSION_PROVIDERS = frozenset({"gemini", "grok", "codex", "agy", "openai", "opencode"})
    if provider in {"gemini", "grok", "codex", "openai", "agy", "opencode"}:
        cred_keys = {
            "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "NEXUS_GEMINI_API_KEY"),
            "agy": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "NEXUS_GEMINI_API_KEY"),
            "grok": ("XAI_API_KEY", "GROK_API_KEY", "NEXUS_GROK_API_KEY"),
            "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "NEXUS_CODEX_API_KEY"),
            "openai": ("OPENAI_API_KEY", "NEXUS_OPENAI_API_KEY"),
            "opencode": (),
        }.get(provider, ())
        has_cred = any(str(env.get(k, "") or "").strip() for k in cred_keys)
        import shutil as _shutil

        binary_name = {
            "gemini": "gemini",
            "agy": "agy",
            "grok": "grok",
            "codex": "codex",
            "openai": "openai",
            "opencode": "opencode",
        }.get(provider, provider)
        binary_present = bool(_shutil.which(binary_name))
        # API-key-only path (openai without CLI session): require env key on require.
        # CLI-session providers: allow require when binary exists; runtime probe proves auth.
        if policy == "require" and not has_cred and source != "operator_environment_override":
            if provider not in CLI_SESSION_PROVIDERS or not binary_present:
                return OnlineExecutionDecision(
                    online_policy=policy,
                    online_execution_requested=True,
                    online_execution_authorized=False,
                    online_authorization_source=source,
                    approved_online_providers=approved,
                    preflight_status=ONLINE_PROVIDER_UNAUTHENTICATED,
                    reason=f"provider_credentials_missing:{provider}",
                    allow_external_context_transfer=transfer_ok,
                    maximum_provider_calls=max_calls,
                    maximum_retry_count=max_retries,
                    requested_provider=provider,
                    physical_invocation_allowed=False,
                    claim_boundary=claim,
                )

    return OnlineExecutionDecision(
        online_policy=policy,
        online_execution_requested=True,
        online_execution_authorized=True,
        online_authorization_source=source,
        approved_online_providers=approved,
        preflight_status=ONLINE_READY,
        reason="online_execution_authorized",
        allow_external_context_transfer=transfer_ok,
        maximum_provider_calls=max_calls,
        maximum_retry_count=max_retries,
        requested_provider=provider,
        physical_invocation_allowed=True,
        claim_boundary=claim,
    )


def decision_from_context(context: Mapping[str, Any] | None) -> OnlineExecutionDecision | None:
    """Extract a previously resolved decision from invoker context / request route."""
    if not isinstance(context, Mapping):
        return None
    raw = context.get("online_execution_decision")
    if isinstance(raw, OnlineExecutionDecision):
        return raw
    if isinstance(raw, Mapping) and raw.get("online_policy"):
        try:
            return OnlineExecutionDecision(
                online_policy=str(raw.get("online_policy") or "deny"),
                online_execution_requested=bool(raw.get("online_execution_requested")),
                online_execution_authorized=bool(raw.get("online_execution_authorized")),
                online_authorization_source=str(raw.get("online_authorization_source") or "fail_closed_default"),
                approved_online_providers=tuple(raw.get("approved_online_providers") or DEFAULT_APPROVED_PROVIDERS),
                preflight_status=str(raw.get("preflight_status") or ONLINE_DENIED_BY_POLICY),
                reason=str(raw.get("reason") or ""),
                allow_external_context_transfer=bool(raw.get("allow_external_context_transfer", True)),
                maximum_provider_calls=int(raw.get("maximum_provider_calls", 10) or 10),
                maximum_retry_count=int(raw.get("maximum_retry_count", 3) or 3),
                requested_provider=str(raw.get("requested_provider") or ""),
                physical_invocation_allowed=bool(raw.get("physical_invocation_allowed")),
                claim_boundary=dict(raw.get("claim_boundary") or {}),
            )
        except (TypeError, ValueError):
            return None
    # Fallback fields on context/route
    if "online_execution_authorized" in context:
        authorized = bool(context.get("online_execution_authorized"))
        return OnlineExecutionDecision(
            online_policy=str(context.get("online_policy") or "deny"),
            online_execution_requested=bool(context.get("online_execution_requested", authorized)),
            online_execution_authorized=authorized,
            online_authorization_source=str(context.get("online_authorization_source") or "fail_closed_default"),
            approved_online_providers=tuple(context.get("approved_online_providers") or DEFAULT_APPROVED_PROVIDERS),
            preflight_status=str(context.get("online_preflight_status") or (ONLINE_READY if authorized else ONLINE_DENIED_BY_POLICY)),
            reason=str(context.get("online_auth_reason") or ""),
            physical_invocation_allowed=authorized and str(context.get("online_authorization_source") or "") != "injected_test_transport",
            claim_boundary={"public_claim_allowed": False},
        )
    return None


def physical_online_authorized(context: Mapping[str, Any] | None, *, injected_transport: bool = False) -> bool:
    """Adapter enforcement helper: real provider subprocesses only when physical allowed."""
    if injected_transport:
        return True
    decision = decision_from_context(context)
    if decision is not None:
        return bool(decision.online_execution_authorized and decision.physical_invocation_allowed)
    # Unbound product path: fail closed. Env is only applied when a decision is
    # resolved via resolve_online_execution_decision / guard_physical_online —
    # adapters must not re-interpret env or workspace alone.
    return False


def resolve_decision_from_meta(
    meta: Mapping[str, Any] | None,
    *,
    project_root: str | Path = ".",
    requested_provider: str = "",
    planner_online_needed: bool = True,
    injected_transport: bool = False,
    environ: Mapping[str, str] | None = None,
) -> OnlineExecutionDecision:
    """Resolve one OnlineExecutionDecision from pipeline/task metadata."""
    data = dict(meta or {})
    provider = str(
        requested_provider
        or data.get("online_provider")
        or data.get("oauth_provider")
        or ""
    ).strip().lower()

    def _stale_missing_provider(prior: OnlineExecutionDecision | None) -> bool:
        """True when require/auto was resolved before a concrete provider was known."""
        if prior is None:
            return False
        if not provider:
            return False
        if str(prior.requested_provider or "").strip():
            return False
        if prior.reason == "require_policy_missing_provider":
            return True
        # Prior denied only because provider was empty under require/auto.
        return (
            prior.online_policy in {"auto", "require"}
            and not prior.online_execution_authorized
            and prior.preflight_status == ONLINE_CONFIGURATION_INVALID
        )

    prior = decision_from_context(data)
    if prior is not None and not _stale_missing_provider(prior):
        return prior
    prior = decision_from_context({"online_execution_decision": data.get("online_execution_decision")})
    if prior is not None and not _stale_missing_provider(prior):
        return prior
    # Empty/missing task policy is not an explicit task deny — only literal "deny"
    # wins over inject/workspace. Unset falls through: inject → workspace → env → fail-closed.
    task_policy = str(data.get("online_policy") or "").strip().lower()
    return resolve_online_execution_decision(
        task_online_policy=task_policy,
        project_root=project_root,
        planner_online_needed=planner_online_needed,
        injected_transport=injected_transport or bool(data.get("injected_transport")),
        requested_provider=provider,
        environ=environ,
    )


def denied_physical_online_result(
    *,
    task_id: str = "",
    decision: OnlineExecutionDecision | None = None,
) -> tuple[dict[str, Any], str]:
    """Canonical (res, raw) when physical Online is not authorized."""
    payload = {
        "status": "FAILED",
        "error": "online_execution_not_authorized",
        "error_category": "online_execution_not_authorized",
        "provider_call_count": 0,
        "invoked": False,
        "output_delivered": False,
        "gate_passed": False,
        "task_id": str(task_id or ""),
        "online_preflight_status": (
            decision.preflight_status if decision is not None else ONLINE_DENIED_BY_POLICY
        ),
        "online_authorization_source": (
            decision.online_authorization_source if decision is not None else "fail_closed_default"
        ),
        "reason": (decision.reason if decision is not None else "online_execution_not_authorized"),
    }
    return payload, "online_execution_not_authorized"


def guard_physical_online(
    gateway: Any,
    meta: Mapping[str, Any] | None,
    *,
    project_root: str | Path = ".",
    requested_provider: str = "",
    planner_online_needed: bool = True,
    injected_transport: bool = False,
    task_id: str = "",
    environ: Mapping[str, str] | None = None,
) -> tuple[bool, OnlineExecutionDecision, tuple[dict[str, Any], str] | None]:
    """Resolve once, bind onto Gateway, and gate physical Online CLI.

    Returns:
      (allowed, decision, denied_result_or_none)

    When ``allowed`` is False, callers must return ``denied_result`` and must
    not invoke surgical_ask / ask_structured / provider subprocesses.
    """
    decision = resolve_decision_from_meta(
        meta,
        project_root=project_root,
        requested_provider=requested_provider,
        planner_online_needed=planner_online_needed,
        injected_transport=injected_transport,
        environ=environ,
    )
    # Persist on metadata for receipt linkage (callers may already hold a dict).
    if isinstance(meta, dict):
        meta["online_execution_decision"] = decision.to_dict()
        meta["online_policy"] = decision.online_policy
        meta["online_execution_authorized"] = decision.online_execution_authorized
        meta["online_authorization_source"] = decision.online_authorization_source
        meta["online_preflight_status"] = decision.preflight_status

    if gateway is not None:
        binder = getattr(gateway, "bind_online_execution_decision", None)
        if callable(binder):
            binder(decision)
        else:
            try:
                gateway._online_execution_decision = decision
            except Exception:
                pass

    physical_ok = bool(decision.online_execution_authorized and decision.physical_invocation_allowed)
    if physical_ok:
        return True, decision, None
    # Fixture transports (injected_test_transport): allow non-CLI callables after bind.
    # Gateway physical CLI remains blocked because physical_invocation_allowed is false.
    if injected_transport and decision.online_execution_authorized:
        return True, decision, None
    return False, decision, denied_physical_online_result(task_id=task_id, decision=decision)
