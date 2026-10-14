from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .context import KernelContext
from .capabilities import _capability_evidence_summary
from .constants import LOCAL_ONLY_PROVIDERS, ONLINE_CLI_SPEC_REGISTRY, REGISTERED_CLI_MODEL_BINDING_FLAGS, REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS, REQUEST_SCHEMA, SELECTION_COMPATIBILITY_DEFAULT, SELECTION_ENVIRONMENT_DEFAULT, SELECTION_EXPLICIT_REQUEST, SELECTION_INJECTED_TRANSPORT, TRANSPORT_GATEWAY_COMPATIBILITY, TRANSPORT_REGISTERED_CLI, TRANSPORT_STRUCTURED_CALLABLE, TRANSPORT_UNRESOLVED
from .identity import _mapping, _optional_identity
from .services.online_payload_contract import (
    online_text_indicates_headless_permission_denial,
)


# Agy permission posture (#179).  The runtime owns the Agy argv: a caller may
# supply only the executable.  The flags below are refused anywhere in the
# option part of the final argv as defense in depth, and the recorded
# ``provider_permission_mode`` is derived from the argv actually executed.
AGY_PERMISSION_BYPASS_FLAGS: frozenset[str] = frozenset(
    {"--dangerously-skip-permissions", "--yolo", "--auto-approve"}
)
AGY_PERMISSION_MODE_PLAN = "plan_no_autoapprove"
AGY_PERMISSION_MODE_UNVERIFIED = "unverified"


def _agy_option_tokens(argv: tuple[str, ...] | list[str], print_flag: str) -> list[str]:
    """Return argv[1:] without the prompt value that follows ``print_flag``."""
    tokens = [str(token) for token in argv[1:]]
    if print_flag and print_flag in tokens:
        index = tokens.index(print_flag)
        tokens = tokens[: index + 1] + tokens[index + 2 :]
    return tokens


def _agy_permission_bypass_flags(tokens: list[str]) -> list[str]:
    """Deny-listed permission flags, including any ``--mode`` other than plan."""
    found: list[str] = []
    for index, token in enumerate(tokens):
        name, has_value, value = token.partition("=")
        if name in AGY_PERMISSION_BYPASS_FLAGS:
            found.append(token)
        elif name == "--mode":
            mode = value if has_value else (tokens[index + 1] if index + 1 < len(tokens) else "")
            if mode != "plan":
                found.append(token if has_value else f"--mode {mode}".rstrip())
    return found


def agy_permission_mode(argv: tuple[str, ...] | list[str], *, print_flag: str = "-p") -> str:
    """Derive the Agy permission posture from the argv that will be executed.

    ``plan_no_autoapprove`` only when the argv carries ``--mode plan`` and
    ``--sandbox`` and no deny-listed flag; ``unverified`` otherwise.
    """
    tokens = _agy_option_tokens(argv, print_flag)
    if _agy_permission_bypass_flags(tokens):
        return AGY_PERMISSION_MODE_UNVERIFIED
    plan = "--mode=plan" in tokens or any(
        tokens[index] == "--mode" and tokens[index + 1] == "plan"
        for index in range(len(tokens) - 1)
    )
    if plan and "--sandbox" in tokens:
        return AGY_PERMISSION_MODE_PLAN
    return AGY_PERMISSION_MODE_UNVERIFIED


# Online A/B S18/S19: the with-Nexus Agy turn runs with the same print-mode argv as the bench's
# bare arm (scripts/bench/agy_provider.py in Nexus-new): a fresh project, a log file outside the
# working directory (observed-model witness), the JSON envelope (response + usage), and Agy's own
# ``--print-timeout``. ``NEXUS_BENCH_PER_CALL_CAP_SEC`` (set by the like-for-like bench) caps it.
AGY_PRINT_TIMEOUT_GRACE_SEC = 15
AGY_PER_CALL_CAP_ENV = "NEXUS_BENCH_PER_CALL_CAP_SEC"
_AGY_RESOLVING_MODEL_RE = re.compile(r"Resolving model (?P<resolved>[^\s]+)")
_AGY_ALIAS_RESOLUTION_RE = re.compile(r'model alias "(?P<alias>[^"\n]+)" resolved to "(?P<resolved>[^"\n]+)"')
_AGY_BACKEND_LABEL_RE = re.compile(r'Propagating selected model override to backend: label="(?P<label>[^"\n]+)"')
AGY_MODEL_IDENTITY_OK = "MODEL_IDENTITY_OK"
AGY_MODEL_IDENTITY_MISMATCH = "MODEL_IDENTITY_MISMATCH"
AGY_MODEL_IDENTITY_UNOBSERVED = "MODEL_IDENTITY_UNOBSERVED"
AGY_LOG_FILE_NAME = "agy.log"


def agy_print_timeout_sec(call_timeout_sec: int | float | None, environ: Mapping[str, str] | None = None) -> int:
    """Agy ``--print-timeout``: the bench per-call cap when set, else the call timeout."""
    values = os.environ if environ is None else environ
    timeout = max(5, int(call_timeout_sec or 0) or 5)
    raw = str(values.get(AGY_PER_CALL_CAP_ENV) or "").strip()
    try:
        cap = int(raw)
    except ValueError:
        cap = 0
    return max(5, min(timeout, cap)) if cap > 0 else timeout


def agy_print_mode_options(*, log_path: str, print_timeout_sec: int) -> list[str]:
    return [
        "--log-file",
        str(log_path),
        "--output-format",
        "json",
        "--print-timeout",
        f"{int(print_timeout_sec)}s",
    ]


def parse_agy_envelope(stdout: str) -> dict[str, Any] | None:
    """The Agy ``--output-format json`` envelope, or None for plain-text output."""
    try:
        envelope = json.loads(str(stdout or "").strip())
    except (TypeError, ValueError):
        return None
    return envelope if isinstance(envelope, dict) and "response" in envelope else None


def agy_observed_model_ids(log_text: str) -> list[str]:
    text = str(log_text or "")
    aliases = {m.group("alias"): m.group("resolved") for m in _AGY_ALIAS_RESOLUTION_RE.finditer(text)}
    resolving = [m.group("resolved") for m in _AGY_RESOLVING_MODEL_RE.finditer(text)]
    return sorted({aliases.get(item, item) for item in resolving} | set(aliases.values()))


def _identity_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def agy_model_identity_verdict(requested_model: str, log_text: str | None) -> dict[str, Any]:
    """Same verdict as the bench bare arm (Nexus-new scripts/bench/agy_account_lease.py).

    ``log_text=None`` means the log could not be read safely (missing or not a regular file).
    """
    ids = agy_observed_model_ids(log_text or "")
    labels = sorted({m.group("label") for m in _AGY_BACKEND_LABEL_RE.finditer(str(log_text or ""))})
    record: dict[str, Any] = {
        "provider_observed_model_ids": ids,
        "provider_observed_model_labels": labels,
    }
    if log_text is None:
        return {**record, "model_identity_status": AGY_MODEL_IDENTITY_UNOBSERVED, "model_identity_reason": "log_unreadable"}
    if not ids or not labels:
        missing = [name for name, values in (("resolved_model", ids), ("backend_label", labels)) if not values]
        return {
            **record,
            "model_identity_status": AGY_MODEL_IDENTITY_UNOBSERVED,
            "model_identity_reason": "missing:" + ",".join(missing),
        }
    if ids != [requested_model]:
        return {**record, "model_identity_status": AGY_MODEL_IDENTITY_MISMATCH, "model_identity_reason": "resolved_model_differs"}
    if any(_identity_token(label) != _identity_token(requested_model) for label in labels):
        return {**record, "model_identity_status": AGY_MODEL_IDENTITY_MISMATCH, "model_identity_reason": "backend_label_differs"}
    return {**record, "model_identity_status": AGY_MODEL_IDENTITY_OK, "model_identity_reason": ""}


def read_private_agy_log(log_path: str) -> str | None:
    """Read the agy log only if it is a regular file (never follow a planted symlink)."""
    try:
        info = os.lstat(log_path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _agy_preflight_failure(
    command: tuple[str, ...],
    *,
    model_name: str,
    has_gateway_authority: bool,
) -> str:
    """Typed fail-closed reason for an Agy invocation, checked before any process."""
    if _agy_permission_bypass_flags([str(part) for part in command[1:]]):
        return "agy_permission_bypass_flag_rejected"
    if len(command) != 1:
        # Keep the pre-existing reason on the gateway-authority edge.
        return (
            "registered_cli_model_binding_command_shape_unsupported"
            if has_gateway_authority
            else "agy_command_shape_unsupported"
        )
    if not model_name:
        return "agy_model_required"
    return ""


def _callable_provider_identity(invoker: Any) -> str:
    values: list[str] = []
    for attribute in ("provider", "online_invoker_provider"):
        try:
            value = getattr(invoker, attribute, None)
        except Exception as exc:
            raise ValueError("online_invoker_provider_malformed") from exc
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("online_invoker_provider_malformed")
            values.append(value)
    if len(set(values)) > 1:
        raise ValueError("online_invoker_provider_ambiguous")
    return values[0] if values else ""


def _registered_cli_model_binding_failure(
    provider: str,
    *,
    model_name: str,
    authority: Any,
) -> str:
    """Validate the model edge before registered CLI discovery or execution."""
    if authority is None:
        return ""
    key = str(provider or "").strip().lower()
    if not isinstance(authority, Mapping):
        return "gateway_invocation_authority_malformed"
    admitted_provider = authority.get("resolved_provider")
    if not isinstance(admitted_provider, str) or not admitted_provider.strip():
        return "gateway_invocation_authority_provider_missing"
    if key != admitted_provider:
        return "gateway_invocation_authority_provider_mismatch"
    if key in REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS:
        return "gateway_invocation_authority_model_binding_unsupported"
    admitted_model = authority.get("resolved_model")
    if not isinstance(admitted_model, str) or not admitted_model.strip():
        return "gateway_invocation_authority_model_missing"
    if not model_name:
        return "gateway_invocation_authority_model_missing"
    if model_name != admitted_model:
        return "gateway_invocation_authority_model_mismatch"
    return ""


def _physical_online_invoker(invoker: Any) -> bool:
    """Classify Online provider callables without trusting a caller opt-out.

        Registered/structured provider adapters carry provider identity and are
        therefore physical by default.  A bounded deterministic compatibility
        callback may explicitly declare ``physical_provider_transport=False`` or
        remain provider-neutral.  Supplying a provider identity can never silently
        opt out of fresh Workforce Admission merely by omitting the marker.
        """
    if invoker is None:
        return False
    marker = getattr(invoker, "physical_provider_transport", None)
    if type(marker) is bool:
        return marker
    provider = str(
        getattr(invoker, "online_invoker_provider", None)
        or getattr(invoker, "provider", None)
        or ""
    ).strip()
    return bool(provider)


@dataclass(frozen=True)
class OnlineCliSpec:
    """Provider-neutral subprocess contract for Online CLI adapters."""

    provider: str
    command: tuple[str, ...]
    timeout_sec: float = 120.0
    working_directory: str = ""
    model_name: str = ""

    def validate(self) -> None:
        if not str(self.provider or "").strip():
            raise ValueError("provider_required")
        if not self.command or any(not str(part).strip() for part in self.command):
            raise ValueError("command_required")
        if self.timeout_sec <= 0:
            raise ValueError("timeout_must_be_positive")
        if self.working_directory:
            wd = str(self.working_directory)
            if not os.path.exists(wd):
                raise ValueError(f"working_directory_not_found:{wd}")
            if not os.path.isdir(wd):
                raise ValueError(f"working_directory_not_directory:{wd}")


@dataclass(frozen=True)
class OnlineTransportBinding:
    """Resolved Online execution binding (identity ≠ transport).

        Fields:
          execution_role: always ``online`` for this binder
          provider: selected provider identity (may be empty, injected, or registry key)
          transport: how the Online call is physically made
          selection_source: why this binding was chosen
          resolution_error: non-empty when transport cannot be resolved
          use_gateway_structured: prefer Gateway ``ask_structured`` compatibility path
          use_registered_cli: prefer registered Online CLI invoker
        """

    execution_role: str
    provider: str
    transport: str
    selection_source: str
    resolution_error: str = ""
    use_gateway_structured: bool = False
    use_registered_cli: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_role": self.execution_role,
            "provider": self.provider,
            "transport": self.transport,
            "selection_source": self.selection_source,
            "resolution_error": self.resolution_error,
            "use_gateway_structured": self.use_gateway_structured,
            "use_registered_cli": self.use_registered_cli,
        }


def build_online_route(
    *,
    recommended_flow: str = "direct",
    gateway_provider: str = "",
    explicit_provider: str = "",
    local_enabled: bool = False,
    selection_source: str = "",
) -> dict[str, Any]:
    """Build an Online route object without conflating local discovery.

        Gateway default / auto-detected local providers (for example Ollama) are
        recorded as environment context but are **not** copied into
        ``route["provider"]`` unless they are registered Online CLI providers or
        the caller supplies an explicit Online provider.
        """
    explicit = str(explicit_provider or "").strip().lower()
    gateway = str(gateway_provider or "").strip().lower()
    source = str(selection_source or "").strip().lower()

    provider = ""
    if explicit:
        provider = explicit
        source = source or SELECTION_EXPLICIT_REQUEST
    elif gateway in ONLINE_CLI_SPEC_REGISTRY and gateway not in LOCAL_ONLY_PROVIDERS:
        provider = gateway
        source = source or SELECTION_ENVIRONMENT_DEFAULT
    else:
        # Local-only or empty gateway default: leave Online provider unset so
        # transport resolution can prefer injected/bound structured transport
        # or gateway compatibility without inventing a false Online identity.
        provider = ""
        source = source or SELECTION_COMPATIBILITY_DEFAULT

    route: dict[str, Any] = {
        "recommended_flow": recommended_flow,
        "execution_role": "online",
        "selection_source": source,
        "local_enabled": bool(local_enabled),
        "gateway_default_provider": gateway,
    }
    if provider:
        route["provider"] = provider
    if gateway and gateway in LOCAL_ONLY_PROVIDERS:
        route["local_provider_detected"] = gateway
    return route


def resolve_online_transport_binding(
    *,
    has_explicit_invoker: bool = False,
    structured_transport_injected: bool = False,
    route_provider: str = "",
    gateway_provider: str = "",
) -> OnlineTransportBinding:
    """Deterministic Online transport resolution.

        Precedence:
          1. explicit online_invoker
          2. injected / bound structured transport
          3. explicit route provider (registered CLI, with gemini gateway special-case)
          4. registered CLI when route provider is registered
          5. gateway compatibility fallback for empty or local-only providers
          6. fail-closed for unknown Online providers
        """
    requested = str(route_provider or "").strip().lower()
    gateway = str(gateway_provider or "").strip().lower()

    if has_explicit_invoker:
        return OnlineTransportBinding(
            execution_role="online",
            provider=requested or "explicit_invoker",
            transport=TRANSPORT_STRUCTURED_CALLABLE,
            selection_source=SELECTION_EXPLICIT_REQUEST,
            use_gateway_structured=False,
            use_registered_cli=False,
        )

    if structured_transport_injected:
        return OnlineTransportBinding(
            execution_role="online",
            # Injected transport identity is not the local auto-detect default.
            provider="injected",
            transport=TRANSPORT_STRUCTURED_CALLABLE,
            selection_source=SELECTION_INJECTED_TRANSPORT,
            use_gateway_structured=True,
            use_registered_cli=False,
        )

    if requested and requested in ONLINE_CLI_SPEC_REGISTRY and not (
        requested in LOCAL_ONLY_PROVIDERS and gateway == requested
    ):
        # Gemini retains specialized Gateway CLI transport when the Gateway
        # itself is also configured for Gemini; other registered providers use
        # the provider-neutral registered CLI edge.
        if requested == "gemini" and gateway == "gemini":
            return OnlineTransportBinding(
                execution_role="online",
                provider="gemini",
                transport=TRANSPORT_GATEWAY_COMPATIBILITY,
                selection_source=SELECTION_ENVIRONMENT_DEFAULT if not requested else SELECTION_EXPLICIT_REQUEST,
                use_gateway_structured=True,
                use_registered_cli=False,
            )
        return OnlineTransportBinding(
            execution_role="online",
            provider=requested,
            transport=TRANSPORT_REGISTERED_CLI,
            selection_source=SELECTION_EXPLICIT_REQUEST,
            use_gateway_structured=False,
            use_registered_cli=True,
        )

    if not requested or requested in LOCAL_ONLY_PROVIDERS:
        return OnlineTransportBinding(
            execution_role="online",
            provider=gateway or requested or "gateway",
            transport=TRANSPORT_GATEWAY_COMPATIBILITY,
            selection_source=SELECTION_COMPATIBILITY_DEFAULT,
            use_gateway_structured=True,
            use_registered_cli=False,
        )

    # Unknown Online provider with no injected transport: fail closed.
    return OnlineTransportBinding(
        execution_role="online",
        provider=requested,
        transport=TRANSPORT_UNRESOLVED,
        selection_source=SELECTION_EXPLICIT_REQUEST,
        resolution_error="provider_not_registered",
        use_gateway_structured=False,
        use_registered_cli=False,
    )


def extract_online_stage_payload(
    online_stage: Mapping[str, Any] | None,
) -> tuple[Any, str, dict[str, Any]]:
    """Canonical Online stage unwrapping for callers.

        UnifiedRuntime stores the invoker result at ``receipt["online"]["response"]``.
        That invoker result itself carries domain output under ``response`` and the
        physical transport text under ``raw_response``.

        Returns:
          (domain_response, raw_response, invoker_payload)
        """
    if not isinstance(online_stage, Mapping):
        return "", "", {}
    invoker_payload = online_stage.get("response", {})
    if not isinstance(invoker_payload, Mapping):
        # Stage response was a bare scalar; treat as domain body.
        return invoker_payload, str(invoker_payload or ""), {}
    domain = invoker_payload.get("response", "")
    raw = str(invoker_payload.get("raw_response", "") or "")
    return domain, raw, dict(invoker_payload)


def resolve_registered_online_cli_spec(
    provider: str,
    *,
    command: tuple[str, ...] | list[str] | str | None = None,
    model_name: str | None = None,
    timeout_sec: float = 120.0,
    environ: Mapping[str, str] | None = None,
    working_directory: str = "",
) -> OnlineCliSpec:
    """Resolve a registered provider command without invoking it.

        Provider-specific flags remain an edge concern.  The resolver accepts an
        explicit argv, then a provider-specific ``*_COMMAND`` environment value,
        and finally a configured/discovered binary. It never shells out or treats
        presence as a live-consumption claim.
        """
    key = str(provider or "").strip().lower()
    metadata = ONLINE_CLI_SPEC_REGISTRY.get(key)
    if metadata is None:
        raise ValueError("provider_not_registered")
    env = dict(environ or os.environ)
    resolved_command: tuple[str, ...]
    if isinstance(command, str):
        resolved_command = tuple(shlex.split(command))
    elif command is not None:
        resolved_command = tuple(str(part) for part in command)
    else:
        configured_command = str(env.get(metadata["command_env"], "") or "").strip()
        configured_binary = str(env.get(metadata["binary_env"], "") or "").strip()
        if configured_command:
            resolved_command = tuple(shlex.split(configured_command))
        elif configured_binary:
            resolved_command = tuple(shlex.split(configured_binary))
        else:
            binary = shutil.which(metadata["binary_name"])
            if not binary:
                raise ValueError("provider_binary_not_found")
            resolved_command = (binary,)
    spec = OnlineCliSpec(
        provider=key,
        command=resolved_command,
        timeout_sec=timeout_sec,
        working_directory=working_directory,
        model_name=_optional_identity(model_name),
    )
    spec.validate()
    return spec


def resolve_registered_provider_executable(
    provider: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the physical executable shared by Gateway and worker adapters.

        AGY historically had two environment aliases (``NEXUS_AGY_BIN`` at the
        Gateway edge and ``NEXUS_AGY_EXECUTABLE`` in the worker).  Treat them as
        aliases of one authority and fail closed when they point at different
        binaries instead of allowing route-specific drift.
        """
    key = str(provider or "").strip().lower()
    metadata = ONLINE_CLI_SPEC_REGISTRY.get(key)
    if metadata is None:
        raise ValueError("provider_not_registered")
    env = dict(environ or os.environ)
    names = [metadata["binary_env"]]
    if key == "agy":
        names.append("NEXUS_AGY_EXECUTABLE")
    candidates: list[str] = []
    for name in names:
        value = str(env.get(name, "") or "").strip()
        if not value:
            continue
        resolved = shutil.which(value) or value
        candidates.append(str(Path(resolved).expanduser().resolve()))
    if candidates and len(set(candidates)) != 1:
        raise ValueError("provider_executable_alias_mismatch")
    executable = candidates[0] if candidates else shutil.which(metadata["binary_name"])
    if not executable:
        raise ValueError("provider_binary_not_found")
    path = Path(executable).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("provider_binary_not_executable")
    return str(path)


_PROCESS_GROUP_TERM_GRACE_SEC = 5.0


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the provider's whole process group (its own session)."""
    import signal

    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        proc.kill()
        return
    for sig, grace in ((signal.SIGTERM, _PROCESS_GROUP_TERM_GRACE_SEC), (signal.SIGKILL, None)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if grace is None:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
        # The leader exited; still reap any child left in the group.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return


def _run_provider_process_group(
    argv: list[str],
    *,
    input: str | None = None,
    cwd: str | None = None,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a physical provider CLI, but a timeout kills the whole group.

    ``subprocess.run(timeout=...)`` kills only the direct child. Provider CLIs are often
    wrappers (shims, node launchers) whose real worker is a grandchild; that worker was
    orphaned and kept its account lease. The provider runs in its own session here, and a
    timeout terminates the full process group before ``TimeoutExpired`` is re-raised.
    """
    pipe = subprocess.PIPE if capture_output else None
    with subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=pipe,
        stderr=pipe,
        cwd=cwd,
        text=text,
        start_new_session=True,
    ) as proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            try:
                exc.stdout, exc.stderr = proc.communicate(timeout=_PROCESS_GROUP_TERM_GRACE_SEC)
            except subprocess.TimeoutExpired:
                pass
            raise
        except BaseException:
            _kill_process_group(proc)
            raise
        retcode = proc.poll()
    if check and retcode:
        raise subprocess.CalledProcessError(retcode, argv, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(argv, retcode, stdout, stderr)


def build_subprocess_online_invoker(
    ctx: KernelContext,
    spec: OnlineCliSpec,
    *,
    runner: Callable[..., Any] = subprocess.run,
    include_local_context: bool = True,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Build a no-shell Online CLI invoker with explicit receipt fields."""

    spec.validate()

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        task_id = str(context.get("task_id", ""))
        context_model = _optional_identity(
            context.get("online_model_name") or context.get("model_name")
        )
        admitted_model = _optional_identity(spec.model_name) or context_model
        authority_failure = _registered_cli_model_binding_failure(
            spec.provider,
            model_name=admitted_model,
            authority=context.get("gateway_invocation_authority"),
        )
        if authority_failure:
            return ctx.normalize_online_invoker_payload(
                provider=spec.provider,
                task_id=task_id,
                invoked=False,
                output_delivered=False,
                gate_passed=False,
                provider_call_count=0,
                response="",
                raw_response="",
                usage={},
                error=authority_failure,
                evidence_refs=[f"online:{spec.provider}:{task_id}:{authority_failure}"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"live_provider_claim": False},
            )
        if spec.provider == "agy":
            agy_failure = _agy_preflight_failure(
                spec.command,
                model_name=admitted_model,
                has_gateway_authority=context.get("gateway_invocation_authority") is not None,
            )
            if agy_failure:
                return ctx.normalize_online_invoker_payload(
                    provider=spec.provider,
                    task_id=task_id,
                    invoked=False,
                    output_delivered=False,
                    gate_passed=False,
                    provider_call_count=0,
                    response="",
                    raw_response="",
                    usage={},
                    error=agy_failure,
                    evidence_refs=[f"online:{spec.provider}:{task_id}:{agy_failure}"],
                    transport=TRANSPORT_REGISTERED_CLI,
                    selection_source=SELECTION_EXPLICIT_REQUEST,
                    extra={"live_provider_claim": False},
                )
        attempt_info = context.get("execution_attempt") if isinstance(context.get("execution_attempt"), Mapping) else {}
        attempt_id = str(
            context.get("attempt_id")
            or attempt_info.get("attempt_id")
            or context.get("planner_decision_id")
            or ""
        )
        prompt = str(context.get("online_prompt") or context.get("task_statement") or "")
        payload = str(context.get("online_payload") or "")
        local_context_forwarded = False
        capability_context_forwarded = False
        vap_injection = ""
        planner_context = context.get("planner")
        canonical_context = (
            context.get("schema") == REQUEST_SCHEMA
            or (isinstance(planner_context, Mapping) and bool(_mapping(planner_context).get("plan_hash")))
        )
        if include_local_context:
            local_stage = context.get("local", {})
            # P2: never dump raw local_outputs (patch/CoT/private reasoning) into Online.
            # Only structure-preserving online-safe concise evidence may be forwarded.
            if isinstance(local_stage, Mapping) and (
                local_stage.get("response") or local_stage.get("invoked") or local_stage.get("local_outputs")
            ):
                build_online_safe_local_forward = ctx.build_online_safe_local_forward

                safe = build_online_safe_local_forward(
                    local_stage,
                    runtime_task_id=task_id,
                    runtime_canonical_execution=context.get("canonical_execution") if isinstance(context.get("canonical_execution"), Mapping) else None,
                    runtime_execution_attempt=context.get("execution_attempt") if isinstance(context.get("execution_attempt"), Mapping) else None,
                    runtime_source_hash=str(context.get("source_hash") or ""),
                    runtime_execution_world=str(_mapping(context.get("canonical_execution")).get("execution_world") or "product_runtime"),
                    final_prompt=prompt,
                )
                forward = safe.get("forward", {}) if isinstance(safe, Mapping) else {}
                verified_assist = (
                    safe.get("verified_assist")
                    if isinstance(safe, Mapping) and isinstance(safe.get("verified_assist"), Mapping)
                    else {}
                )
                vap_injection = str(verified_assist.get("injection_fragment") or "")
                if isinstance(forward, Mapping) and (
                    forward.get("concise_summary")
                    or forward.get("candidate_hash")
                    or str(forward.get("verifier_status") or "") not in {"", "not_run"}
                ):
                    prompt += "\n\n[LOCAL_ASSIST_CONTEXT]\n" + json.dumps(
                        forward,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    local_context_forwarded = True
                if vap_injection and vap_injection not in prompt:
                    prompt = f"{prompt}\n{vap_injection}"
            capability_results = context.get("capability_results", {})
            if capability_results and not canonical_context:
                compressed = bool(context.get("capability_context_compressed"))
                # Compressed path: evidence summary only. Uncompressed: capability
                # stage receipts (status/refs/task_id), not private Local CoT fields.
                prompt += (
                    "\n\n[CAPABILITY_EVIDENCE_SUMMARY]\n"
                    if compressed
                    else "\n\n[CAPABILITY_CONTEXT]\n"
                ) + json.dumps(
                    _capability_evidence_summary(capability_results)
                    if compressed
                    else capability_results,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                capability_context_forwarded = True
        stdin = f"{prompt}\n\n[PAYLOAD]\n{payload}" if payload else prompt

        # Canonical Planner contexts require the shared package before provider invocation.
        if canonical_context and context.get("capability_evidence_bundle") is None:
            return ctx.normalize_online_invoker_payload(
                provider=spec.provider, task_id=task_id, invoked=False,
                output_delivered=False, gate_passed=False, provider_call_count=0,
                response="", raw_response="", usage={},
                error="canonical_runtime_context_bundle_missing",
                evidence_refs=[f"online:{spec.provider}:{task_id}:context_bundle_missing"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"live_provider_claim": False},
            )
        context_package = None
        build_online_consumption_receipt = None
        if context.get("capability_evidence_bundle") is not None:
            try:
                from nexus_runtime.task_context import append_model_context_to_prompt, build_online_context_package
                from nexus_runtime.task_context.consumption import build_online_consumption_receipt
                context_package = build_online_context_package(context)
                stdin = append_model_context_to_prompt(stdin, context_package)
            except ModuleNotFoundError:
                return ctx.normalize_online_invoker_payload(
                    provider=spec.provider, task_id=task_id, invoked=False,
                    output_delivered=False, gate_passed=False, provider_call_count=0,
                    response="", raw_response="", usage={},
                    error="canonical_runtime_dependency_missing",
                    evidence_refs=[f"online:{spec.provider}:{task_id}:context_package_dependency_missing"],
                    transport=TRANSPORT_REGISTERED_CLI, selection_source=SELECTION_EXPLICIT_REQUEST,
                    extra={"live_provider_claim": False},
                )
            except ValueError as exc:
                return ctx.normalize_online_invoker_payload(
                    provider=spec.provider, task_id=task_id, invoked=False,
                    output_delivered=False, gate_passed=False, provider_call_count=0,
                    response="", raw_response="", usage={},
                    error=f"canonical_runtime_context_package_invalid:{exc}",
                    evidence_refs=[f"online:{spec.provider}:{task_id}:context_package_invalid"],
                    transport=TRANSPORT_REGISTERED_CLI, selection_source=SELECTION_EXPLICIT_REQUEST,
                    extra={"live_provider_claim": False},
                )

        meta = ONLINE_CLI_SPEC_REGISTRY.get(spec.provider, {})
        print_flag = str(meta.get("print_flag") or "").strip()
        argv = list(spec.command)
        agy_log_path = ""
        agy_log_dir = ""
        agy_print_timeout = agy_print_timeout_sec(spec.timeout_sec)
        if spec.provider == "agy":
            # Outside the ephemeral working directory, like the bench's bare arm, inside a private
            # (0700) directory so no other local user can pre-plant the log or a symlink there.
            agy_log_dir = tempfile.mkdtemp(prefix="nexus-agy-online-log-")
            agy_log_path = str(Path(agy_log_dir) / AGY_LOG_FILE_NAME)
        stdin_input: str | None = None
        prompt_transport = "stdin"

        model_binding = REGISTERED_CLI_MODEL_BINDING_FLAGS.get(spec.provider)
        if context.get("gateway_invocation_authority") is not None and admitted_model and model_binding and len(argv) != 1:
            if spec.provider == "agy":
                shutil.rmtree(agy_log_dir, ignore_errors=True)
                return ctx.normalize_online_invoker_payload(
                    provider=spec.provider,
                    task_id=task_id,
                    invoked=False,
                    output_delivered=False,
                    gate_passed=False,
                    provider_call_count=0,
                    response="",
                    raw_response="",
                    usage={},
                    error="registered_cli_model_binding_command_shape_unsupported",
                    evidence_refs=[
                        f"online:{spec.provider}:{task_id}:"
                        "registered_cli_model_binding_command_shape_unsupported"
                    ],
                    transport=TRANSPORT_REGISTERED_CLI,
                    selection_source=SELECTION_EXPLICIT_REQUEST,
                    extra={"live_provider_claim": False},
                )
            subcommand, model_flag = model_binding
            accepted_model_flags = {model_flag}
            if spec.provider == "codex":
                accepted_model_flags.add("--model")
            if subcommand:
                if model_flag:
                    binding_shape_ok = (
                        len(argv) >= 4
                        and argv[1] == subcommand
                        and argv[2] in accepted_model_flags
                        and argv[3] == admitted_model
                    )
                else:
                    binding_shape_ok = len(argv) >= 3 and argv[1] == subcommand and argv[2] == admitted_model
            else:
                binding_shape_ok = (
                    len(argv) >= 3
                    and argv[1] in accepted_model_flags
                    and argv[2] == admitted_model
                )
            if not binding_shape_ok:
                return ctx.normalize_online_invoker_payload(
                    provider=spec.provider,
                    task_id=task_id,
                    invoked=False,
                    output_delivered=False,
                    gate_passed=False,
                    provider_call_count=0,
                    response="",
                    raw_response="",
                    usage={},
                    error="registered_cli_model_binding_command_shape_unsupported",
                    evidence_refs=[
                        f"online:{spec.provider}:{task_id}:"
                        "registered_cli_model_binding_command_shape_unsupported"
                    ],
                    transport=TRANSPORT_REGISTERED_CLI,
                    selection_source=SELECTION_EXPLICIT_REQUEST,
                    extra={"live_provider_claim": False},
                )
            stdin_input = stdin
            prompt_transport = "stdin"
        elif print_flag and len(argv) == 1:
            if model_binding and admitted_model:
                subcommand, model_flag = model_binding
                if spec.provider == "agy":
                    argv = [
                        argv[0],
                        "--new-project",
                        "--mode",
                        "plan",
                        "--sandbox",
                        model_flag,
                        admitted_model,
                        *agy_print_mode_options(log_path=agy_log_path, print_timeout_sec=agy_print_timeout),
                        print_flag,
                        stdin,
                    ]
                else:
                    argv = (
                        ([argv[0], subcommand, model_flag, admitted_model, stdin] if model_flag else [argv[0], subcommand, admitted_model, stdin])
                        if subcommand
                        else [argv[0], model_flag, admitted_model, stdin]
                    )
            elif spec.provider == "agy":
                argv = [
                    argv[0],
                    "--new-project",
                    "--mode",
                    "plan",
                    "--sandbox",
                    *agy_print_mode_options(log_path=agy_log_path, print_timeout_sec=agy_print_timeout),
                    print_flag,
                    stdin,
                ]
            else:
                argv = [argv[0], print_flag, stdin]
            stdin_input = ""
            prompt_transport = "argv"
        elif spec.provider == "opencode" and len(argv) == 1:
            subcommand, model_flag = REGISTERED_CLI_MODEL_BINDING_FLAGS["opencode"]
            model = admitted_model or str(meta.get("default_model", "") or "").strip()
            argv = [argv[0], subcommand, model_flag, model, stdin]
            stdin_input = ""
            prompt_transport = "argv"
        elif spec.provider == "cline" and len(argv) == 1:
            model = admitted_model or str(meta.get("default_model", "glm-5.2") or "glm-5.2").strip()
            argv = [argv[0], "--json", "--yolo", "--model", model, stdin]
            stdin_input = ""
            prompt_transport = "argv"
        elif spec.provider in {"mimo", "ollama"} and len(argv) == 1:
            model = admitted_model or str(meta.get("default_model", "") or "").strip()
            if not model and spec.provider == "ollama":
                from nexus_runtime.planning.services.model_default_resolution import (
                    ModelDefaultNotAdmittedError,
                    resolve_default_model,
                )

                try:
                    model = resolve_default_model()
                except ModelDefaultNotAdmittedError as exc:
                    return ctx.normalize_online_invoker_payload(
                        provider=spec.provider,
                        task_id=task_id,
                        invoked=False,
                        output_delivered=False,
                        gate_passed=False,
                        provider_call_count=0,
                        response="",
                        raw_response="",
                        usage={},
                        error=str(exc),
                        evidence_refs=[
                            f"online:{spec.provider}:{task_id}:MODEL_DEFAULT_NOT_ADMITTED"
                        ],
                        transport=TRANSPORT_REGISTERED_CLI,
                        selection_source=SELECTION_EXPLICIT_REQUEST,
                        extra={
                            "live_provider_claim": False,
                            "error_code": exc.code,
                            "role": exc.role,
                        },
                    )
            if spec.provider == "mimo":
                argv = [argv[0], "run", "--model", model, stdin]
            else:
                argv = [argv[0], "run", model, stdin]
            stdin_input = ""
            prompt_transport = "argv"
        else:
            stdin_input = stdin
            prompt_transport = "stdin"

        permission_mode = "provider_default"
        if spec.provider == "agy":
            permission_mode = agy_permission_mode(argv, print_flag=print_flag)
            if permission_mode != AGY_PERMISSION_MODE_PLAN:
                if agy_log_dir:
                    shutil.rmtree(agy_log_dir, ignore_errors=True)
                return ctx.normalize_online_invoker_payload(
                    provider=spec.provider,
                    task_id=task_id,
                    invoked=False,
                    output_delivered=False,
                    gate_passed=False,
                    provider_call_count=0,
                    response="",
                    raw_response="",
                    usage={},
                    error="agy_permission_mode_unverified",
                    evidence_refs=[f"online:{spec.provider}:{task_id}:agy_permission_mode_unverified"],
                    transport=TRANSPORT_REGISTERED_CLI,
                    selection_source=SELECTION_EXPLICIT_REQUEST,
                    extra={"live_provider_claim": False, "provider_permission_mode": permission_mode},
                )
        cmd_fp = hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode("utf-8")).hexdigest()
        exec_path = str(shutil.which(argv[0]) or argv[0])
        exec_hash = hashlib.sha256(exec_path.encode("utf-8")).hexdigest()
        agy_isolated_working_directory = ""
        effective_working_directory = str(spec.working_directory or "")
        # Agy always runs in a runtime-owned ephemeral directory (#181); a
        # caller-bound working directory is never handed to the provider.
        if spec.provider == "agy":
            agy_isolated_working_directory = tempfile.mkdtemp(
                prefix="nexus-agy-online-"
            )
            effective_working_directory = agy_isolated_working_directory
        cwd_str = str(effective_working_directory or os.getcwd())
        cwd_hash = hashlib.sha256(cwd_str.encode("utf-8")).hexdigest()
        input_sha256 = hashlib.sha256(stdin.encode("utf-8")).hexdigest()

        def attach_context_receipt(result_payload: dict[str, Any]) -> dict[str, Any]:
            if context_package is not None and build_online_consumption_receipt is not None:
                result_payload["model_context_consumption"] = build_online_consumption_receipt(
                    context_package,
                    result_payload,
                    physical_transport=runner is subprocess.run,
                    expected_provider_input_sha256=input_sha256,
                )
            return result_payload

        proc_inv_id = hashlib.sha256(
            json.dumps([task_id, attempt_id, spec.provider, cmd_fp, input_sha256, cwd_hash]).encode("utf-8")
        ).hexdigest()

        start_time = time.monotonic()

        def _build_process_evidence(
            started: bool,
            stdout_str: str,
            stderr_str: str,
            retcode: int | None,
            elapsed_ms: int,
        ) -> dict[str, Any]:
            return {
                "schema": "nexus.provider_process_evidence.v1",
                "provider": spec.provider,
                "transport": TRANSPORT_REGISTERED_CLI,
                "process_started": started,
                "prompt_transport": prompt_transport,
                "command_fingerprint": cmd_fp,
                "executable_path_hash": exec_hash,
                "working_directory_hash": cwd_hash,
                "sandboxed_working_directory": bool(effective_working_directory),
                "working_directory_isolation": (
                    "runtime_ephemeral"
                    if agy_isolated_working_directory
                    else "caller_bound"
                    if spec.working_directory
                    else "inherited"
                ),
                "provider_permission_mode": permission_mode,
                "provider_input_sha256": input_sha256,
                "stdout_sha256": hashlib.sha256(stdout_str.encode("utf-8")).hexdigest() if started else "",
                "stderr_sha256": hashlib.sha256(stderr_str.encode("utf-8")).hexdigest() if started else "",
                "returncode": retcode,
                "wall_time_ms": elapsed_ms,
                "attempt_id": attempt_id,
                "process_invocation_id": proc_inv_id,
            }

        try:
            # The physical transport runs the provider in its own process group so a
            # timeout cannot orphan a grandchild worker (Nexus-new online A/B 20261013b).
            physical_runner = _run_provider_process_group if runner is subprocess.run else runner
            result = physical_runner(
                argv,
                input=stdin_input,
                cwd=effective_working_directory or None,
                capture_output=True,
                text=True,
                timeout=(
                    agy_print_timeout + AGY_PRINT_TIMEOUT_GRACE_SEC
                    if agy_log_path
                    else spec.timeout_sec
                ),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            if agy_isolated_working_directory:
                shutil.rmtree(agy_isolated_working_directory, ignore_errors=True)
            if agy_log_dir:
                shutil.rmtree(agy_log_dir, ignore_errors=True)
            elapsed = int((time.monotonic() - start_time) * 1000)
            pe = _build_process_evidence(True, "", str(exc), None, max(0, elapsed))
            return attach_context_receipt(ctx.normalize_online_invoker_payload(
                provider=spec.provider,
                task_id=task_id,
                invoked=True,
                output_delivered=False,
                gate_passed=False,
                provider_call_count=1,
                response="",
                raw_response="",
                usage={},
                error="provider_timeout",
                evidence_refs=[f"online:{spec.provider}:{task_id}:timeout"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"returncode": None, "stderr": str(exc), "process_evidence": pe},
            ))
        except OSError as exc:
            if agy_isolated_working_directory:
                shutil.rmtree(agy_isolated_working_directory, ignore_errors=True)
            if agy_log_dir:
                shutil.rmtree(agy_log_dir, ignore_errors=True)
            elapsed = int((time.monotonic() - start_time) * 1000)
            pe = _build_process_evidence(False, "", str(exc), None, max(0, elapsed))
            return attach_context_receipt(ctx.normalize_online_invoker_payload(
                provider=spec.provider,
                task_id=task_id,
                invoked=False,
                output_delivered=False,
                gate_passed=False,
                provider_call_count=0,
                response="",
                raw_response="",
                usage={},
                error="provider_not_invoked",
                evidence_refs=[f"online:{spec.provider}:{task_id}:not_invoked"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"returncode": None, "stderr": str(exc), "process_evidence": pe},
            ))
        if agy_isolated_working_directory:
            shutil.rmtree(agy_isolated_working_directory, ignore_errors=True)
        elapsed = int((time.monotonic() - start_time) * 1000)
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        returncode = int(getattr(result, "returncode", 1))
        response_text = stdout
        provider_usage: dict[str, Any] = {}
        agy_extra: dict[str, Any] = {}
        agy_identity_failure = ""
        if agy_log_path:
            try:
                log_text = read_private_agy_log(agy_log_path)
            finally:
                shutil.rmtree(agy_log_dir, ignore_errors=True)
            envelope = parse_agy_envelope(stdout)
            identity = agy_model_identity_verdict(admitted_model, log_text)
            agy_extra = {
                "provider_output_format": "agy_json_envelope" if envelope is not None else "text",
                "provider_print_timeout_sec": agy_print_timeout,
                **identity,
            }
            if envelope is not None and identity["model_identity_status"] != AGY_MODEL_IDENTITY_OK:
                # A real agy JSON turn whose model cannot be attested is never delivered (fails
                # closed like the bench bare arm). Plain-text output (no envelope, e.g. an older
                # agy or a test stub) keeps the legacy behaviour with the status recorded.
                agy_identity_failure = identity["model_identity_status"].lower()
            if envelope is not None:
                response_text = str(envelope.get("response") or "")
                usage = envelope.get("usage")
                provider_usage = dict(usage) if isinstance(usage, Mapping) else {}
                agy_extra["provider_envelope_status"] = str(envelope.get("status") or "")
                agy_extra["provider_conversation_id"] = str(envelope.get("conversation_id") or "")
        permission_denied = online_text_indicates_headless_permission_denial(
            stdout
        ) or online_text_indicates_headless_permission_denial(stderr)
        delivered = bool(response_text.strip()) and not permission_denied
        if agy_identity_failure:
            delivered = False
            failure_error = agy_identity_failure
        elif permission_denied:
            failure_error = "provider_headless_permission_denied"
        elif returncode == 0 and delivered:
            failure_error = ""
        else:
            failure_error = "provider_subprocess_failed"
        pe = _build_process_evidence(True, stdout, stderr, returncode, max(0, elapsed))
        return attach_context_receipt(ctx.normalize_online_invoker_payload(
            provider=spec.provider,
            task_id=task_id,
            invoked=True,
            output_delivered=delivered,
            gate_passed=returncode == 0 and delivered,
            provider_call_count=1,
            response=response_text,
            raw_response=response_text,
            usage=provider_usage,
            error=failure_error,
            evidence_refs=(
                [f"online:{spec.provider}:{task_id}:subprocess"]
                + ([f"online:{spec.provider}:{task_id}:local_context_forwarded"] if local_context_forwarded else [])
                + ([f"online:{spec.provider}:{task_id}:capability_context_forwarded"] if capability_context_forwarded else [])
                + ([f"online:{spec.provider}:{task_id}:compressed_context_applied"] if context.get("capability_context_compressed") else [])
            ),
            transport=TRANSPORT_REGISTERED_CLI,
            selection_source=SELECTION_EXPLICIT_REQUEST,
            extra={
                "returncode": returncode,
                "stderr": stderr,
                "process_evidence": pe,
                "assembled_online_prompt": stdin,
                **agy_extra,
            },
        ))

    invoke.provider = spec.provider  # type: ignore[attr-defined]
    invoke.online_invoker_provider = spec.provider  # type: ignore[attr-defined]
    invoke.physical_provider_transport = runner is subprocess.run  # type: ignore[attr-defined]
    invoke._nexus_model_context_wrapped = True  # type: ignore[attr-defined]
    return invoke


def build_registered_online_invoker(
    ctx: KernelContext,
    provider: str,
    *,
    command: tuple[str, ...] | list[str] | str | None = None,
    model_name: str | None = None,
    timeout_sec: float = 120.0,
    environ: Mapping[str, str] | None = None,
    runner: Callable[..., Any] = subprocess.run,
    include_local_context: bool = True,
    working_directory: str = "",
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Build the single provider-neutral Online edge adapter.

        Provider-specific command discovery is intentionally kept at this edge.
        The returned callable has the same contract for Gemini, Grok, Codex, and
        OpenAI and is consumed by :class:`UnifiedRuntime`; it does not create a
        second planner, receipt, verifier, or learning path.

        Binary resolution (``shutil.which``) and registry validation are deferred
        past the authorization check so that ``online_policy=deny`` never fails
        due to a missing binary or unregistered provider that would never be
        invoked — *deny-lazy*.
        """
    key = str(provider or "").strip().lower()

    # Non-subprocess (test/deterministic) runners resolve eagerly; they never
    # hit the physical authorization gate and always supply an explicit argv.
    if runner is not subprocess.run:
        spec = resolve_registered_online_cli_spec(
            key,
            command=command,
            model_name=model_name,
            timeout_sec=timeout_sec,
            environ=environ,
        )
        return build_subprocess_online_invoker(
            ctx,
            spec,
            runner=runner,
            include_local_context=include_local_context,
        )

    # Real provider subprocesses: lazy registry validation + binary resolution
    # after authorization.  Exceptions from the resolver never escape the
    # invoker boundary; they produce a fail-closed receipt instead.
    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        decision_from_context = ctx.decision_from_context
        physical_online_authorized = ctx.physical_online_authorized

        task_id = str(context.get("task_id", ""))
        context_model = _optional_identity(
            context.get("online_model_name") or context.get("model_name")
        )
        admitted_model = _optional_identity(model_name) or context_model
        authority_failure = _registered_cli_model_binding_failure(
            key,
            model_name=admitted_model,
            authority=context.get("gateway_invocation_authority"),
        )
        if authority_failure:
            return ctx.normalize_online_invoker_payload(
                provider=key,
                task_id=task_id,
                invoked=False,
                output_delivered=False,
                gate_passed=False,
                provider_call_count=0,
                response="",
                raw_response="",
                usage={},
                error=authority_failure,
                evidence_refs=[f"online:{key}:{task_id}:{authority_failure}"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"live_provider_claim": False},
            )
        decision = decision_from_context(context if isinstance(context, Mapping) else {})
        injected_only = bool(
            decision is not None
            and decision.online_execution_authorized
            and decision.online_authorization_source == "injected_test_transport"
        )
        # A real provider subprocess needs a physical authorization (task auto|require,
        # workspace policy, operator override).  A fixture (injected_test_transport)
        # decision never starts a process (#190); fixtures that want a fake executable pass
        # a non-subprocess ``runner=`` (resolved eagerly above, classified non-physical).
        if not physical_online_authorized(context, injected_transport=False):
            error = (
                "injected_test_transport_cannot_start_provider_process"
                if injected_only
                else "online_execution_not_authorized"
            )
            return ctx.normalize_online_invoker_payload(
                provider=key,
                task_id=task_id,
                invoked=False,
                output_delivered=False,
                gate_passed=False,
                provider_call_count=0,
                response="",
                raw_response="",
                usage={},
                error=error,
                evidence_refs=[f"online:{key}:{task_id}:authorization_required"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"live_provider_claim": False},
            )

        try:
            spec = resolve_registered_online_cli_spec(
                key,
                command=command,
                model_name=admitted_model,
                timeout_sec=timeout_sec,
                environ=environ,
            )
        except ValueError as exc:
            error_code = str(exc)
            return ctx.normalize_online_invoker_payload(
                provider=key,
                task_id=task_id,
                invoked=False,
                output_delivered=False,
                gate_passed=False,
                provider_call_count=0,
                response="",
                raw_response="",
                usage={},
                error=error_code,
                evidence_refs=[f"online:{key}:{task_id}:{error_code}"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"live_provider_claim": False},
            )

        try:
            invoker = build_subprocess_online_invoker(
                ctx,
                spec,
                runner=runner,
                include_local_context=include_local_context,
            )
        except (TypeError, OSError) as exc:
            error_code = f"{exc.__class__.__name__}:{exc}"
            return ctx.normalize_online_invoker_payload(
                provider=key,
                task_id=task_id,
                invoked=False,
                output_delivered=False,
                gate_passed=False,
                provider_call_count=0,
                response="",
                raw_response="",
                usage={},
                error=error_code,
                evidence_refs=[f"online:{key}:{task_id}:build_failed"],
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"live_provider_claim": False},
            )

        return invoker(context)

    invoke.provider = key  # type: ignore[attr-defined]
    invoke.online_invoker_provider = key  # type: ignore[attr-defined]
    invoke.physical_provider_transport = True  # type: ignore[attr-defined]
    invoke._nexus_model_context_wrapped = True  # type: ignore[attr-defined]
    return invoke


def build_structured_online_invoker(
    ctx: KernelContext,
    ask_structured: Callable[..., Any],
    *,
    phase: str = "R",
    model_name: str | None = None,
    output_schema: Mapping[str, Any] | None = None,
    provider: str = "gateway",
    transport: str = TRANSPORT_STRUCTURED_CALLABLE,
    selection_source: str = SELECTION_EXPLICIT_REQUEST,
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """Adapt a compatibility structured transport into the canonical seam.

        This is an edge adapter for old fixtures or transports only.  Planner,
        receipt, verifier, and learning ownership stays in ``UnifiedRuntime``.
        """

    def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
        result = ask_structured(
            prompt=str(context.get("online_prompt") or context.get("task_statement") or ""),
            payload=str(context.get("online_payload") or ""),
            phase=str(context.get("online_phase") or phase),
            output_schema=dict(context.get("online_output_schema") or output_schema or {}),
            model_name=context.get("online_model_name") or model_name,
        )
        if isinstance(result, tuple) and len(result) >= 2:
            structured, raw = result[0], result[1]
        else:
            structured, raw = result, ""
        response = structured if isinstance(structured, Mapping) else str(raw or structured or "")
        delivered = bool(response)
        task_id = str(context.get("task_id", ""))
        usage: dict[str, Any] = {}
        if isinstance(structured, Mapping):
            maybe_usage = structured.get("usage")
            if isinstance(maybe_usage, Mapping):
                usage = dict(maybe_usage)
            for key in ("tokens_used", "token_capture_status", "gateway_token_source"):
                if key in structured and key not in usage:
                    usage[key] = structured.get(key)
        return ctx.normalize_online_invoker_payload(
            provider=provider,
            task_id=task_id,
            invoked=True,
            output_delivered=delivered,
            gate_passed=delivered,
            provider_call_count=1 if delivered else 0,
            response=response,
            raw_response=str(raw or ""),
            usage=usage,
            error="" if delivered else "structured_transport_empty_response",
            evidence_refs=[f"online:{provider}:{task_id}:structured_transport"],
            transport=transport,
            selection_source=selection_source,
        )

    invoke.provider = provider  # type: ignore[attr-defined]
    invoke.online_invoker_provider = provider  # type: ignore[attr-defined]
    invoke.physical_provider_transport = True  # type: ignore[attr-defined]
    return invoke
