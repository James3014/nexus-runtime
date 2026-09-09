from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping
from .ports import RuntimeBindings, require_complete_bindings

@dataclass(frozen=True, slots=True)
class RuntimeExports:
    _values: Mapping[str, object]
    def __getattr__(self, name: str) -> object:
        try: return self._values[name]
        except KeyError as exc: raise AttributeError(name) from exc
    def names(self) -> tuple[str, ...]: return tuple(sorted(self._values))

def build_runtime(bindings: RuntimeBindings) -> RuntimeExports:
    _nexus_generated_bindings = require_complete_bindings(bindings, RuntimeBindings)
    """Canonical task-scoped runtime seam for Online and Local execution.

    The seam is deliberately provider-neutral.  Adapters supply callables for
    Online execution, verification, and learning; the runtime owns task identity,
    one planner invocation, stage ordering, and one fail-closed receipt.
    """


    import contextlib
    import hashlib
    import json
    import os
    import shlex
    import shutil
    import stat
    import subprocess
    import tempfile
    import time
    from dataclasses import dataclass, field, replace
    from pathlib import Path
    from typing import Any, Callable, Mapping

    CanonicalPlanningBundle = _nexus_generated_bindings.CanonicalPlanningBundle
    CanonicalTaskContext = _nexus_generated_bindings.CanonicalTaskContext
    EXECUTION_DEPTH_FULL = _nexus_generated_bindings.EXECUTION_DEPTH_FULL
    EXECUTION_DEPTH_LIGHT = _nexus_generated_bindings.EXECUTION_DEPTH_LIGHT
    EXECUTION_DEPTH_STANDARD = _nexus_generated_bindings.EXECUTION_DEPTH_STANDARD
    ExecutionReplanAuthorization = _nexus_generated_bindings.ExecutionReplanAuthorization
    apply_execution_depth_floor = _nexus_generated_bindings.apply_execution_depth_floor
    next_execution_depth_after_failure = _nexus_generated_bindings.next_execution_depth_after_failure
    CapabilityPlanner = _nexus_generated_bindings.CapabilityPlanner
    replan_canonical_task_bundle = _nexus_generated_bindings.replan_canonical_task_bundle
    WorkforcePolicyLoader = _nexus_generated_bindings.WorkforcePolicyLoader
    RuntimeWorkforceAdmissionRecord = _nexus_generated_bindings.RuntimeWorkforceAdmissionRecord
    _aggregate_hash = _nexus_generated_bindings._aggregate_hash
    _as_json_value = _nexus_generated_bindings._as_json_value
    _binding_payload = _nexus_generated_bindings._binding_payload
    _parse_demands = _nexus_generated_bindings._parse_demands
    _sha256_json = _nexus_generated_bindings._sha256_json
    evaluate_runtime_workforce_admission = _nexus_generated_bindings.evaluate_runtime_workforce_admission
    _ONLINE_NON_DELIVERY_MARKERS = _nexus_generated_bindings._ONLINE_NON_DELIVERY_MARKERS
    normalize_online_invoker_payload = _nexus_generated_bindings.normalize_online_invoker_payload
    online_payload_indicates_non_delivery = _nexus_generated_bindings.online_payload_indicates_non_delivery
    attach_r3_receipt_base = _nexus_generated_bindings.attach_r3_receipt_base
    build_execution_attempt_id = _nexus_generated_bindings.build_execution_attempt_id
    validate_receipt_base = _nexus_generated_bindings.validate_receipt_base
    attach_failure_diagnostics = _nexus_generated_bindings.attach_failure_diagnostics

    REQUEST_SCHEMA = "nexus.unified_runtime.request.v1"
    RECEIPT_SCHEMA = "nexus.unified_runtime.receipt.v1"
    LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA = "nexus.local_model_invocation_authority.v1"
    RUNTIME_WORKFORCE_ADMISSION_SCHEMA = "nexus.runtime_workforce_admission.v1"
    RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA = "nexus.runtime_workforce_admission_record.v1"
    WORKFORCE_ADMISSION_DECISION_SCHEMA = "nexus.workforce_admission_decision.v1"


    def _assert_runtime_receipt_owner(owner_context: Any, path: Path) -> None:
        """Validate an opted-in receipt write through the shared owner context.

        Runtime keeps its legacy receipt path when no context is supplied.  When
        an owner transaction is supplied, the shared state-owner adapter is the
        authority for the exact role/path and must reject before any parent
        directory creation or receipt bytes.
        """
        if owner_context is None:
            return
        assert_owner_write = _nexus_generated_bindings.assert_owner_write

        try:
            root = owner_context.binding.root.resolve(strict=True)
            if not path.is_absolute():
                raise ValueError("receipt path must be absolute for an owner write")
            relative_path = path.relative_to(root).as_posix()
            cursor = root
            for part in Path(relative_path).parts[:-1]:
                cursor = cursor / part
                if cursor.exists() or cursor.is_symlink():
                    info = cursor.lstat()
                    if not cursor.is_dir() or cursor.is_symlink():
                        raise ValueError("runtime receipt parent is unsafe")
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if path.is_symlink() or not path.is_file() or not stat.S_ISREG(info.st_mode):
                    raise ValueError("runtime receipt target is unsafe")
        except (AttributeError, OSError, ValueError) as exc:
            raise ValueError("runtime_receipt_owner_path_invalid") from exc
        assert_owner_write(owner_context, role="runtime_receipt", relative_path=relative_path)


    def _validate_runtime_owner_context(owner_context: Any, receipt_path: str | Path | None) -> None:
        if owner_context is None:
            return
        if receipt_path is None:
            raise ValueError("runtime_receipt_path_required")
        _assert_runtime_receipt_owner(owner_context, Path(receipt_path))


    def _write_receipt_atomic(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


    @contextlib.contextmanager
    def _runtime_write_context(
        runtime_writer_factory: Any,
        *,
        task_id: str,
        role: str,
        path: Path,
        owner_context: Any = None,
    ):
        """Enter a fresh source-owned lease only at the durable write boundary."""
        if owner_context is not None and runtime_writer_factory is not None:
            raise ValueError("runtime_owner_context_and_factory_conflict")
        if owner_context is not None:
            yield owner_context
            return
        if runtime_writer_factory is not None:
            RuntimeWriterFactory = _nexus_generated_bindings.RuntimeWriterFactory
            if not isinstance(runtime_writer_factory, RuntimeWriterFactory):
                raise ValueError("runtime_writer_factory_invalid")
            runtime_writer_factory.validate_entry(path=path, role=role)
            with runtime_writer_factory.for_operation(task_id, role=role, path=path) as context:
                yield context
            return
        _validate_runtime_writer_entry(
            None,
            owner_context=None,
            receipt_path=path if role == "runtime_receipt" else None,
            effect_journal=None,
            selected_effect_path=path if role == "effect_journal" else None,
        )
        yield None


    def _assert_runtime_context_active(factory: Any, context: Any) -> None:
        if factory is not None:
            factory.assert_context(context)


    def _validate_runtime_effect_owner(owner_context: Any, effect_journal: Any) -> None:
        if owner_context is None or effect_journal is None:
            return
        EffectJournal = _nexus_generated_bindings.EffectJournal
        assert_owner_write = _nexus_generated_bindings.assert_owner_write

        if not isinstance(effect_journal, EffectJournal):
            return
        try:
            binding_root = owner_context.binding.root.resolve(strict=True)
            journal_root = effect_journal.project_root.resolve(strict=True)
            if journal_root != binding_root:
                raise ValueError("runtime_effect_owner_root_mismatch")
            if effect_journal.generation != owner_context.writer_generation:
                raise ValueError("runtime_effect_owner_generation_mismatch")
            assert_owner_write(
                owner_context,
                role="effect_journal",
                relative_path=".nexus/events/effect_journal.v1.json",
            )
        except (AttributeError, OSError, ValueError) as exc:
            if str(exc).startswith("runtime_effect_owner_"):
                raise
            raise ValueError("runtime_effect_owner_binding_invalid") from exc


    def _validate_runtime_writer_entry(
        runtime_writer_factory: Any,
        *,
        owner_context: Any,
        receipt_path: str | Path | None,
        effect_journal: Any,
        selected_effect_path: Path | None = None,
    ) -> None:
        """Fail closed before Planner/model/effect work for an activated root."""
        if runtime_writer_factory is not None:
            RuntimeWriterFactory = _nexus_generated_bindings.RuntimeWriterFactory
            if not isinstance(runtime_writer_factory, RuntimeWriterFactory):
                raise ValueError("runtime_writer_factory_invalid")
            if owner_context is not None:
                raise ValueError("runtime_owner_context_and_factory_conflict")
            if receipt_path is not None:
                runtime_writer_factory.validate_entry(path=receipt_path, role="runtime_receipt")
            if effect_journal is not None:
                runtime_writer_factory.validate_entry(path=effect_journal.path, role="effect_journal")
            return
        if owner_context is not None:
            return
        candidates: list[Path] = []
        if receipt_path is not None:
            candidates.append(Path(receipt_path))
        if effect_journal is not None and hasattr(effect_journal, "project_root"):
            candidates.append(Path(effect_journal.project_root))
        if selected_effect_path is not None:
            candidates.append(selected_effect_path)
        if not candidates:
            return
        candidates = [variant for path in candidates for variant in (path.absolute(), path.resolve())]
        read_manifest = _nexus_generated_bindings.read_manifest
        read_generation = _nexus_generated_bindings.read_generation
        receipt_mode = receipt_path is not None
        for candidate in candidates:
            root = candidate if candidate.is_dir() else candidate.parent
            while True:
                marker = root / ".nexus" / "writer-quiescence-hold.json"
                try:
                    held = marker.is_symlink() or marker.exists()
                except OSError:
                    held = True
                if held or read_manifest(root) is not None or (receipt_mode and read_generation(root) is not None):
                    raise ValueError("runtime_writer_factory_required")
                if root == root.parent:
                    break
                root = root.parent


    def build_execution_replan_request(
        *,
        task_id: str,
        planner_decision_id: str,
        current_execution_depth: str,
        verifier_stage: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Build a machine-readable execution replan request based on trusted verifier outcome evidence."""
        current_depth = str(current_execution_depth or "LIGHT")

        stage_dict = verifier_stage if isinstance(verifier_stage, Mapping) else {}
        invoked = bool(stage_dict.get("invoked", False)) if verifier_stage is not None else False
        status_raw = str(stage_dict.get("status", "")).upper()
        gate_passed = bool(stage_dict.get("gate_passed", False))
        task_identity_shared = bool(stage_dict.get("task_identity_shared", True))
        evidence_present = bool(stage_dict.get("evidence_present", False))
        evidence_refs = [str(ref) for ref in stage_dict.get("evidence_refs", []) or []]

        if verifier_stage is None or not invoked or status_raw in {"NOT_RUN", "NOT_REQUESTED", ""}:
            trigger = "verifier_not_observed"
            verifier_outcome_trusted = False
            replan_required = False
            depth_escalated = False
            manual_review_required = False
            requested_depth = current_depth
            verifier_status = status_raw or "NOT_RUN"
        elif not task_identity_shared:
            trigger = "verifier_identity_mismatch"
            verifier_outcome_trusted = False
            replan_required = False
            depth_escalated = False
            manual_review_required = False
            requested_depth = current_depth
            verifier_status = "FAILED"
        elif gate_passed and status_raw == "SUCCEEDED":
            trigger = "verifier_passed"
            verifier_outcome_trusted = True
            replan_required = False
            depth_escalated = False
            manual_review_required = False
            requested_depth = current_depth
            verifier_status = "SUCCEEDED"
        elif not evidence_present:
            trigger = "verifier_evidence_untrusted"
            verifier_outcome_trusted = False
            replan_required = False
            depth_escalated = False
            manual_review_required = False
            requested_depth = current_depth
            verifier_status = "FAILED"
        else:
            # Trusted failure case
            verifier_outcome_trusted = True
            replan_required = True
            verifier_status = "FAILED"
            requested_depth = next_execution_depth_after_failure(current_depth)
            if current_depth == EXECUTION_DEPTH_FULL:
                trigger = "verifier_failed_at_full_depth"
                depth_escalated = False
                manual_review_required = True
            else:
                trigger = "verifier_failed"
                depth_escalated = True
                manual_review_required = False

        payload_for_id = {
            "task_id": str(task_id),
            "source_planner_decision_id": str(planner_decision_id),
            "current_execution_depth": current_depth,
            "requested_execution_depth": requested_depth,
            "trigger": trigger,
            "verifier_status": verifier_status,
            "verifier_evidence_refs": sorted(evidence_refs),
        }
        canonical_str = json.dumps(payload_for_id, sort_keys=True, separators=(",", ":"))
        replan_request_id = "sha256:" + hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()

        return {
            "schema": "nexus.execution_replan_request.v1",
            "task_id": str(task_id),
            "source_planner_decision_id": str(planner_decision_id),
            "current_execution_depth": current_depth,
            "requested_execution_depth": requested_depth,
            "trigger": trigger,
            "verifier_outcome_trusted": verifier_outcome_trusted,
            "replan_required": replan_required,
            "depth_escalated": depth_escalated,
            "manual_review_required": manual_review_required,
            "verifier_status": verifier_status,
            "verifier_evidence_refs": evidence_refs,
            "replan_request_id": replan_request_id,
            "public_claim_allowed": False,
        }


    def execution_replan_request_authority_projection(
        value: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Project all authority-bearing fields of an execution_replan_request."""
        if not isinstance(value, Mapping):
            return {}

        v_stage = value.get("verifier_stage") if isinstance(value.get("verifier_stage"), Mapping) else {}
        v_refs = value.get("verifier_evidence_refs") or v_stage.get("evidence_refs") or []
        if isinstance(v_refs, (list, tuple)):
            sorted_refs = sorted(str(r) for r in v_refs)
        else:
            sorted_refs = []

        return {
            "schema": str(value.get("schema") or ""),
            "task_id": str(value.get("task_id") or ""),
            "source_planner_decision_id": str(value.get("source_planner_decision_id") or ""),
            "current_execution_depth": str(value.get("current_execution_depth") or ""),
            "requested_execution_depth": str(value.get("requested_execution_depth") or ""),
            "trigger": str(value.get("trigger") or ""),
            "verifier_outcome_trusted": bool(value.get("verifier_outcome_trusted")),
            "replan_required": bool(value.get("replan_required")),
            "depth_escalated": bool(value.get("depth_escalated")),
            "manual_review_required": bool(value.get("manual_review_required")),
            "verifier_status": str(value.get("verifier_status") or ""),
            "verifier_evidence_refs": sorted_refs,
            "replan_request_id": str(value.get("replan_request_id") or ""),
            "public_claim_allowed": bool(value.get("public_claim_allowed")),
        }


    # Provider-neutral registration metadata.  Commands stay configurable at the
    # edge; this registry records the supported adapter contract without claiming
    # that a provider binary was invoked.
    ONLINE_CLI_SPEC_REGISTRY: dict[str, dict[str, str]] = {
        "gemini": {
            "transport": "subprocess",
            "binary_env": "NEXUS_GEMINI_BIN",
            "command_env": "NEXUS_GEMINI_COMMAND",
            "binary_name": "gemini",
            "print_flag": "-p",
            "default_model": "gemini-3.6-flash",
        },
        "agy": {
            "transport": "subprocess",
            "binary_env": "NEXUS_AGY_BIN",
            "command_env": "NEXUS_AGY_COMMAND",
            "binary_name": "agy",
            "print_flag": "-p",
            "default_model": "gemini-3.6-flash-high",
        },
        "grok": {
            "transport": "subprocess",
            "binary_env": "NEXUS_GROK_BIN",
            "command_env": "NEXUS_GROK_COMMAND",
            "binary_name": "grok",
            "print_flag": "-p",
            "default_model": "grok-4.5",
        },
        "codex": {
            "transport": "subprocess",
            "binary_env": "NEXUS_CODEX_BIN",
            "command_env": "NEXUS_CODEX_COMMAND",
            "binary_name": "codex",
            "print_flag": "exec",
            "default_model": "gpt-5.6-luna",
        },
        "openai": {
            "transport": "subprocess",
            "binary_env": "NEXUS_OPENAI_BIN",
            "command_env": "NEXUS_OPENAI_COMMAND",
            "binary_name": "openai",
            "print_flag": "",
            "default_model": "gpt-5",
        },
        "opencode": {
            "transport": "subprocess",
            "binary_env": "NEXUS_OPENCODE_BIN",
            "command_env": "NEXUS_OPENCODE_COMMAND",
            "binary_name": "opencode",
            "print_flag": "",
            "default_model": "opencode/deepseek-v4-flash-free",
        },
        "cline": {
            "transport": "subprocess",
            "binary_env": "NEXUS_CLINE_BIN",
            "command_env": "NEXUS_CLINE_COMMAND",
            "binary_name": "cline",
            "print_flag": "",
            "default_model": "glm-5.2",
        },
        "mimo": {
            "transport": "subprocess",
            "binary_env": "NEXUS_MIMO_BIN",
            "command_env": "NEXUS_MIMO_COMMAND",
            "binary_name": "mimo",
            "print_flag": "",
            "default_model": "xiaomi/mimo-v2.5",
        },
        "ollama": {
            "transport": "subprocess",
            "binary_env": "NEXUS_OLLAMA_BIN",
            "command_env": "NEXUS_OLLAMA_COMMAND",
            "binary_name": "ollama",
            "print_flag": "",
            "default_model": "qwen2.5-s2t-advisor:3b",
        },
    }

    # These providers have no verified registered-CLI model-binding contract. An
    # admitted physical call must not silently fall back to a provider default.
    REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS: frozenset[str] = frozenset(
        {"agy", "grok", "openai"}
    )

    # Explicit provider contracts. These are not inferred from installed CLIs.
    REGISTERED_CLI_MODEL_BINDING_FLAGS: dict[str, tuple[str, str]] = {
        "codex": ("exec", "-m"),
        "opencode": ("run", "--model"),
        "cline": ("", "--model"),
        "mimo": ("run", "--model"),
        "ollama": ("run", ""),
    }

    # Local-only providers may appear on Gateway defaults (auto-detect) but are not
    # Online CLI registry members. They must not be promoted into Online route.provider
    # merely because they are locally available.
    LOCAL_ONLY_PROVIDERS: frozenset[str] = frozenset({"ollama"})

    TRANSPORT_STRUCTURED_CALLABLE = "structured_callable"
    TRANSPORT_REGISTERED_CLI = "registered_cli"
    TRANSPORT_GATEWAY_COMPATIBILITY = "gateway_compatibility"
    TRANSPORT_UNRESOLVED = "unresolved"

    SELECTION_EXPLICIT_REQUEST = "explicit_request"
    SELECTION_INJECTED_TRANSPORT = "injected_transport"
    SELECTION_ENVIRONMENT_DEFAULT = "environment_default"
    SELECTION_COMPATIBILITY_DEFAULT = "compatibility_default"
    SELECTION_PLANNER = "planner"
    GATEWAY_INVOCATION_AUTHORITY_SCHEMA = "nexus.gateway_invocation_authority.v1"


    def _required_identity(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name}_missing")
        return value


    def _optional_identity(value: Any) -> str:
        text = value if isinstance(value, str) else str(value or "")
        return text if text.strip() else ""


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


    def _validate_online_admission_record(
        *,
        plan_payload: Mapping[str, Any],
        admission_payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Validate the one Online record and its hashes from the canonical T2B payload."""
        if not isinstance(admission_payload, Mapping):
            raise ValueError("workforce_admission_missing")
        if admission_payload.get("overall_decision") != "ALLOW":
            raise ValueError("workforce_admission_overall_decision_not_allow")

        snapshot = plan_payload.get("signal_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ValueError("workforce_admission_signal_snapshot_missing")
        try:
            demands = _parse_demands(snapshot.get("workforce_demands"))
        except Exception as exc:
            # The runtime algorithm is fail-closed; expose a stable reason rather
            # than an implementation-specific parser detail.
            raise ValueError("workforce_admission_demands_malformed") from exc

        records = admission_payload.get("records")
        if not isinstance(records, list) or len(records) != len(demands.demands):
            raise ValueError("workforce_admission_records_malformed")
        policy_identity = admission_payload.get("policy_identity")
        if not isinstance(policy_identity, Mapping):
            raise ValueError("workforce_admission_policy_identity_missing")
        policy_hash = _required_identity(
            policy_identity.get("policy_hash"), "workforce_admission_policy_hash"
        )

        recomputed_hashes: list[str] = []
        online_record: Mapping[str, Any] | None = None
        online_demand_count = 0
        for demand, record in zip(demands.demands, records):
            if not isinstance(record, Mapping):
                raise ValueError("workforce_admission_record_malformed")
            if record.get("demand") != _as_json_value(demand.to_dict()):
                raise ValueError("workforce_admission_record_demand_mismatch")
            request = record.get("request")
            decision = record.get("decision")
            if not isinstance(request, Mapping) or not isinstance(decision, Mapping):
                raise ValueError("workforce_admission_record_malformed")
            expected_binding_hash = _sha256_json(
                _binding_payload(demand, request, decision, policy_identity)
            )
            if record.get("binding_hash") != expected_binding_hash:
                raise ValueError("workforce_admission_record_binding_hash_mismatch")
            recomputed_hashes.append(expected_binding_hash)
            if demand.execution_channel == "online":
                online_demand_count += 1
                if online_record is not None:
                    raise ValueError("workforce_admission_online_record_ambiguous")
                online_record = record

        if online_demand_count != 1 or online_record is None:
            raise ValueError("workforce_admission_online_record_count_invalid")
        if _aggregate_hash(
            policy_hash,
            [
                RuntimeWorkforceAdmissionRecord(
                    schema="",
                    demand={},
                    request={},
                    decision={},
                    binding_hash=value,
                )
                for value in recomputed_hashes
            ],
        ) != admission_payload.get("aggregate_binding_hash"):
            raise ValueError("workforce_admission_aggregate_binding_hash_mismatch")

        decision = online_record.get("decision")
        if not isinstance(decision, Mapping) or decision.get("decision") != "ALLOW":
            raise ValueError("workforce_admission_online_record_decision_not_allow")
        decision_policy_hash = _required_identity(
            decision.get("policy_hash"), "workforce_admission_record_policy_hash"
        )
        if decision_policy_hash != policy_hash:
            raise ValueError("workforce_admission_record_policy_hash_mismatch")
        resolved_worker_id = _required_identity(
            decision.get("resolved_worker_id"), "workforce_admission_resolved_worker_id"
        )
        resolved_provider = _required_identity(
            decision.get("resolved_provider"), "workforce_admission_resolved_provider"
        )
        resolved_model = _required_identity(
            decision.get("resolved_model"), "workforce_admission_resolved_model"
        )
        aggregate_binding_hash = _required_identity(
            admission_payload.get("aggregate_binding_hash"),
            "workforce_admission_aggregate_binding_hash",
        )
        record_binding_hash = _required_identity(
            online_record.get("binding_hash"), "workforce_admission_record_binding_hash"
        )
        return {
            "demand_id": str(online_record.get("demand", {}).get("demand_id") or ""),
            "resolved_worker_id": resolved_worker_id,
            "resolved_provider": resolved_provider,
            "resolved_model": resolved_model,
            "policy_hash": policy_hash,
            "binding_hash": record_binding_hash,
            "aggregate_binding_hash": aggregate_binding_hash,
        }


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


    def _build_gateway_invocation_authority(
        *,
        request: UnifiedRuntimeRequest,
        plan_payload: Mapping[str, Any],
        admission_payload: Mapping[str, Any] | None,
        invoker: Any,
        effective_decision: str,
        effective_reason: str = "",
    ) -> dict[str, Any]:
        authority: dict[str, Any] = {
            "schema": GATEWAY_INVOCATION_AUTHORITY_SCHEMA,
            "status": "BLOCKED",
            "gate_passed": False,
            "failure_reason": "",
            "admission_overall_decision": str(
                admission_payload.get("overall_decision") if isinstance(admission_payload, Mapping) else ""
            ),
            "admission_record_decision": "",
            "resolved_worker_id": "",
            "resolved_provider": "",
            "resolved_model": "",
            "policy_hash": "",
            "binding_hash": "",
            "aggregate_binding_hash": "",
            "route_provider": "",
            "transport_provider": "",
            "online_model_name": "",
            "invoker_provider": "",
        }
        try:
            admitted = _validate_online_admission_record(
                plan_payload=plan_payload,
                admission_payload=admission_payload,
            )
            authority.update(admitted)
            authority["admission_record_decision"] = "ALLOW"
            if effective_decision != "ALLOW":
                raise ValueError(effective_reason or "workforce_admission_effective_decision_not_allow")

            route = request.route
            supplied_route_provider = route.get("provider")
            route_provider = (
                _required_identity(supplied_route_provider, "online_route_provider")
                if supplied_route_provider is not None
                else admitted["resolved_provider"]
            )
            transport_binding = route.get("online_transport_binding")
            if transport_binding is None:
                transport_provider = admitted["resolved_provider"]
            elif isinstance(transport_binding, Mapping):
                transport_provider = _required_identity(
                    transport_binding.get("provider"), "online_transport_provider"
                )
            else:
                raise ValueError("online_transport_binding_missing")
            online_model_name = (
                _required_identity(request.online_model_name, "online_model_name")
                if request.online_model_name is not None
                else admitted["resolved_model"]
            )
            route_invoker_provider = route.get("online_invoker_provider")
            if route_invoker_provider is not None:
                route_invoker_provider = _required_identity(
                    route_invoker_provider, "online_invoker_provider"
                )
            workforce_dispatcher = getattr(invoker, "workforce_dispatcher", False) is True
            callable_provider = _callable_provider_identity(invoker)
            if workforce_dispatcher:
                if callable_provider:
                    raise ValueError("workforce_dispatcher_provider_must_be_deferred")
                callable_provider = admitted["resolved_provider"]
            if route_invoker_provider and callable_provider and route_invoker_provider != callable_provider:
                raise ValueError("online_invoker_provider_ambiguous")
            invoker_provider = route_invoker_provider or callable_provider
            if not invoker_provider:
                raise ValueError("online_invoker_provider_missing")

            authority.update(
                {
                    "route_provider": route_provider,
                    "transport_provider": transport_provider,
                    "online_model_name": online_model_name,
                    "invoker_provider": invoker_provider,
                }
            )
            if route_provider != admitted["resolved_provider"]:
                raise ValueError("online_route_provider_mismatch")
            if transport_provider != admitted["resolved_provider"]:
                raise ValueError("online_transport_provider_mismatch")
            if invoker_provider != admitted["resolved_provider"]:
                raise ValueError("online_invoker_provider_mismatch")
            if online_model_name != admitted["resolved_model"]:
                raise ValueError("online_model_name_mismatch")
        except ValueError as exc:
            authority["failure_reason"] = str(exc)
            return authority

        authority["status"] = "ALLOW"
        authority["gate_passed"] = True
        return authority


    def _local_admission_record_decision(admission_payload: Any) -> str:
        if not isinstance(admission_payload, Mapping):
            return ""
        for record in admission_payload.get("records", []) or []:
            if not isinstance(record, Mapping):
                continue
            demand = record.get("demand")
            decision = record.get("decision")
            if (
                isinstance(demand, Mapping)
                and demand.get("execution_channel") == "local"
                and isinstance(decision, Mapping)
            ):
                return str(decision.get("decision") or "")
        return ""


    def _validate_local_admission_record(
        *,
        plan_payload: Mapping[str, Any],
        admission_payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Validate the exact local T2B record before Local service execution."""
        if not isinstance(admission_payload, Mapping):
            raise ValueError("workforce_admission_missing")
        if admission_payload.get("schema") != RUNTIME_WORKFORCE_ADMISSION_SCHEMA:
            raise ValueError("workforce_admission_schema_invalid")

        snapshot = plan_payload.get("signal_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ValueError("workforce_admission_signal_snapshot_missing")
        try:
            demands = _parse_demands(snapshot.get("workforce_demands"))
        except Exception as exc:
            raise ValueError("workforce_admission_demands_malformed") from exc

        records = admission_payload.get("records")
        if not isinstance(records, list) or len(records) != len(demands.demands):
            raise ValueError("workforce_admission_records_malformed")
        policy_identity = admission_payload.get("policy_identity")
        if not isinstance(policy_identity, Mapping):
            raise ValueError("workforce_admission_policy_identity_missing")
        policy_hash = _required_identity(
            policy_identity.get("policy_hash"), "workforce_admission_policy_hash"
        )

        recomputed_hashes: list[str] = []
        local_record: Mapping[str, Any] | None = None
        local_demand_count = 0
        for demand, record in zip(demands.demands, records):
            if not isinstance(record, Mapping):
                raise ValueError("workforce_admission_record_malformed")
            if record.get("schema") != RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA:
                raise ValueError("workforce_admission_record_schema_invalid")
            if record.get("demand") != _as_json_value(demand.to_dict()):
                raise ValueError("workforce_admission_record_demand_mismatch")
            request = record.get("request")
            decision = record.get("decision")
            if not isinstance(request, Mapping) or not isinstance(decision, Mapping):
                raise ValueError("workforce_admission_record_malformed")
            if decision.get("schema") != WORKFORCE_ADMISSION_DECISION_SCHEMA:
                raise ValueError("workforce_admission_decision_schema_invalid")
            expected_binding_hash = _sha256_json(
                _binding_payload(demand, request, decision, policy_identity)
            )
            if record.get("binding_hash") != expected_binding_hash:
                raise ValueError("workforce_admission_record_binding_hash_mismatch")
            recomputed_hashes.append(expected_binding_hash)
            if demand.execution_channel == "local":
                local_demand_count += 1
                if local_record is not None:
                    raise ValueError("workforce_admission_local_record_ambiguous")
                local_record = record

        if local_demand_count != 1 or local_record is None:
            raise ValueError("workforce_admission_local_record_count_invalid")
        if _aggregate_hash(
            policy_hash,
            [
                RuntimeWorkforceAdmissionRecord(
                    schema=RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA,
                    demand={},
                    request={},
                    decision={},
                    binding_hash=value,
                )
                for value in recomputed_hashes
            ],
        ) != admission_payload.get("aggregate_binding_hash"):
            raise ValueError("workforce_admission_aggregate_binding_hash_mismatch")

        decision = local_record.get("decision")
        if not isinstance(decision, Mapping) or decision.get("decision") != "ALLOW":
            raise ValueError("workforce_admission_local_record_decision_not_allow")
        decision_policy_hash = _required_identity(
            decision.get("policy_hash"), "workforce_admission_record_policy_hash"
        )
        if decision_policy_hash != policy_hash:
            raise ValueError("workforce_admission_record_policy_hash_mismatch")
        resolved_worker_id = _required_identity(
            decision.get("resolved_worker_id"), "workforce_admission_resolved_worker_id"
        )
        resolved_provider = _required_identity(
            decision.get("resolved_provider"), "workforce_admission_resolved_provider"
        )
        resolved_model = _required_identity(
            decision.get("resolved_model"), "workforce_admission_resolved_model"
        )
        aggregate_binding_hash = _required_identity(
            admission_payload.get("aggregate_binding_hash"),
            "workforce_admission_aggregate_binding_hash",
        )
        record_binding_hash = _required_identity(
            local_record.get("binding_hash"), "workforce_admission_record_binding_hash"
        )
        demand_payload = local_record.get("demand")
        if not isinstance(demand_payload, Mapping):
            raise ValueError("workforce_admission_local_demand_missing")
        return {
            "demand_id": str(demand_payload.get("demand_id") or ""),
            "requested_role": _required_identity(
                demand_payload.get("requested_role"),
                "workforce_admission_local_requested_role",
            ),
            "mutation_intent": demand_payload.get("mutation_intent") is True,
            "resolved_worker_id": resolved_worker_id,
            "resolved_provider": resolved_provider,
            "resolved_model": resolved_model,
            "policy_hash": policy_hash,
            "binding_hash": record_binding_hash,
            "aggregate_binding_hash": aggregate_binding_hash,
        }


    def _build_local_model_invocation_authority(
        *,
        plan_payload: Mapping[str, Any],
        admission_payload: Mapping[str, Any] | None,
        effective_decision: str,
        effective_reason: str = "",
    ) -> dict[str, Any]:
        authority: dict[str, Any] = {
            "schema": LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA,
            "status": "BLOCKED",
            "gate_passed": False,
            "failure_reason": "",
            "demand_id": "",
            "requested_role": "",
            "mutation_intent": False,
            "resolved_worker_id": "",
            "resolved_provider": "",
            "resolved_model": "",
            "policy_hash": "",
            "binding_hash": "",
            "aggregate_binding_hash": "",
            "admission_record_decision": _local_admission_record_decision(admission_payload),
        }
        try:
            admitted = _validate_local_admission_record(
                plan_payload=plan_payload,
                admission_payload=admission_payload,
            )
            authority.update(admitted)
            if effective_decision != "ALLOW":
                raise ValueError(effective_reason or "workforce_admission_effective_decision_not_allow")
        except ValueError as exc:
            authority["failure_reason"] = str(exc)
            return authority

        authority["status"] = "ALLOW"
        authority["gate_passed"] = True
        return authority


    def _local_authority_failure_stage(
        *,
        task_id: str,
        authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        reason = str(authority.get("failure_reason") or "local_model_invocation_authority_blocked")
        authority_payload = dict(authority)
        response = {
            "task_id": task_id,
            "invoked": False,
            "local_model_invoked": False,
            "output_delivered": False,
            "gate_passed": False,
            "local_model_call_count": 0,
            "model_call_count": 0,
            "provider_call_count": 0,
            "error": reason,
            "evidence_refs": [f"local:{task_id}:local_model_invocation_authority:{reason}"],
            "local_model_invocation_authority": authority_payload,
        }
        return _stage(
            "local",
            status="FAILED",
            invoked=False,
            evidence_present=True,
            gate_passed=False,
            evidence_refs=list(response["evidence_refs"]),
            reason=reason,
            task_id=task_id,
            response_task_id=task_id,
            task_identity_shared=True,
            local_call_count=0,
            local_model_call_count=0,
            model_call_count=0,
            provider_call_count=0,
            context_trace={"local_model_invocation_authority": authority_payload},
            local_model_invocation_authority=authority_payload,
            response=response,
        )


    def _online_authority_failure_stage(
        *,
        task_id: str,
        authority: Mapping[str, Any],
    ) -> dict[str, Any]:
        reason = str(authority.get("failure_reason") or "gateway_invocation_authority_blocked")
        provider = str(authority.get("invoker_provider") or authority.get("resolved_provider") or "")
        response = {
            "provider": provider,
            "task_id": task_id,
            "invoked": False,
            "output_delivered": False,
            "gate_passed": False,
            "provider_call_count": 0,
            "response": "",
            "raw_response": "",
            "usage": {},
            "error": reason,
            "evidence_refs": [f"online:{task_id}:gateway_invocation_authority:{reason}"],
            "gateway_invocation_authority": dict(authority),
        }
        return _stage(
            "online",
            status="FAILED",
            invoked=False,
            evidence_present=True,
            gate_passed=False,
            evidence_refs=list(response["evidence_refs"]),
            reason=reason,
            task_id=task_id,
            response_task_id=task_id,
            task_identity_shared=True,
            provider_call_count=0,
            context_trace={"gateway_invocation_authority": dict(authority)},
            response=response,
        )


    def _safe_task_id(value: str) -> str:
        raw = str(value or "").strip()
        if not raw or any(char in raw for char in "/\\\x00"):
            raise ValueError("task_id_invalid")
        return raw


    def _hash_json(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


    def _validate_workforce_route(route: Mapping[str, Any]) -> None:
        """Validate runtime workforce control flags before Planner or policy load."""
        for field_name in ("workforce_admission_enabled", "workforce_rebind_authorized"):
            if field_name in route and type(route[field_name]) is not bool:
                raise ValueError(f"{field_name}_must_be_boolean")
        if route.get("workforce_rebind_authorized") is True:
            reason = route.get("workforce_rebind_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("workforce_rebind_reason_required")


    def _workforce_admission_required(
        request: UnifiedRuntimeRequest,
        *,
        physical_local: bool = False,
        physical_online: bool = False,
    ) -> bool:
        """Return whether fresh Workforce Admission is mandatory for this run.

        Caller route flags may request admission, but they may never disable it for
        a physical Local model or Online provider edge. Deterministic/injected
        non-physical test transports remain outside this mandatory physical gate.
        """
        return (
            request.canonical_planning_bundle is not None
            or request.route.get("workforce_admission_enabled") is True
            or physical_local
            or physical_online
        )


    def _physical_local_service(service: Any) -> bool:
        if service is None:
            return False
        marker = getattr(service, "physical_model_transport", None)
        if type(marker) is bool:
            return marker
        service_type = type(service)
        return (
            service_type.__module__ == "nexus.services.local_assist_service"
            and service_type.__name__ == "LocalAssistService"
        )


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


    def _build_workforce_admission_lineage(
        *,
        route: Mapping[str, Any],
        attempt_number: int,
        current_aggregate_binding_hash: Any,
        current_planner_decision_id: str,
        replan_authorization: ExecutionReplanAuthorization | None,
        parent_receipt: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the additive, JSON-safe binding-continuity proof for admission."""
        prior_admission = (
            parent_receipt.get("workforce_admission")
            if isinstance(parent_receipt, Mapping)
            else None
        )
        source_aggregate = ""
        if isinstance(prior_admission, Mapping):
            source_aggregate = str(prior_admission.get("aggregate_binding_hash") or "")
        current_aggregate = str(current_aggregate_binding_hash or "")
        binding_changed = bool(source_aggregate and source_aggregate != current_aggregate)
        rebind_authorized = route.get("workforce_rebind_authorized") is True
        rebind_reason = str(route.get("workforce_rebind_reason") or "")

        if attempt_number == 1:
            status = "FIRST_ADMISSION"
        elif not source_aggregate:
            status = "ENABLED_ON_REPLAN"
        elif binding_changed and rebind_authorized:
            status = "REBOUND"
        elif binding_changed:
            status = "BLOCKED_REBIND"
        else:
            status = "UNCHANGED"

        return {
            "schema": "nexus.runtime_workforce_admission_lineage.v1",
            "attempt_number": int(attempt_number),
            "source_aggregate_binding_hash": source_aggregate,
            "current_aggregate_binding_hash": current_aggregate,
            "source_receipt_hash": (
                replan_authorization.source_receipt_hash if replan_authorization is not None else ""
            ),
            "source_run_anchor_hash": (
                replan_authorization.source_run_anchor_hash if replan_authorization is not None else ""
            ),
            "source_replan_request_id": (
                replan_authorization.source_replan_request_id if replan_authorization is not None else ""
            ),
            "current_planner_decision_id": str(current_planner_decision_id),
            "binding_changed": binding_changed,
            "rebind_authorized": rebind_authorized,
            "rebind_reason": rebind_reason,
            "status": status,
        }


    def _local_action_from_request(local_request: Any, local_stage: Mapping[str, Any]) -> str:
        """Resolve Local action for executor-identity attribution (advisor vs executor)."""
        action = ""
        if isinstance(local_request, Mapping):
            action = str(local_request.get("action") or "")
        elif local_request is not None:
            action = str(getattr(local_request, "action", "") or "")
        if not action:
            response = local_stage.get("response") if isinstance(local_stage, Mapping) else None
            if isinstance(response, Mapping):
                action = str(response.get("action") or "")
        return action.strip().lower()


    def _local_executor_invoked_proven(
        *,
        action: str,
        physical_callable: str,
        local_stage: Mapping[str, Any],
    ) -> bool:
        """True only when action + stage + physical callable + executor_invoked all prove Executor ran.

        Requires ALL of:
          - action in {candidate, verified-subtask}
          - Local stage invoked
          - physical_callable == LocalModelExecutor.run
          - executor_invoked == true
        """
        if action not in {"candidate", "verified-subtask"}:
            return False
        if not bool(local_stage.get("invoked", False)):
            return False
        callable_name = str(physical_callable or "").strip()
        if callable_name != "LocalModelExecutor.run":
            return False
        response = local_stage.get("response") if isinstance(local_stage, Mapping) else None
        if not isinstance(response, Mapping):
            return False
        return response.get("executor_invoked") is True


    def _repair_loop_result_from_local_stage(
        *,
        task_id: str,
        local_stage: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Materialize repair-loop truth from the Local verified-subtask receipt.

        A model call or isolated candidate alone is insufficient.  Success needs
        candidate/apply hash agreement, verifier PASS, and a complete disk receipt
        bound to the same task.
        """
        response = (
            local_stage.get("response")
            if isinstance(local_stage.get("response"), Mapping)
            else {}
        )
        candidate = (
            response.get("candidate_summary")
            if isinstance(response.get("candidate_summary"), Mapping)
            else {}
        )
        verifier = (
            response.get("verifier_summary")
            if isinstance(response.get("verifier_summary"), Mapping)
            else {}
        )
        evidence_refs = [str(item) for item in response.get("evidence_refs", []) or []]
        candidate_hash = str(candidate.get("selected_candidate_hash") or "").strip()
        hash_matched = candidate.get("selected_candidate_hash_matches_applied") is True
        verifier_passed = (
            verifier.get("verifier_reached") is True
            and str(verifier.get("verifier_status") or "").strip().lower()
            in {"pass", "passed", "ok"}
            and int(verifier.get("exit_code") or 0) == 0
        )
        receipt_path = Path(str(response.get("receipt_path") or ""))
        disk_receipt: dict[str, Any] = {}
        if receipt_path.is_file() and receipt_path.stat().st_size <= 1_000_000:
            try:
                loaded = json.loads(receipt_path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    disk_receipt = dict(loaded)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                disk_receipt = {}
        disk_hashes = [str(item) for item in disk_receipt.get("candidate_hashes", []) or []]
        receipt_complete = bool(
            disk_receipt.get("task_id") == task_id
            and disk_receipt.get("terminal_status") == "SUCCEEDED"
            and disk_receipt.get("receipt_complete") is True
            and str(disk_receipt.get("verifier_result") or "").lower()
            in {"pass", "passed", "ok"}
            and candidate_hash
            and candidate_hash in disk_hashes
        )
        executor_proven = _local_executor_invoked_proven(
            action=str(response.get("action") or "").strip().lower(),
            physical_callable=str(response.get("physical_callable") or ""),
            local_stage=local_stage,
        )
        success = bool(
            executor_proven
            and response.get("output_delivered") is True
            and candidate.get("isolation_status") == "isolated"
            and candidate_hash
            and hash_matched
            and verifier_passed
            and receipt_complete
            and evidence_refs
        )
        blockers: list[str] = []
        if not executor_proven:
            blockers.append("local_model_executor_not_proven")
        if not candidate_hash:
            blockers.append("missing_candidate_hash")
        if not hash_matched:
            blockers.append("candidate_hash_not_applied")
        if not verifier_passed:
            blockers.append("verifier_not_passed")
        if not receipt_complete:
            blockers.append("local_receipt_incomplete")
        if not evidence_refs:
            blockers.append("missing_local_evidence_refs")
        outcome = {
            "action": "verified_local_repair",
            "semantic_status": "SUCCEEDED" if success else "BLOCKED",
            "evidence_refs": evidence_refs,
            "candidate_hash": candidate_hash,
            "hash_matched": hash_matched,
            "verifier_passed": verifier_passed,
            "settlement_decision": "receipt_complete" if receipt_complete else "incomplete",
            "receipt_path": str(receipt_path) if str(receipt_path) else "",
            "blockers": blockers,
            "physical_callable": "LocalModelExecutor.run",
        }
        return {
            "task_id": task_id,
            "invoked": bool(local_stage.get("invoked")),
            "skipped": False,
            "status": "SUCCEEDED" if success else "BLOCKED",
            "gate_passed": success,
            "outcome_contributed": success,
            "evidence_refs": evidence_refs,
            "physical_callable": "LocalModelExecutor.run",
            "telemetry": dict(local_stage.get("telemetry") or {}),
            "stub": False,
            "response": {"status": "SUCCEEDED" if success else "BLOCKED", "outcome": outcome},
        }


    def _formal_local_runtime_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "").strip()
        candidate = payload.get("candidate_summary") if isinstance(payload.get("candidate_summary"), Mapping) else {}
        verifier = payload.get("verifier_summary") if isinstance(payload.get("verifier_summary"), Mapping) else {}
        claim_boundary = payload.get("claim_boundary") if isinstance(payload.get("claim_boundary"), Mapping) else {}
        receipt_path = str(payload.get("receipt_path") or "")
        physical_callable = str(payload.get("physical_callable") or "")
        executor_invoked = payload.get("executor_invoked") is True
        local_model_invoked = bool(payload.get("local_model_invoked", payload.get("invoked", False)))
        output_delivered = payload.get("output_delivered") is True
        provider_call_count = int(payload.get("provider_call_count") or 0)
        model_call_count = int(payload.get("model_call_count") or (provider_call_count if local_model_invoked else 0))
        candidate_isolated = (
            str(candidate.get("isolation_status") or "") == "isolated"
            and bool(candidate.get("selected_candidate_hash"))
            and candidate.get("selected_candidate_hash_matches_applied") is True
        )
        verifier_status = str(verifier.get("verifier_status") or "").strip().lower()
        verifier_passed = (
            action != "verified-subtask"
            or (
                verifier.get("verifier_reached") is True
                and verifier_status in {"pass", "passed", "ok"}
                and int(verifier.get("exit_code") or 0) == 0
            )
        )
        blockers: list[str] = []
        fallback_reason = str(payload.get("fallback_reason") or "").strip()
        if fallback_reason and fallback_reason not in {"candidate_not_delivered", "provider_not_invoked"}:
            blockers.append(fallback_reason)
        if payload.get("schema") != "nexus.local_assist.response.v1":
            blockers.append("local_assist_response_schema_missing")
        if action not in {"candidate", "verified-subtask"}:
            blockers.append("local_action_not_executor_bound")
        if physical_callable != "LocalModelExecutor.run":
            blockers.append("local_physical_callable_not_executor")
        if not executor_invoked:
            blockers.append("local_executor_not_invoked")
        if not local_model_invoked:
            blockers.append("local_model_not_invoked")
        if not output_delivered:
            blockers.append("local_output_not_delivered")
        if not candidate_isolated:
            blockers.append("candidate_not_isolated")
        if not verifier_passed:
            blockers.append("verifier_not_passed")
        if not receipt_path:
            blockers.append("local_receipt_path_missing")
        if claim_boundary and claim_boundary.get("local_model_executor_invoked") is not True:
            blockers.append("claim_boundary_executor_not_invoked")

        gate_passed = not blockers
        return {
            "schema": "nexus.formal_local_runtime_lineage.v1",
            "entrypoint": "UnifiedRuntime._run_local",
            "service": "LocalAssistService.handle",
            "executor": "LocalModelExecutor.run",
            "status": "ALLOW" if gate_passed else "BLOCKED",
            "gate_passed": gate_passed,
            "failure_reason": blockers[0] if blockers else "",
            "blockers": blockers,
            "action": action,
            "physical_callable": physical_callable,
            "executor_invoked": executor_invoked,
            "local_model_invoked": local_model_invoked,
            "output_delivered": output_delivered,
            "provider_call_count": provider_call_count,
            "model_call_count": model_call_count,
            "candidate_isolation_status": str(candidate.get("isolation_status") or ""),
            "selected_candidate_hash": str(candidate.get("selected_candidate_hash") or ""),
            "verifier_reached": bool(verifier.get("verifier_reached")),
            "verifier_status": verifier_status,
            "receipt_path": receipt_path,
        }


    def _formal_local_advisor_lineage(payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a non-mutating Local advisor without pretending it ran an executor."""
        action = str(payload.get("action") or "").strip()
        claim_boundary = (
            payload.get("claim_boundary")
            if isinstance(payload.get("claim_boundary"), Mapping)
            else {}
        )
        receipt_path = str(payload.get("receipt_path") or "")
        physical_callable = str(payload.get("physical_callable") or "")
        executor_invoked = payload.get("executor_invoked") is True
        local_model_invoked = bool(payload.get("local_model_invoked", payload.get("invoked", False)))
        output_delivered = payload.get("output_delivered") is True
        provider_call_count = int(payload.get("provider_call_count") or 0)
        model_call_count = int(
            payload.get("model_call_count") or (provider_call_count if local_model_invoked else 0)
        )
        blockers: list[str] = []
        fallback_reason = str(payload.get("fallback_reason") or "").strip()
        if fallback_reason and fallback_reason not in {"candidate_not_delivered", "provider_not_invoked"}:
            blockers.append(fallback_reason)
        if payload.get("schema") != "nexus.local_assist.response.v1":
            blockers.append("local_assist_response_schema_missing")
        if action != "advisor":
            blockers.append("local_action_not_advisor_bound")
        if physical_callable != "LocalModelProvider.generate":
            blockers.append("local_physical_callable_not_advisor")
        if executor_invoked:
            blockers.append("local_advisor_executor_forbidden")
        if not local_model_invoked:
            blockers.append("local_model_not_invoked")
        if not output_delivered:
            blockers.append("local_output_not_delivered")
        if not receipt_path:
            blockers.append("local_receipt_path_missing")
        if claim_boundary.get("local_model_executor_invoked") is True:
            blockers.append("local_advisor_claimed_executor")

        gate_passed = not blockers
        return {
            "schema": "nexus.formal_local_runtime_lineage.v1",
            "entrypoint": "UnifiedRuntime._run_local",
            "service": "LocalAssistService.handle",
            "executor": "",
            "status": "ALLOW" if gate_passed else "BLOCKED",
            "gate_passed": gate_passed,
            "failure_reason": blockers[0] if blockers else "",
            "blockers": blockers,
            "action": action,
            "physical_callable": physical_callable,
            "executor_invoked": executor_invoked,
            "local_model_invoked": local_model_invoked,
            "output_delivered": output_delivered,
            "provider_call_count": provider_call_count,
            "model_call_count": model_call_count,
            "candidate_isolation_status": "not_applicable",
            "selected_candidate_hash": "",
            "verifier_reached": False,
            "verifier_status": "not_applicable",
            "receipt_path": receipt_path,
        }


    def _mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            converted = to_dict()
            if isinstance(converted, Mapping):
                return dict(converted)
        return {"status": "FAILED", "error": "stage_result_not_mapping"}


    def _capability_evidence_summary(results: Mapping[str, Any]) -> dict[str, Any]:
        """Keep Online receipt awareness after Local context compression."""
        summary: dict[str, Any] = {}
        for name, value in results.items():
            data = value if isinstance(value, Mapping) else {}
            summary[str(name)] = {
                "status": str(data.get("status", "")),
                "task_id": str(data.get("task_id", "")),
                "evidence_refs": [str(ref) for ref in data.get("evidence_refs", []) or []][:6],
            }
        return summary


    def _stage(
        name: str,
        *,
        status: str,
        invoked: bool = False,
        evidence_present: bool = False,
        gate_passed: bool = False,
        outcome_contributed: bool = False,
        evidence_refs: list[str] | None = None,
        reason: str = "",
        **fields: Any,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "status": status,
            "invoked": invoked,
            "evidence_present": evidence_present,
            "gate_passed": gate_passed,
            "outcome_contributed": outcome_contributed,
            "evidence_refs": list(evidence_refs or []),
            "reason": reason,
            **fields,
        }


    def _capability_stage(
        name: str,
        task_id: str,
        result: Any,
        *,
        delegated_to: str = "Local",
    ) -> dict[str, Any]:
        """Normalize an explicit capability executor result fail-closed."""
        data = _mapping(result)
        response_task_id = str(data.get("task_id", "") or "")
        task_identity_shared = not response_task_id or response_task_id == task_id
        skipped = bool(data.get("skipped", False)) or str(data.get("status", "")).upper() == "SKIPPED"
        skip_reason = str(data.get("skip_reason") or "")
        invoked = bool(data.get("invoked", False)) and not skipped
        gate_passed = bool(
            data.get(
                "gate_passed",
                str(data.get("status", "")).lower() in {"ok", "pass", "passed", "succeeded", "success"},
            )
        )
        evidence_refs = [str(ref) for ref in data.get("evidence_refs", []) or []]
        evidence_present = bool(evidence_refs or data.get("evidence"))
        if skipped and task_identity_shared and evidence_present:
            status = "SKIPPED"
            # Explicit skip is coverage-success for FCM; not a stage hard-fail.
            gate_passed = True if data.get("gate_passed", True) else gate_passed
        elif task_identity_shared and invoked and evidence_present and gate_passed:
            status = "SUCCEEDED"
        else:
            status = "FAILED"
        return _stage(
            f"capability:{name}",
            status=status,
            invoked=invoked,
            evidence_present=evidence_present,
            gate_passed=task_identity_shared and gate_passed,
            outcome_contributed=bool(data.get("outcome_contributed", False)),
            evidence_refs=evidence_refs,
            reason=(
                "capability_task_id_mismatch"
                if not task_identity_shared
                else (skip_reason if skipped else "")
            ),
            task_id=task_id,
            response_task_id=response_task_id,
            task_identity_shared=task_identity_shared,
            delegated_to=str(data.get("delegated_to", delegated_to) or delegated_to),
            physical_callable=str(data.get("physical_callable") or ""),
            telemetry=dict(data.get("telemetry") or {}) if isinstance(data.get("telemetry"), Mapping) else {},
            response=data,
            skipped=skipped,
            skip_reason=skip_reason if skipped else "",
        )


    @dataclass(frozen=True)
    class UnifiedRuntimeRequest:
        """Inputs shared by every provider route for one task."""

        task_id: str
        workspace_revision: str
        task_statement: str
        task_type: str
        route: Mapping[str, Any]
        online_enabled: bool = True
        local_enabled: bool = False
        online_prompt: str = ""
        online_payload: str = ""
        online_phase: str = "R"
        online_model_name: str | None = None
        online_output_schema: Mapping[str, Any] | None = None
        pillars: Mapping[str, Any] = field(default_factory=dict)
        codeintel: Mapping[str, Any] = field(default_factory=dict)
        phase_trace: Mapping[str, Any] = field(default_factory=dict)
        budget: Mapping[str, Any] = field(default_factory=dict)
        skills: tuple[Mapping[str, Any], ...] = ()
        local_request: Any = None
        evidence_refs: tuple[str, ...] = ()
        canonical_context: Mapping[str, Any] | None = None
        canonical_planning_bundle: CanonicalPlanningBundle | None = None
        schema: str = REQUEST_SCHEMA

        def validate(self) -> None:
            if self.schema != REQUEST_SCHEMA:
                raise ValueError("unsupported_request_schema")
            _safe_task_id(self.task_id)
            if not str(self.workspace_revision).strip():
                raise ValueError("missing_workspace_revision")
            if not str(self.task_statement).strip():
                raise ValueError("missing_task_statement")
            if not str(self.task_type).strip():
                raise ValueError("missing_task_type")
            if not isinstance(self.route, Mapping):
                raise ValueError("route_must_be_object")
            if not self.online_enabled and not self.local_enabled:
                raise ValueError("at_least_one_runtime_route_required")
            if self.local_enabled and self.local_request is None:
                raise ValueError("local_request_required")
            if self.canonical_context is not None:
                if not isinstance(self.canonical_context, Mapping):
                    raise TypeError("canonical_context_must_be_mapping")
                allowed = {
                    "execution_world",
                    "transport_ingress",
                    "task_facts",
                    "authority_inputs",
                }
                unexpected = sorted(set(self.canonical_context) - allowed)
                if unexpected:
                    raise ValueError(f"canonical_context_field_forbidden:{unexpected[0]}")
            if self.canonical_planning_bundle is not None:
                if not isinstance(self.canonical_planning_bundle, CanonicalPlanningBundle):
                    raise TypeError("canonical_planning_bundle_must_be_CanonicalPlanningBundle")
                expected_context = build_canonical_runtime_context(self)
                if expected_context.context_hash != self.canonical_planning_bundle.context.context_hash:
                    raise ValueError("canonical_planning_bundle_request_binding_mismatch")


    def build_canonical_runtime_context(request: UnifiedRuntimeRequest) -> CanonicalTaskContext:
        """Project only task facts accepted by the canonical planning seam."""
        route_features: Mapping[str, Any] = {}
        if isinstance(request.route, Mapping):
            raw_features = request.route.get("route_features")
            if isinstance(raw_features, Mapping):
                route_features = raw_features
        identity = dict(request.canonical_context or {})
        bound_context = (
            request.canonical_planning_bundle.context
            if request.canonical_planning_bundle is not None
            else None
        )
        execution_world = str(
            identity.get("execution_world")
            or (bound_context.execution_world if bound_context is not None else "product_runtime")
        )
        transport_ingress = str(
            identity.get("transport_ingress")
            or (bound_context.transport_ingress if bound_context is not None else "direct")
        )
        task_facts = identity.get("task_facts")
        if task_facts is None and bound_context is not None:
            task_facts = bound_context.task_facts
        authority_inputs = identity.get("authority_inputs")
        if authority_inputs is None and bound_context is not None:
            authority_inputs = bound_context.authority_inputs
        return CanonicalTaskContext(
            task_id=request.task_id,
            task_type=request.task_type,
            task_desc=request.task_statement,
            execution_world=execution_world,
            transport_ingress=transport_ingress,
            execution_channels=tuple(
                channel
                for channel, enabled in (
                    ("online", request.online_enabled),
                    ("local", request.local_enabled),
                )
                if enabled
            ),
            route_features=route_features,
            task_facts=task_facts or {},
            authority_inputs=authority_inputs or {},
            pillars=request.pillars,
            codeintel=request.codeintel,
            phase_trace=request.phase_trace,
            budget=request.budget,
        )


    def canonical_execution_identity(bundle: CanonicalPlanningBundle) -> dict[str, Any]:
        """JSON-safe identity shared unchanged by Online, Local, and receipts."""
        payload = bundle.to_dict()
        return {
            "schema": "nexus.canonical_execution_identity.v1",
            "task_id": bundle.context.task_id,
            "context_hash": payload["context_hash"],
            "plan_hash": payload["plan_hash"],
            "decision_hash": payload["decision_hash"],
            "projection_hash": payload["projection_hash"],
            "execution_decision_authority": payload["execution_decision_authority"],
            "execution_world": bundle.decision.execution_world,
            "canonical_execution_topology": bundle.decision.execution_topology,
            "execution_decision": payload["execution_decision"],
            "canonical_execution_projection": payload["canonical_execution_projection"],
        }


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


    def build_subprocess_online_invoker(
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
                return normalize_online_invoker_payload(
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
            if include_local_context:
                local_stage = context.get("local", {})
                # P2: never dump raw local_outputs (patch/CoT/private reasoning) into Online.
                # Only structure-preserving online-safe concise evidence may be forwarded.
                if isinstance(local_stage, Mapping) and (
                    local_stage.get("response") or local_stage.get("invoked") or local_stage.get("local_outputs")
                ):
                    build_online_safe_local_forward = _nexus_generated_bindings.build_online_safe_local_forward

                    safe = build_online_safe_local_forward(local_stage)
                    forward = safe.get("forward", {}) if isinstance(safe, Mapping) else {}
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
                capability_results = context.get("capability_results", {})
                if capability_results:
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

            meta = ONLINE_CLI_SPEC_REGISTRY.get(spec.provider, {})
            print_flag = str(meta.get("print_flag") or "").strip()
            argv = list(spec.command)
            stdin_input: str | None = None
            prompt_transport = "stdin"

            model_binding = REGISTERED_CLI_MODEL_BINDING_FLAGS.get(spec.provider)
            if context.get("gateway_invocation_authority") is not None and admitted_model and model_binding and len(argv) != 1:
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
                    return normalize_online_invoker_payload(
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
                    argv = (
                        ([argv[0], subcommand, model_flag, admitted_model, stdin] if model_flag else [argv[0], subcommand, admitted_model, stdin])
                        if subcommand
                        else [argv[0], model_flag, admitted_model, stdin]
                    )
                elif spec.provider == "agy":
                    argv = [argv[0], "--dangerously-skip-permissions", print_flag, stdin]
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
                if spec.provider == "mimo":
                    argv = [argv[0], "run", "--model", model, stdin]
                else:
                    argv = [argv[0], "run", model, stdin]
                stdin_input = ""
                prompt_transport = "argv"
            else:
                if spec.provider == "agy" and "--dangerously-skip-permissions" not in argv:
                    argv.insert(1, "--dangerously-skip-permissions")
                stdin_input = stdin
                prompt_transport = "stdin"

            cmd_fp = hashlib.sha256(json.dumps(argv, ensure_ascii=False).encode("utf-8")).hexdigest()
            exec_path = str(shutil.which(argv[0]) or argv[0])
            exec_hash = hashlib.sha256(exec_path.encode("utf-8")).hexdigest()
            cwd_str = str(spec.working_directory or os.getcwd())
            cwd_hash = hashlib.sha256(cwd_str.encode("utf-8")).hexdigest()
            input_sha256 = hashlib.sha256(stdin.encode("utf-8")).hexdigest()

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
                    "sandboxed_working_directory": bool(spec.working_directory),
                    "provider_input_sha256": input_sha256,
                    "stdout_sha256": hashlib.sha256(stdout_str.encode("utf-8")).hexdigest() if started else "",
                    "stderr_sha256": hashlib.sha256(stderr_str.encode("utf-8")).hexdigest() if started else "",
                    "returncode": retcode,
                    "wall_time_ms": elapsed_ms,
                    "attempt_id": attempt_id,
                    "process_invocation_id": proc_inv_id,
                }

            try:
                result = runner(
                    argv,
                    input=stdin_input,
                    cwd=spec.working_directory or None,
                    capture_output=True,
                    text=True,
                    timeout=spec.timeout_sec,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                elapsed = int((time.monotonic() - start_time) * 1000)
                pe = _build_process_evidence(True, "", str(exc), None, max(0, elapsed))
                return normalize_online_invoker_payload(
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
                )
            except OSError as exc:
                elapsed = int((time.monotonic() - start_time) * 1000)
                pe = _build_process_evidence(False, "", str(exc), None, max(0, elapsed))
                return normalize_online_invoker_payload(
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
                )
            elapsed = int((time.monotonic() - start_time) * 1000)
            stdout = str(getattr(result, "stdout", "") or "")
            stderr = str(getattr(result, "stderr", "") or "")
            returncode = int(getattr(result, "returncode", 1))
            delivered = bool(stdout.strip())
            pe = _build_process_evidence(True, stdout, stderr, returncode, max(0, elapsed))
            return normalize_online_invoker_payload(
                provider=spec.provider,
                task_id=task_id,
                invoked=True,
                output_delivered=delivered,
                gate_passed=returncode == 0 and delivered,
                provider_call_count=1,
                response=stdout,
                raw_response=stdout,
                usage={},
                error="" if returncode == 0 and delivered else "provider_subprocess_failed",
                evidence_refs=(
                    [f"online:{spec.provider}:{task_id}:subprocess"]
                    + ([f"online:{spec.provider}:{task_id}:local_context_forwarded"] if local_context_forwarded else [])
                    + ([f"online:{spec.provider}:{task_id}:capability_context_forwarded"] if capability_context_forwarded else [])
                    + ([f"online:{spec.provider}:{task_id}:compressed_context_applied"] if context.get("capability_context_compressed") else [])
                ),
                transport=TRANSPORT_REGISTERED_CLI,
                selection_source=SELECTION_EXPLICIT_REQUEST,
                extra={"returncode": returncode, "stderr": stderr, "process_evidence": pe},
            )

        invoke.provider = spec.provider  # type: ignore[attr-defined]
        invoke.online_invoker_provider = spec.provider  # type: ignore[attr-defined]
        invoke.physical_provider_transport = runner is subprocess.run  # type: ignore[attr-defined]
        return invoke


    def build_registered_online_invoker(
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
                spec,
                runner=runner,
                include_local_context=include_local_context,
            )

        # Real provider subprocesses: lazy registry validation + binary resolution
        # after authorization.  Exceptions from the resolver never escape the
        # invoker boundary; they produce a fail-closed receipt instead.
        def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
            decision_from_context = _nexus_generated_bindings.decision_from_context
            physical_online_authorized = _nexus_generated_bindings.physical_online_authorized

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
                return normalize_online_invoker_payload(
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
            inject_authorized = bool(
                decision is not None
                and decision.online_execution_authorized
                and decision.online_authorization_source == "injected_test_transport"
            )
            physical_ok = physical_online_authorized(context, injected_transport=False)
            if not inject_authorized and not physical_ok:
                return normalize_online_invoker_payload(
                    provider=key,
                    task_id=task_id,
                    invoked=False,
                    output_delivered=False,
                    gate_passed=False,
                    provider_call_count=0,
                    response="",
                    raw_response="",
                    usage={},
                    error="online_execution_not_authorized",
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
                return normalize_online_invoker_payload(
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
                    spec,
                    runner=runner,
                    include_local_context=include_local_context,
                )
            except (TypeError, OSError) as exc:
                error_code = f"{exc.__class__.__name__}:{exc}"
                return normalize_online_invoker_payload(
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

            payload = invoker(context)
            if inject_authorized and isinstance(payload, dict):
                payload = dict(payload)
                payload["selection_source"] = SELECTION_INJECTED_TRANSPORT
                payload["live_provider_claim"] = False
                if payload.get("transport") in {"", None, TRANSPORT_REGISTERED_CLI}:
                    payload["transport"] = TRANSPORT_STRUCTURED_CALLABLE
            return payload

        invoke.provider = key  # type: ignore[attr-defined]
        invoke.online_invoker_provider = key  # type: ignore[attr-defined]
        invoke.physical_provider_transport = True  # type: ignore[attr-defined]
        return invoke


    def _build_default_memory_retrieval_adapter(project_root: str | Path) -> Any:
        FindingsMemoryLessonStore = _nexus_generated_bindings.FindingsMemoryLessonStore
        LocalJsonlLessonStore = _nexus_generated_bindings.LocalJsonlLessonStore
        MemoryRepositoryLessonStore = _nexus_generated_bindings.MemoryRepositoryLessonStore
        MemoryRetrievalAdapter = _nexus_generated_bindings.MemoryRetrievalAdapter
        NexusCompositeLessonStore = _nexus_generated_bindings.NexusCompositeLessonStore

        root = Path(project_root).expanduser().resolve()
        return MemoryRetrievalAdapter(
            store=NexusCompositeLessonStore(
                [
                    LocalJsonlLessonStore(
                        path=root / ".nexus" / "reports" / "learn" / "learning_closure.jsonl"
                    ),
                    FindingsMemoryLessonStore(project_root=root),
                    MemoryRepositoryLessonStore(project_root=root),
                ]
            )
        )


    def build_local_memory_capability_invoker(
        project_root: str | Path,
        *,
        adapter: Any = None,
        limit: int = 5,
    ) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        """Build the bounded, read-only Local memory edge for the shared runtime.

        The adapter owns backend selection and provenance filtering; this edge
        only binds the task identity, query, and a capability receipt.  A miss is
        a valid read result, while a retrieval failure remains gate-failed.
        """
        if adapter is None:
            adapter = _build_default_memory_retrieval_adapter(project_root)

        bounded_limit = max(1, min(int(limit), 20))

        def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
            task_id = str(context.get("task_id", ""))
            query = str(
                context.get("task_statement")
                or context.get("online_prompt")
                or ""
            ).strip()
            try:
                lessons = list(adapter.retrieve(query_text=query, limit=bounded_limit))
                metadata = dict(getattr(adapter, "last_metadata", {}) or {})
                status = str(metadata.get("status", "ok") or "ok")
                gate_passed = status == "ok"
                evidence_refs = [f"memory:{task_id}:retrieval_attempted"]
                evidence_refs.append(
                    f"memory:{task_id}:{'hit' if lessons else 'no_match'}"
                )
                return {
                    "task_id": task_id,
                    "invoked": True,
                    "gate_passed": gate_passed,
                    "outcome_contributed": bool(lessons),
                    "evidence": "MemoryRetrievalAdapter.retrieve",
                    "evidence_refs": evidence_refs,
                    "response": {
                        "query": query,
                        "lessons": [
                            {
                                "finding_id": lesson.finding_id,
                                "summary": lesson.summary,
                                "relevance_score": lesson.relevance_score,
                                "provenance": lesson.provenance,
                                "source": lesson.source,
                                "pattern_type": lesson.pattern_type,
                                "task_id": lesson.task_id,
                            }
                            for lesson in lessons
                        ],
                        "metadata": metadata,
                    },
                }
            except Exception as exc:  # fail closed in the shared receipt
                return {
                    "task_id": task_id,
                    "invoked": True,
                    "gate_passed": False,
                    "evidence": "MemoryRetrievalAdapter.retrieve",
                    "evidence_refs": [f"memory:{task_id}:retrieval_exception"],
                    "error": f"{exc.__class__.__name__}:{exc}",
                }

        return invoke


    def build_local_search_ranking_capability_invoker(
        project_root: str | Path,
        *,
        adapter: Any = None,
        limit: int = 5,
    ) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        """Build a bounded Local semantic retrieval/ranking capability edge."""
        if adapter is None:
            adapter = _build_default_memory_retrieval_adapter(project_root)
        bounded_limit = max(1, min(int(limit), 20))

        def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
            task_id = str(context.get("task_id", ""))
            query = str(
                context.get("task_statement")
                or context.get("online_prompt")
                or ""
            ).strip()
            route = context.get("route", {})
            route = route if isinstance(route, Mapping) else {}
            anchor_symbol = str(route.get("anchor_symbol", "") or "")
            anchor_file = str(route.get("anchor_file", "") or "")
            try:
                lessons = list(
                    adapter.retrieve_reranked(
                        query_text=query,
                        anchor_symbol=anchor_symbol,
                        anchor_file=anchor_file,
                        limit=bounded_limit,
                        task_id=task_id,
                    )
                )
                metadata = dict(getattr(adapter, "last_metadata", {}) or {})
                status = str(metadata.get("status", "ok") or "ok")
                gate_passed = status == "ok"
                evidence_refs = [f"search:{task_id}:retrieval_attempted"]
                evidence_refs.append(f"search:{task_id}:{'ranked' if lessons else 'no_match'}")
                return {
                    "task_id": task_id,
                    "invoked": True,
                    "gate_passed": gate_passed,
                    "outcome_contributed": bool(lessons),
                    "evidence": "MemoryRetrievalAdapter.retrieve_reranked",
                    "evidence_refs": evidence_refs,
                    "response": {
                        "query": query,
                        "anchor_symbol": anchor_symbol,
                        "anchor_file": anchor_file,
                        "selected_ids": [lesson.finding_id for lesson in lessons],
                        "results": [
                            {
                                "finding_id": lesson.finding_id,
                                "summary": lesson.summary,
                                "relevance_score": lesson.relevance_score,
                                "provenance": lesson.provenance,
                                "source": lesson.source,
                                "pattern_type": lesson.pattern_type,
                                "task_id": lesson.task_id,
                            }
                            for lesson in lessons
                        ],
                        "metadata": metadata,
                    },
                }
            except Exception as exc:  # fail closed in the shared receipt
                return {
                    "task_id": task_id,
                    "invoked": True,
                    "gate_passed": False,
                    "evidence": "MemoryRetrievalAdapter.retrieve_reranked",
                    "evidence_refs": [f"search:{task_id}:retrieval_exception"],
                    "error": f"{exc.__class__.__name__}:{exc}",
                }

        return invoke


    def build_local_ast_capability_invoker(
        project_root: str | Path,
        *,
        max_files: int = 5,
    ) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        """Build a bounded, read-only AST/code-intel capability edge."""
        root = Path(project_root).expanduser().resolve()
        bounded_files = max(1, min(int(max_files), 5))

        def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
            task_id = str(context.get("task_id", ""))
            route = context.get("route", {})
            route = route if isinstance(route, Mapping) else {}
            raw_files = route.get("target_files") or route.get("target_file") or ()
            if isinstance(raw_files, (str, Path)):
                raw_files = (raw_files,)
            files: list[Path] = []
            rejected: list[str] = []
            for raw in list(raw_files or ())[:bounded_files]:
                candidate = (root / str(raw)).resolve() if not Path(str(raw)).is_absolute() else Path(str(raw)).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    rejected.append(f"outside_project_root:{candidate}")
                    continue
                files.append(candidate)
            if not files:
                return {
                    "task_id": task_id,
                    "invoked": True,
                    "gate_passed": False,
                    "evidence": "RuntimeASTExtractor.extract_from_file",
                    "evidence_refs": [f"ast:{task_id}:no_safe_target"],
                    "error": "ast_target_required",
                    "response": {"files": [], "rejected": rejected},
                }

            try:
                RuntimeASTExtractor = _nexus_generated_bindings.RuntimeASTExtractor

                nodes: list[dict[str, Any]] = []
                edges: list[dict[str, Any]] = []
                risks: list[str] = list(rejected)
                for file_path in files:
                    file_nodes, file_edges, file_risks = RuntimeASTExtractor.extract_from_file(str(file_path))
                    nodes.extend(file_nodes)
                    edges.extend(file_edges)
                    risks.extend(file_risks)
                fatal_risks = tuple(
                    risk for risk in risks
                    if str(risk).startswith(("file_not_found:", "ast_parse_error:"))
                )
                gate_passed = bool(nodes) and not fatal_risks
                return {
                    "task_id": task_id,
                    "invoked": True,
                    "gate_passed": gate_passed,
                    "outcome_contributed": bool(nodes),
                    "evidence": "RuntimeASTExtractor.extract_from_file",
                    "evidence_refs": [
                        f"ast:{task_id}:extracted",
                        *[f"ast:{task_id}:file:{path.name}" for path in files],
                    ],
                    "response": {
                        "files": [str(path) for path in files],
                        "node_count": len(nodes),
                        "edge_count": len(edges),
                        "nodes": nodes[:50],
                        "edges": edges[:100],
                        "risks": risks,
                    },
                }
            except Exception as exc:  # fail closed in the shared receipt
                return {
                    "task_id": task_id,
                    "invoked": True,
                    "gate_passed": False,
                    "evidence": "RuntimeASTExtractor.extract_from_file",
                    "evidence_refs": [f"ast:{task_id}:exception"],
                    "error": f"{exc.__class__.__name__}:{exc}",
                }

        return invoke


    def build_prompt_compression_capability_invoker(
        *,
        max_chars: int = 4096,
    ) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
        """Build a deterministic, receipt-backed context compression edge."""
        bounded_max_chars = max(256, min(int(max_chars), 32_000))

        def _text(value: Any) -> str:
            return str(value or "")

        def _compact(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

        def invoke(context: Mapping[str, Any]) -> dict[str, Any]:
            task_id = str(context.get("task_id", ""))
            prompt = _text(context.get("online_prompt") or context.get("task_statement"))
            payload = _text(context.get("online_payload"))
            capability_results = context.get("capability_results", {})
            capability_results = capability_results if isinstance(capability_results, Mapping) else {}
            raw_context = _compact(
                {
                    "prompt": prompt,
                    "payload": payload,
                    "capability_results": capability_results,
                }
            )
            original_chars = len(raw_context)
            compacted = raw_context
            truncated = False
            if original_chars > bounded_max_chars:
                cap_summary = {
                    str(name): {
                        "status": str(value.get("status", "")) if isinstance(value, Mapping) else "",
                        "evidence_refs": list(value.get("evidence_refs", []) or [])[:4]
                        if isinstance(value, Mapping)
                        else [],
                    }
                    for name, value in capability_results.items()
                }
                # Reduce prompt/payload together until the serialized context fits
                # while preserving a valid JSON object and capability evidence keys.
                for field_budget in range(bounded_max_chars, 0, -64):
                    prompt_budget = max(32, field_budget // 2)
                    payload_budget = max(32, field_budget - prompt_budget)
                    candidate = {
                        "prompt": prompt[:prompt_budget],
                        "payload": payload[:payload_budget],
                        "capability_results": cap_summary,
                        "truncated": True,
                    }
                    candidate_text = _compact(candidate)
                    if len(candidate_text) <= bounded_max_chars:
                        compacted = candidate_text
                        truncated = True
                        break
                else:
                    compacted = _compact(
                        {
                            "prompt": "",
                            "payload": "",
                            "capability_results": {},
                            "capability_keys": list(cap_summary)[:8],
                            "truncated": True,
                        }
                    )
                    truncated = True
            compressed_chars = len(compacted)
            gate_passed = bool(original_chars and compacted and compressed_chars <= original_chars)
            evidence_refs = [f"compression:{task_id}:measured"]
            if truncated:
                evidence_refs.append(f"compression:{task_id}:truncated")
            return {
                "task_id": task_id,
                "invoked": True,
                "gate_passed": gate_passed,
                "action": "compress_context",
                "semantic_status": "SUCCEEDED" if gate_passed else "FAILED",
                "outcome_contributed": gate_passed,
                "evidence": "bounded_json_context_compression",
                "evidence_refs": evidence_refs,
                "physical_callable": (
                    "nexus.services.unified_runtime."
                    "build_prompt_compression_capability_invoker"
                ),
                "response": {
                    "action": "compress_context",
                    "semantic_status": "SUCCEEDED" if gate_passed else "FAILED",
                    "original_context_chars": original_chars,
                    "compressed_context_chars": compressed_chars,
                    "compression_ratio": round(1.0 - (compressed_chars / original_chars), 4)
                    if original_chars
                    else 0.0,
                    "truncated": truncated,
                    "compressed_context": compacted,
                },
            }

        return invoke


    def build_structured_online_invoker(
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
            return normalize_online_invoker_payload(
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


    class UnifiedRuntime:
        """Execute one task through a shared planner and emit one receipt.

        This class does not fabricate provider success.  A missing callable,
        provider exception, missing invocation flag, or missing verifier/learning
        result remains visible in the receipt and prevents completion.
        """

        def __init__(
            self,
            *,
            planner: CapabilityPlanner | None = None,
            local_service: Any = None,
            workforce_policy_loader: Any = None,
            consumer_ports: Any = None,
        ) -> None:
            self._planner = planner or CapabilityPlanner()
            self._local_service = local_service
            self._workforce_policy_loader = (
                workforce_policy_loader if workforce_policy_loader is not None else WorkforcePolicyLoader()
            )
            self._consumer_ports = None
            if consumer_ports is not None:
                self.bind_consumer_ports(consumer_ports)

        def bind_consumer_ports(self, consumer_ports: Any) -> None:
            """Bind immutable assembly-owned runtime writer/effect ports."""
            if consumer_ports is None or not hasattr(consumer_ports, "runtime_kwargs"):
                raise ValueError("loaded_writer_consumer_ports_required")
            if self._consumer_ports is not None and self._consumer_ports is not consumer_ports:
                raise ValueError("loaded_writer_consumer_ports_rebind_denied")
            self._consumer_ports = consumer_ports

        def _consumer_runtime_kwargs(self, supplied: dict[str, Any]) -> dict[str, Any]:
            if self._consumer_ports is None:
                return supplied
            defaults = self._consumer_ports.runtime_kwargs()
            for name in ("runtime_writer_factory", "effect_journal", "effect_dispatch", "effect_reconcile"):
                value = supplied.get(name)
                if value is not None and value is not defaults[name]:
                    raise ValueError(f"loaded_writer_{name}_override_denied")
                supplied[name] = defaults[name]
            supplied["effect_fenced"] = bool(supplied.get("effect_fenced") or defaults["effect_fenced"])
            return supplied

        def run(
            self,
            request: UnifiedRuntimeRequest,
            *,
            online_invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            capability_invokers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
            verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            learning: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            receipt_path: str | Path | None = None,
            effect_journal: Any = None,
            effect_dispatch: Any = None,
            effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
            effect_fenced: bool = False,
            owner_context: Any = None,
            runtime_writer_factory: Any = None,
        ) -> dict[str, Any]:
            values = self._consumer_runtime_kwargs({
                "runtime_writer_factory": runtime_writer_factory,
                "effect_journal": effect_journal,
                "effect_dispatch": effect_dispatch,
                "effect_reconcile": effect_reconcile,
                "effect_fenced": effect_fenced,
            })
            runtime_writer_factory = values["runtime_writer_factory"]
            effect_journal = values["effect_journal"]
            effect_dispatch = values["effect_dispatch"]
            effect_reconcile = values["effect_reconcile"]
            effect_fenced = values["effect_fenced"]
            return self._run_once(
                request=request,
                online_invoker=online_invoker,
                capability_invokers=capability_invokers,
                verifier=verifier,
                learning=learning,
                receipt_path=receipt_path,
                replan_authorization=None,
                attempt_number=1,
                parent_receipt=None,
                effect_journal=effect_journal,
                effect_dispatch=effect_dispatch,
                effect_reconcile=effect_reconcile,
                effect_fenced=effect_fenced,
                owner_context=owner_context,
                runtime_writer_factory=runtime_writer_factory,
            )

        def run_replan(
            self,
            previous_receipt: Mapping[str, Any],
            request: UnifiedRuntimeRequest,
            *,
            online_invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            capability_invokers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
            verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            learning: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            receipt_path: str | Path | None = None,
            effect_journal: Any = None,
            effect_dispatch: Any = None,
            effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
            effect_fenced: bool = False,
            owner_context: Any = None,
            runtime_writer_factory: Any = None,
        ) -> dict[str, Any]:
            values = self._consumer_runtime_kwargs({
                "runtime_writer_factory": runtime_writer_factory,
                "effect_journal": effect_journal,
                "effect_dispatch": effect_dispatch,
                "effect_reconcile": effect_reconcile,
                "effect_fenced": effect_fenced,
            })
            runtime_writer_factory = values["runtime_writer_factory"]
            effect_journal = values["effect_journal"]
            effect_dispatch = values["effect_dispatch"]
            effect_reconcile = values["effect_reconcile"]
            effect_fenced = values["effect_fenced"]
            _validate_workforce_route(request.route)
            res = validate_receipt_base(previous_receipt, mode="strict")
            if not res.get("ok"):
                blockers = res.get("blockers") or ["validation_failed"]
                raise ValueError(f"prior_receipt_base_invalid:{','.join(blockers)}")

            if previous_receipt.get("schema") != RECEIPT_SCHEMA:
                raise ValueError("unsupported_prior_receipt_schema")
            if bool(previous_receipt.get("receipt_complete")):
                raise ValueError("prior_receipt_not_incomplete")
            if str(previous_receipt.get("terminal_status", "")) != "INCOMPLETE":
                raise ValueError("prior_receipt_not_incomplete")
            if bool(previous_receipt.get("public_claim_allowed")):
                raise ValueError("prior_receipt_public_claim_not_false")
            if effect_journal is not None:
                EffectJournal = _nexus_generated_bindings.EffectJournal
                if not isinstance(effect_journal, EffectJournal): raise ValueError("canonical_effect_journal_required")
            _validate_runtime_writer_entry(
                runtime_writer_factory,
                owner_context=owner_context,
                receipt_path=receipt_path or previous_receipt.get("receipt_path"),
                effect_journal=effect_journal,
            )
            _validate_runtime_owner_context(owner_context, receipt_path or previous_receipt.get("receipt_path"))
            _validate_runtime_effect_owner(owner_context, effect_journal)
            if previous_receipt.get("effect_journal_bindings") and effect_journal is None:
                raise ValueError("replan_effect_journal_required")
            for binding in previous_receipt.get("effect_journal_bindings", []) or []:
                if not isinstance(binding, Mapping) or binding.get("state") != "COMPLETED":
                    raise ValueError("replan_effect_binding_incomplete")
                record = effect_journal.get(str(binding.get("effect_id"))) if effect_journal is not None else None
                if not isinstance(record, Mapping) or any(record.get(k) != binding.get(k) for k in ("operation_id", "effect_id", "request_digest", "generation", "state", "result_digest", "project_root")):
                    raise ValueError("replan_effect_binding_mismatch")

            if str(previous_receipt.get("task_id", "")) != str(request.task_id):
                raise ValueError("replan_task_id_mismatch")
            if str(previous_receipt.get("workspace_revision", "")) != str(request.workspace_revision):
                raise ValueError("replan_workspace_revision_mismatch")

            replan_req = previous_receipt.get("execution_replan_request")
            if not isinstance(replan_req, Mapping) or replan_req.get("schema") != "nexus.execution_replan_request.v1":
                raise ValueError("replan_request_missing")
            if str(replan_req.get("task_id", "")) != str(request.task_id):
                raise ValueError("replan_task_id_mismatch")
            if str(replan_req.get("source_planner_decision_id", "")) != str(previous_receipt.get("planner_decision_id", "")):
                raise ValueError("replan_request_integrity_mismatch")
            if not bool(replan_req.get("verifier_outcome_trusted")):
                raise ValueError("replan_request_not_trusted")
            if not bool(replan_req.get("replan_required")):
                raise ValueError("replan_not_required")
            if bool(replan_req.get("manual_review_required")):
                raise ValueError("replan_manual_review_required")
            if bool(replan_req.get("public_claim_allowed")):
                raise ValueError("replan_request_not_trusted")

            if "workforce_admission" in previous_receipt:
                prior_canonical = previous_receipt.get("canonical_execution")
                if not _workforce_admission_required(
                    request,
                    physical_online=_physical_online_invoker(online_invoker),
                ) and not isinstance(prior_canonical, Mapping):
                    raise ValueError("replan_workforce_admission_required")

            recomputed = build_execution_replan_request(
                task_id=request.task_id,
                planner_decision_id=str(previous_receipt.get("planner_decision_id", "")),
                current_execution_depth=str(previous_receipt.get("execution_depth", "")),
                verifier_stage=previous_receipt.get("verifier"),
            )
            stored_proj = execution_replan_request_authority_projection(replan_req)
            rebuilt_proj = execution_replan_request_authority_projection(recomputed)
            if stored_proj != rebuilt_proj:
                raise ValueError("replan_request_integrity_mismatch")

            current_depth = str(previous_receipt.get("execution_depth") or "LIGHT")
            if current_depth == EXECUTION_DEPTH_FULL:
                raise ValueError("replan_manual_review_required")

            expected_next_depth = next_execution_depth_after_failure(current_depth)
            if str(replan_req.get("requested_execution_depth", "")) != expected_next_depth:
                raise ValueError("replan_depth_transition_invalid")

            prior_attempt_info = previous_receipt.get("execution_attempt")
            prior_attempt_num = int(prior_attempt_info.get("attempt_number", 1)) if isinstance(prior_attempt_info, Mapping) else 1
            if prior_attempt_num >= 2:
                raise ValueError("replan_attempt_budget_exhausted")

            base = previous_receipt.get("receipt_base") if isinstance(previous_receipt.get("receipt_base"), Mapping) else previous_receipt
            canonical_task_id = str(base.get("task_id") or previous_receipt.get("task_id") or "")
            canonical_workspace_revision = str(base.get("workspace_revision") or previous_receipt.get("workspace_revision") or "")
            canonical_planner_decision_id = str(base.get("planner_decision_id") or previous_receipt.get("planner_decision_id") or "")
            canonical_receipt_hash = str(base.get("receipt_hash") or previous_receipt.get("receipt_hash") or "")
            canonical_run_anchor_hash = str(base.get("run_anchor_hash") or previous_receipt.get("run_anchor_hash") or "")

            authorization = ExecutionReplanAuthorization(
                task_id=canonical_task_id,
                workspace_revision=canonical_workspace_revision,
                source_planner_decision_id=canonical_planner_decision_id,
                source_replan_request_id=str(replan_req.get("replan_request_id", "")),
                source_receipt_hash=canonical_receipt_hash,
                source_run_anchor_hash=canonical_run_anchor_hash,
                requested_execution_depth=str(replan_req.get("requested_execution_depth", "")),
                attempt_number=2,
                max_attempts=2,
            )

            prior_canonical = previous_receipt.get("canonical_execution")
            if isinstance(prior_canonical, Mapping):
                if request.canonical_planning_bundle is not None:
                    raise ValueError("canonical_replan_requires_fresh_planning_bundle")
                replan_bundle = replan_canonical_task_bundle(
                    build_canonical_runtime_context(request),
                    authorization,
                )
                request = replace(request, canonical_planning_bundle=replan_bundle)

            return self._run_once(
                request=request,
                online_invoker=online_invoker,
                capability_invokers=capability_invokers,
                verifier=verifier,
                learning=learning,
                receipt_path=receipt_path,
                replan_authorization=authorization,
                attempt_number=2,
                parent_receipt=previous_receipt,
                effect_journal=effect_journal,
                effect_dispatch=effect_dispatch,
                effect_reconcile=effect_reconcile,
                effect_fenced=effect_fenced,
                owner_context=owner_context,
                runtime_writer_factory=runtime_writer_factory,
            )

        def _run_once(
            self,
            request: UnifiedRuntimeRequest,
            *,
            online_invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            capability_invokers: Mapping[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] | None = None,
            verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            learning: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
            receipt_path: str | Path | None = None,
            replan_authorization: ExecutionReplanAuthorization | None = None,
            attempt_number: int = 1,
            parent_receipt: Mapping[str, Any] | None = None,
            effect_journal: Any = None,
            effect_dispatch: Any = None,
            effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
            effect_fenced: bool = False,
            owner_context: Any = None,
            runtime_writer_factory: Any = None,
        ) -> dict[str, Any]:
            request.validate()
            _validate_runtime_writer_entry(
                runtime_writer_factory,
                owner_context=owner_context,
                receipt_path=receipt_path,
                effect_journal=effect_journal,
            )
            _validate_runtime_owner_context(owner_context, receipt_path)
            _validate_runtime_effect_owner(owner_context, effect_journal)
            fenced = effect_fenced or bool(request.route.get("effect_fenced", False))
            EffectDispatchPort = _nexus_generated_bindings.EffectDispatchPort
            EffectJournal = _nexus_generated_bindings.EffectJournal
            EffectReconcilePort = _nexus_generated_bindings.EffectReconcilePort
            if effect_journal is not None:
                if not isinstance(effect_journal, EffectJournal):
                    raise ValueError("canonical_effect_journal_required")
                if not isinstance(effect_dispatch, EffectDispatchPort):
                    raise ValueError("effect_dispatch_port_required")
                if not isinstance(effect_reconcile, EffectReconcilePort):
                    raise ValueError("effect_reconcile_port_required")
            if fenced and effect_journal is None:
                raise ValueError("effect_journal_required_for_fenced_effects")
            if fenced and not isinstance(effect_dispatch, EffectDispatchPort):
                raise ValueError("effect_dispatch_port_required")
            if fenced and not isinstance(effect_reconcile, EffectReconcilePort):
                raise ValueError("effect_reconcile_port_required")
            _validate_workforce_route(request.route)
            planner_route = dict(request.route)
            planner_route.setdefault("local_enabled", request.local_enabled)
            planner_route.setdefault("online_enabled", request.online_enabled)
            # Opt-in Nexus Light: merge deterministic preflight/postflight invokers.
            # Default route is unchanged when flags are absent.
            merged_invokers: dict[str, Callable[[Mapping[str, Any]], Mapping[str, Any]]] = dict(
                capability_invokers or {}
            )
            capability_invokers = merged_invokers or None
            planner_kwargs: dict[str, Any] = {
                "task_desc": request.task_statement,
                "task_type": request.task_type,
                "route": planner_route,
                "pillars": dict(request.pillars),
                "codeintel": dict(request.codeintel),
                "phase_trace": dict(request.phase_trace),
                "budget": dict(request.budget),
                "skills": [dict(item) for item in request.skills],
            }
            if replan_authorization is not None:
                planner_kwargs["replan_authorization"] = replan_authorization

            canonical_bundle = request.canonical_planning_bundle
            if canonical_bundle is not None:
                plan = canonical_bundle.plan
                plan_payload = plan.to_dict()
                plan_hash = canonical_bundle.plan_hash
                canonical_execution = canonical_execution_identity(canonical_bundle)
            else:
                plan = self._planner.plan(**planner_kwargs)
                plan_payload = plan.to_dict()
                plan_hash = _hash_json(plan_payload)
                canonical_execution = None
            # Single stable decision id from actual plan payload/hash — never invent
            # a second id downstream on receipt, context_trace, or capability rows.
            planner_decision_id = plan_hash

            if attempt_number == 1:
                attempt_payload_for_id = {
                    "task_id": str(request.task_id),
                    "workspace_revision": str(request.workspace_revision),
                    "attempt_number": 1,
                    "parent_receipt_hash": "",
                    "source_replan_request_id": "",
                    "planner_decision_id": str(planner_decision_id),
                    "execution_depth": str(plan.execution_depth),
                }
                parent_attempt_id = ""
                parent_receipt_hash = ""
                parent_run_anchor_hash = ""
                source_replan_request_id = ""
                source_planner_decision_id = ""
            else:
                parent_attempt_info = (parent_receipt or {}).get("execution_attempt") or {}
                parent_attempt_id = str(parent_attempt_info.get("attempt_id") or "")
                parent_receipt_hash = str((parent_receipt or {}).get("receipt_hash") or "")
                parent_run_anchor_hash = str((parent_receipt or {}).get("run_anchor_hash") or "")
                source_replan_request_id = str(((parent_receipt or {}).get("execution_replan_request") or {}).get("replan_request_id") or "")
                source_planner_decision_id = str((parent_receipt or {}).get("planner_decision_id") or "")
                attempt_payload_for_id = {
                    "task_id": str(request.task_id),
                    "workspace_revision": str(request.workspace_revision),
                    "attempt_number": 2,
                    "parent_receipt_hash": parent_receipt_hash,
                    "source_replan_request_id": source_replan_request_id,
                    "planner_decision_id": str(planner_decision_id),
                    "execution_depth": str(plan.execution_depth),
                }

            attempt_id = build_execution_attempt_id(
                task_id=str(request.task_id),
                workspace_revision=str(request.workspace_revision),
                attempt_number=attempt_number,
                parent_receipt_hash=parent_receipt_hash,
                source_replan_request_id=source_replan_request_id,
                planner_decision_id=str(planner_decision_id),
                execution_depth=str(plan.execution_depth),
            )

            execution_attempt = {
                "schema": "nexus.execution_attempt.v1",
                "attempt_number": attempt_number,
                "max_attempts": 2,
                "attempt_id": attempt_id,
                "is_replan": (attempt_number > 1),
                "parent_attempt_id": parent_attempt_id,
                "parent_receipt_hash": parent_receipt_hash,
                "parent_run_anchor_hash": parent_run_anchor_hash,
                "source_replan_request_id": source_replan_request_id,
                "source_planner_decision_id": source_planner_decision_id,
                "planner_decision_id": str(planner_decision_id),
                "execution_depth": str(plan.execution_depth),
            }

            plan_payload["execution_attempt"] = execution_attempt

            snapshot = plan_payload.get("signal_snapshot")
            if isinstance(snapshot, Mapping):
                snapshot_with_id = dict(snapshot)
                snapshot_with_id["planner_decision_id"] = planner_decision_id
                plan_payload = dict(plan_payload)
                plan_payload["signal_snapshot"] = snapshot_with_id
            else:
                plan_payload = dict(plan_payload)
                plan_payload["signal_snapshot"] = {"planner_decision_id": planner_decision_id}
            # Stamp stable ids onto the payload so Online armor / invokers share the
            # same plan_hash as planner stage (hash itself remains pre-stamp).
            plan_payload["plan_hash"] = plan_hash
            plan_payload["planner_decision_id"] = planner_decision_id
            if canonical_execution is not None:
                plan_payload["canonical_execution"] = canonical_execution
                canonical_snapshot = dict(plan_payload.get("signal_snapshot") or {})
                canonical_snapshot["canonical_execution"] = canonical_execution
                plan_payload["signal_snapshot"] = canonical_snapshot
            planner_stage = _stage(
                "planner",
                status="SUCCEEDED",
                invoked=True,
                evidence_present=True,
                gate_passed=True,
                evidence_refs=["runtime:planner_invoked"],
                selected_capabilities=list(plan.selected_capabilities),
                required_capabilities=list(plan.required_capabilities),
                conditional_capabilities=list(plan.conditional_capabilities),
                pending_capabilities=list(plan.pending_capabilities),
                plan_schema=plan.schema_version,
                plan_hash=plan_hash,
                planner_decision_id=planner_decision_id,
                execution_depth=plan.execution_depth,
                execution_attempt=execution_attempt,
                canonical_execution=canonical_execution or {},
            )

            stages: dict[str, dict[str, Any]] = {"planner": planner_stage}

            physical_local_required = bool(
                request.local_enabled
                and "local_model_executor" in set(plan.selected_capabilities)
                and _physical_local_service(self._local_service)
            )
            physical_online_required = bool(
                request.online_enabled and _physical_online_invoker(online_invoker)
            )
            workforce_admission_required = _workforce_admission_required(
                request,
                physical_local=physical_local_required,
                physical_online=physical_online_required,
            )

            workforce_admission_payload: dict[str, Any] | None = None
            gateway_invocation_authority: dict[str, Any] | None = None
            local_model_invocation_authority: dict[str, Any] | None = None
            if workforce_admission_required:
                signal_snapshot = plan_payload.get("signal_snapshot")
                raw_workforce_demands = (
                    signal_snapshot.get("workforce_demands")
                    if isinstance(signal_snapshot, Mapping)
                    else None
                )
                try:
                    workforce_admission = evaluate_runtime_workforce_admission(
                        raw_workforce_demands,
                        request.route.get("workforce_bindings"),
                        self._workforce_policy_loader,
                    )
                    workforce_admission_payload = (
                        workforce_admission.to_dict()
                        if callable(getattr(workforce_admission, "to_dict", None))
                        else dict(workforce_admission)
                        if isinstance(workforce_admission, Mapping)
                        else None
                    )
                except Exception:
                    workforce_admission_payload = None
                if not isinstance(workforce_admission_payload, dict):
                    workforce_admission_payload = {
                        "schema": "nexus.runtime_workforce_admission.v1",
                        "policy_identity": {},
                        "overall_decision": "BLOCK",
                        "overall_reasons": ["workforce_admission_missing"],
                        "records": [],
                        "aggregate_binding_hash": "",
                    }
                workforce_lineage = _build_workforce_admission_lineage(
                    route=request.route,
                    attempt_number=attempt_number,
                    current_aggregate_binding_hash=workforce_admission_payload.get(
                        "aggregate_binding_hash", ""
                    ),
                    current_planner_decision_id=planner_decision_id,
                    replan_authorization=replan_authorization,
                    parent_receipt=parent_receipt,
                )
                plan_payload = dict(plan_payload)
                plan_payload["workforce_admission"] = workforce_admission_payload
                plan_payload["workforce_admission_lineage"] = workforce_lineage
                stamped_snapshot = dict(plan_payload.get("signal_snapshot") or {})
                stamped_snapshot["workforce_admission"] = workforce_admission_payload
                stamped_snapshot["workforce_admission_lineage"] = workforce_lineage
                plan_payload["signal_snapshot"] = stamped_snapshot
                workforce_decision = str(workforce_admission_payload.get("overall_decision") or "BLOCK")
                rebind_blocked = workforce_lineage["status"] == "BLOCKED_REBIND"
                effective_workforce_decision = "BLOCK" if rebind_blocked else workforce_decision
                effective_workforce_reason = (
                    "workforce_rebind_not_authorized" if rebind_blocked else ""
                )
                if request.online_enabled:
                    gateway_invocation_authority = _build_gateway_invocation_authority(
                        request=request,
                        plan_payload=plan_payload,
                        admission_payload=workforce_admission_payload,
                        invoker=online_invoker,
                        effective_decision=effective_workforce_decision,
                        effective_reason=effective_workforce_reason,
                    )
                    plan_payload = dict(plan_payload)
                    stamped_snapshot = dict(plan_payload.get("signal_snapshot") or {})
                    stamped_snapshot["gateway_invocation_authority"] = gateway_invocation_authority
                    plan_payload["signal_snapshot"] = stamped_snapshot
                    plan_payload["gateway_invocation_authority"] = gateway_invocation_authority
                if request.local_enabled:
                    local_model_invocation_authority = _build_local_model_invocation_authority(
                        plan_payload=plan_payload,
                        admission_payload=workforce_admission_payload,
                        effective_decision=effective_workforce_decision,
                        effective_reason=effective_workforce_reason,
                    )
                    plan_payload = dict(plan_payload)
                    stamped_snapshot = dict(plan_payload.get("signal_snapshot") or {})
                    stamped_snapshot["local_model_invocation_authority"] = local_model_invocation_authority
                    plan_payload["signal_snapshot"] = stamped_snapshot
                    plan_payload["local_model_invocation_authority"] = local_model_invocation_authority
                workforce_evidence_refs = [
                    f"runtime:workforce_admission:{workforce_decision}:"
                    f"{workforce_admission_payload.get('aggregate_binding_hash', '')[:16]}"
                ]
                workforce_stage_fields: dict[str, Any] = {
                    "workforce_admission_lineage": workforce_lineage,
                    "effective_decision": effective_workforce_decision,
                    "effective_reason": effective_workforce_reason,
                }
                if rebind_blocked:
                    workforce_evidence_refs.append(
                        "runtime:workforce_rebind:BLOCKED_REBIND:not_authorized"
                    )
                    workforce_stage_fields["rebind_evidence"] = {
                        "status": "BLOCKED_REBIND",
                        "authorized": False,
                        "reason": "workforce_rebind_not_authorized",
                    }
                workforce_stage = _stage(
                    "workforce_admission",
                    status=(
                        "SUCCEEDED"
                        if effective_workforce_decision == "ALLOW"
                        else ("BLOCKED" if effective_workforce_decision == "BLOCK" else "INCOMPLETE")
                    ),
                    invoked=True,
                    evidence_present=True,
                    gate_passed=effective_workforce_decision == "ALLOW",
                    evidence_refs=workforce_evidence_refs,
                    reason=effective_workforce_reason,
                    decision=effective_workforce_decision,
                    result=workforce_admission_payload,
                    aggregate_binding_hash=workforce_admission_payload.get("aggregate_binding_hash", ""),
                    **workforce_stage_fields,
                )
                stages["workforce_admission"] = workforce_stage

                if workforce_decision in {"BLOCK", "ESCALATE"} or rebind_blocked:
                    terminal_status = (
                        "BLOCKED"
                        if workforce_decision == "BLOCK" or rebind_blocked
                        else "INCOMPLETE"
                    )
                    terminal_stages = dict(stages)
                    for stage_name in ("local", "online", "verifier", "learning"):
                        terminal_stages[stage_name] = _stage(
                            stage_name,
                            status="NOT_REQUESTED",
                            reason=(
                                "blocked_by_workforce_rebind"
                                if rebind_blocked
                                else "blocked_by_workforce_admission"
                            ),
                            invoked=False,
                            evidence_present=False,
                            gate_passed=False,
                        )
                    if local_model_invocation_authority is not None:
                        terminal_stages["local"] = _local_authority_failure_stage(
                            task_id=request.task_id,
                            authority=local_model_invocation_authority,
                        )
                    if request.online_enabled and gateway_invocation_authority is not None:
                        terminal_stages["online"] = _online_authority_failure_stage(
                            task_id=request.task_id,
                            authority=gateway_invocation_authority,
                        )
                    terminal_planner = dict(planner_stage)
                    terminal_planner["plan_payload"] = plan_payload
                    terminal_stages["planner"] = terminal_planner
                    terminal_context_trace = {
                        "task_id": request.task_id,
                        "workspace_revision": request.workspace_revision,
                        "planner_decision_id": planner_decision_id,
                        "execution_depth": plan.execution_depth,
                        "execution_attempt": execution_attempt,
                        "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
                        "source_replan_request_id": execution_attempt["source_replan_request_id"],
                        "workforce_admission_lineage": workforce_lineage,
                        "route": {
                            key: request.route.get(key)
                            for key in (
                                "mainchain_entry",
                                "mainchain_route_version",
                                "route_freeze",
                                "product_entry",
                                "with_nexus_armor",
                            )
                            if isinstance(request.route, Mapping) and key in request.route
                        },
                    }
                    if canonical_execution is not None:
                        terminal_context_trace["canonical_execution"] = canonical_execution
                    if gateway_invocation_authority is not None:
                        terminal_context_trace["gateway_invocation_authority"] = gateway_invocation_authority
                    if local_model_invocation_authority is not None:
                        terminal_context_trace["local_model_invocation_authority"] = local_model_invocation_authority
                    terminal_receipt = {
                        "schema": RECEIPT_SCHEMA,
                        "task_id": request.task_id,
                        "workspace_revision": request.workspace_revision,
                        "planner_decision_id": planner_decision_id,
                        "plan_hash": plan_hash,
                        "execution_depth": plan.execution_depth,
                        "execution_attempt": execution_attempt,
                        "context_trace": terminal_context_trace,
                        "planner": terminal_planner,
                        "plan_payload": plan_payload,
                        "workforce_admission": workforce_admission_payload,
                        "workforce_admission_lineage": workforce_lineage,
                        "capabilities": [],
                        "capability_results": {},
                        "local": terminal_stages["local"],
                        "online": terminal_stages["online"],
                        "verifier": terminal_stages["verifier"],
                        "learning": terminal_stages["learning"],
                        "stages": list(terminal_stages.values()),
                        "evidence_refs": sorted(
                            {
                                *list(request.evidence_refs),
                                *list(workforce_stage["evidence_refs"]),
                            }
                        ),
                        "receipt_complete": False,
                        "capability_closure_complete": False,
                        "terminal_status": terminal_status,
                        "claim_boundary": {
                            "task_identity_shared": True,
                            "planner_shared": True,
                            "local_online_continuation": False,
                            "receipt_complete": False,
                            "capability_closure_complete": False,
                            "outcome_contributed": False,
                            "value_measured": False,
                            "public_claim_allowed": False,
                        },
                        "selection_authority": "CapabilityPlanner",
                        "selected_capabilities": list(plan.selected_capabilities),
                        "executed_capabilities": [],
                        "contributed_capabilities": [],
                        "consumed_evidence_ids": [],
                        "capability_call_count": 0,
                        "local_call_count": 0,
                        "online_call_count": 0,
                        "verifier_call_count": 0,
                        "learning_call_count": 0,
                        "provider_call_count": 0,
                        "invocation_counts": {
                            "capability": 0,
                            "local": 0,
                            "online": 0,
                            "verifier": 0,
                            "learning": 0,
                        },
                        "public_claim_allowed": False,
                    }
                    if canonical_execution is not None:
                        terminal_receipt["canonical_execution"] = canonical_execution
                        terminal_receipt["execution_world"] = canonical_execution[
                            "execution_world"
                        ]
                        terminal_receipt["canonical_execution_topology"] = canonical_execution[
                            "canonical_execution_topology"
                        ]
                    if gateway_invocation_authority is not None:
                        terminal_receipt["gateway_invocation_authority"] = gateway_invocation_authority
                    if local_model_invocation_authority is not None:
                        terminal_receipt["local_model_invocation_authority"] = local_model_invocation_authority
                    attach_failure_diagnostics(terminal_receipt)
                    attach_r3_receipt_base(terminal_receipt)
                    if receipt_path is not None:
                        path = Path(receipt_path)
                        terminal_receipt["receipt_path"] = str(path)
                        with _runtime_write_context(runtime_writer_factory, task_id=request.task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                            _assert_runtime_context_active(runtime_writer_factory, write_context)
                            _assert_runtime_receipt_owner(write_context, path)
                            _write_receipt_atomic(path, terminal_receipt)
                    return terminal_receipt

            capability_results: dict[str, dict[str, Any]] = {}
            effect_journal_bindings: list[dict[str, Any]] = []
            postflight_names = {
                "acceptance_check",
                "artifact_gate",
                "bdd_acceptance_skill",
                "claim_gate",
                "delivery_gate",
            }
            # Coverage handlers for every selected name (real / stub / explicit skip).
            # Not a product route — invoker map only.
            LOCAL_STAGE_CAPABILITIES = _nexus_generated_bindings.LOCAL_STAGE_CAPABILITIES
            ensure_selected_coverage_invokers = _nexus_generated_bindings.ensure_selected_coverage_invokers

            invoker_map = ensure_selected_coverage_invokers(
                list(plan.selected_capabilities),
                capability_invokers,
                codeintel=dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {},
            )
            # P2: preflight BEFORE Local/Online so both stages share one evidence baseline.
            # Local-owned capabilities are not preflight-invoked here.
            preflight_items = [
                (name, inv)
                for name, inv in invoker_map.items()
                if name not in postflight_names
                and name not in LOCAL_STAGE_CAPABILITIES
                and name in plan.selected_capabilities
            ]
            postflight_items = [
                (name, inv)
                for name, inv in invoker_map.items()
                if name in postflight_names and name in plan.selected_capabilities
            ]

            def _invoke_capability(
                capability_name: str,
                invoker: Any,
                capability_context: dict[str, Any],
            ) -> None:
                if capability_name not in plan.selected_capabilities:
                    return
                if not callable(invoker):
                    result: Any = {
                        "task_id": request.task_id,
                        "invoked": False,
                        "gate_passed": False,
                        "evidence_refs": [f"capability:{capability_name}:{request.task_id}:not_callable"],
                    }
                else:
                    try:
                        if fenced and getattr(invoker, "effectful", False) is not True and getattr(invoker, "pure", False) is not True:
                            raise ValueError("unclassified_selected_capability")
                        if effect_journal is not None and getattr(invoker, "effectful", False) is True:
                            import hashlib as _effect_hash
                            import json as _effect_json
                            effect_context = dict(capability_context)
                            effect_context["capability_name"] = capability_name
                            request_digest = _effect_hash.sha256(_effect_json.dumps(effect_context, sort_keys=True, default=str).encode()).hexdigest()
                            identity = {
                                "task_id": request.task_id,
                                "workspace_revision": request.workspace_revision,
                                "planner_decision_id": planner_decision_id,
                                "attempt_number": attempt_number,
                                "action": f"capability:{capability_name}",
                                "subject_revision": request.workspace_revision,
                                "request_digest": request_digest,
                            }
                            deterministic_effect_id = _nexus_generated_bindings.deterministic_effect_id
                            operation_digest = _nexus_generated_bindings.operation_digest
                            effect_journal_bindings.append({"effect_id": deterministic_effect_id(identity), "operation_id": operation_digest(identity), "action": identity["action"], "request_digest": request_digest, "project_root": str(effect_journal.project_root)})
                            with _runtime_write_context(runtime_writer_factory, task_id=request.task_id, role="effect_journal", path=effect_journal.path, owner_context=owner_context) as effect_context:
                                _assert_runtime_context_active(runtime_writer_factory, effect_context)
                                result = effect_journal.execute(
                                    identity=identity,
                                    subject=request.task_id,
                                    request_digest=request_digest,
                                    dispatch=lambda: effect_dispatch.dispatch(lambda: invoker(capability_context)),
                                    reconcile=lambda record: effect_reconcile.reconcile(record),
                                )
                            saved = effect_journal.get(deterministic_effect_id(identity)) or {}
                            effect_journal_bindings[-1].update({"generation": saved.get("generation"), "state": saved.get("state"), "result_digest": saved.get("result_digest", ""), "project_root": str(effect_journal.project_root)})
                        else:
                            result = invoker(capability_context)
                    except Exception as exc:  # fail closed in the shared receipt
                        result = {
                            "task_id": request.task_id,
                            "invoked": True,
                            "gate_passed": False,
                            "evidence_refs": [f"capability:{capability_name}:{request.task_id}:exception"],
                            "error": f"{exc.__class__.__name__}:{exc}",
                        }
                capability_results[capability_name] = _capability_stage(
                    capability_name,
                    request.task_id,
                    result,
                )

            # Empty local placeholder until after shared preflight.
            local_stage = _stage("local", status="NOT_REQUESTED", reason="pending_shared_preflight")
            capability_context = {
                "schema": REQUEST_SCHEMA,
                "task_id": request.task_id,
                "workspace_revision": request.workspace_revision,
                "task_statement": request.task_statement,
                "task_type": request.task_type,
                "route": dict(request.route),
                "codeintel": dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {},
                "pillars": dict(request.pillars) if isinstance(request.pillars, Mapping) else {},
                "planner": plan_payload,
                "local": local_stage,
                "online_prompt": request.online_prompt,
                "online_payload": request.online_payload,
                "capability_results": capability_results,
            }
            if request.local_request is not None:
                for field_name in ("workspace_root", "target_file", "target_symbol"):
                    value = getattr(request.local_request, field_name, None)
                    if value not in (None, ""):
                        capability_context[field_name] = value
            for capability_name, invoker in preflight_items:
                _invoke_capability(capability_name, invoker, capability_context)

            # Immutable shared evidence bundle (P2) — both Local and Online must see same baseline.
            build_capability_evidence_bundle = _nexus_generated_bindings.build_capability_evidence_bundle

            evidence_bundle = build_capability_evidence_bundle(
                task_id=request.task_id,
                workspace_revision=request.workspace_revision,
                task_statement=request.task_statement,
                plan_payload=plan_payload,
                plan_hash=plan_hash,
                planner_decision_id=planner_decision_id,
                capability_results=capability_results,
                selected_capabilities=list(plan.selected_capabilities),
                source_hash=hashlib.sha256(
                    f"{request.workspace_revision}:{request.task_statement}".encode("utf-8")
                ).hexdigest(),
            )
            plan_payload = dict(plan_payload)
            _evidence_consumer_view = _nexus_generated_bindings._evidence_consumer_view
            _verify_evidence_bundle = _nexus_generated_bindings._verify_evidence_bundle
            sealed_verdict = _verify_evidence_bundle(evidence_bundle)
            if not sealed_verdict.get("ok"):
                # Fail closed: do NOT mutate sealed bundle with seal_verify;
                # do NOT call Local/Online; terminal BLOCKED.
                stages["shared_capability_evidence"] = _stage(
                    "shared_capability_evidence",
                    status="BLOCKED",
                    invoked=True,
                    evidence_present=True,
                    gate_passed=False,
                    evidence_refs=["runtime:evidence_bundle:seal_failed"],
                    reason="capability_evidence_seal_failed",
                    blockers=list(sealed_verdict.get("blockers") or []),
                    baseline_hash=str(evidence_bundle.get("baseline_hash") or ""),
                    bundle_hash=str(evidence_bundle.get("bundle_hash") or ""),
                )
                stages["local"] = _stage(
                    "local",
                    status="NOT_REQUESTED",
                    reason="blocked_by_evidence_seal",
                    invoked=False,
                    gate_passed=False,
                )
                if local_model_invocation_authority is not None:
                    stages["local"] = _local_authority_failure_stage(
                        task_id=request.task_id,
                        authority=local_model_invocation_authority,
                    )
                stages["online"] = _stage(
                    "online",
                    status="NOT_REQUESTED",
                    reason="blocked_by_evidence_seal",
                    invoked=False,
                    gate_passed=False,
                    provider_call_count=0,
                    context_trace=(
                        {"gateway_invocation_authority": gateway_invocation_authority}
                        if gateway_invocation_authority is not None
                        else {}
                    ),
                )
                stages["verifier"] = _stage(
                    "verifier",
                    status="NOT_REQUESTED",
                    reason="blocked_by_evidence_seal",
                    invoked=False,
                    gate_passed=False,
                )
                stages["learning"] = _stage(
                    "learning",
                    status="NOT_REQUESTED",
                    reason="blocked_by_evidence_seal",
                    invoked=False,
                    gate_passed=False,
                )
                claim_boundary = {
                    "task_identity_shared": True,
                    "planner_shared": planner_stage["invoked"],
                    "local_online_continuation": False,
                    "receipt_complete": False,
                    "capability_closure_complete": False,
                    "outcome_contributed": False,
                    "value_measured": False,
                    "public_claim_allowed": False,
                }
                blocked_receipt = {
                    "schema": RECEIPT_SCHEMA,
                    "task_id": request.task_id,
                    "workspace_revision": request.workspace_revision,
                    "planner_decision_id": planner_decision_id,
                    "execution_depth": plan.execution_depth,
                    "planner": planner_stage,
                    "capabilities": [],
                    "capability_results": capability_results,
                    "local": stages["local"],
                    "online": stages["online"],
                    "verifier": stages["verifier"],
                    "learning": stages["learning"],
                    "stages": list(stages.values()),
                    "evidence_refs": sorted(
                        {
                            *list(request.evidence_refs),
                            "runtime:evidence_bundle:seal_failed",
                        }
                    ),
                    "receipt_complete": False,
                    "capability_closure_complete": False,
                    "terminal_status": "BLOCKED",
                    "claim_boundary": claim_boundary,
                    "capability_evidence_bundle": evidence_bundle,
                    "seal_verify": sealed_verdict,
                    "selection_authority": "CapabilityPlanner",
                    "local_call_count": 0,
                    "online_call_count": 0,
                    "selected_capabilities": list(plan.selected_capabilities),
                    "executed_capabilities": [],
                    "consumed_evidence_ids": [],
                    "contributed_capabilities": [],
                    "public_claim_allowed": False,
                }
                terminal_planner = dict(planner_stage)
                terminal_context_trace = {
                    "task_id": request.task_id,
                    "workspace_revision": request.workspace_revision,
                    "planner_decision_id": planner_decision_id,
                    "execution_depth": plan.execution_depth,
                    "execution_attempt": execution_attempt,
                    "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
                    "source_replan_request_id": execution_attempt["source_replan_request_id"],
                }
                if workforce_admission_payload is not None:
                    terminal_planner["plan_payload"] = plan_payload
                    terminal_context_trace["workforce_admission_lineage"] = workforce_lineage
                if gateway_invocation_authority is not None:
                    terminal_context_trace["gateway_invocation_authority"] = gateway_invocation_authority
                if local_model_invocation_authority is not None:
                    terminal_context_trace["local_model_invocation_authority"] = local_model_invocation_authority
                blocked_receipt["planner"] = terminal_planner
                blocked_receipt["execution_attempt"] = execution_attempt
                blocked_receipt["context_trace"] = terminal_context_trace
                capability_call_count = sum(
                    1
                    for capability_stage in capability_results.values()
                    if isinstance(capability_stage, Mapping)
                    and capability_stage.get("invoked") is True
                )
                if workforce_admission_payload is not None:
                    blocked_receipt["plan_payload"] = plan_payload
                    blocked_receipt["workforce_admission"] = workforce_admission_payload
                    blocked_receipt["workforce_admission_lineage"] = workforce_lineage
                blocked_receipt.update(
                    {
                        "capability_call_count": capability_call_count,
                        "verifier_call_count": 0,
                        "learning_call_count": 0,
                        "provider_call_count": 0,
                        "invocation_counts": {
                            "capability": capability_call_count,
                            "local": 0,
                            "online": 0,
                            "verifier": 0,
                            "learning": 0,
                        },
                    }
                )
                if gateway_invocation_authority is not None:
                    blocked_receipt["gateway_invocation_authority"] = gateway_invocation_authority
                if local_model_invocation_authority is not None:
                    blocked_receipt["local_model_invocation_authority"] = local_model_invocation_authority
                if receipt_path is not None:
                    path = Path(receipt_path)
                    blocked_receipt["receipt_path"] = str(path)
                    with _runtime_write_context(runtime_writer_factory, task_id=request.task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                        _assert_runtime_context_active(runtime_writer_factory, write_context)
                        _assert_runtime_receipt_owner(write_context, path)
                        _write_receipt_atomic(path, blocked_receipt)
                return attach_failure_diagnostics(blocked_receipt)

            # Full read-only consumer view — same root bundle_hash for Local and Online.
            # seal_verify is never written into the sealed body.
            evidence_consumer = _evidence_consumer_view(evidence_bundle)
            plan_payload["capability_evidence_bundle"] = evidence_consumer
            # LocalAssist planner_snapshot reads signal_snapshot — mirror full bundle there.
            snap = dict(plan_payload.get("signal_snapshot") or {})
            snap["capability_evidence_bundle"] = evidence_consumer
            snap["baseline_hash"] = evidence_bundle["baseline_hash"]
            snap["bundle_hash"] = evidence_bundle["bundle_hash"]
            snap["planner_decision_id"] = planner_decision_id
            # Local-consumable selected set from Planner (not hard-coded local_model_executor).
            local_consumable = [
                n for n in plan.selected_capabilities
                if n == "local_model_executor" or n in {
                    "memory", "codeintel", "belief", "semantic_searcher", "lancedb",
                    "repair_loop", "sandbox",
                }
            ]
            if "local_model_executor" in plan.selected_capabilities and "local_model_executor" not in local_consumable:
                local_consumable.append("local_model_executor")
            # Always include planner-selected local_model_executor when present; otherwise
            # pass the planner-selected intersection that Local can consume.
            if not local_consumable:
                local_consumable = [n for n in plan.selected_capabilities if n == "local_model_executor"]
            snap["selected_capabilities"] = list(plan.selected_capabilities)
            snap["local_consumable_capabilities"] = list(local_consumable)
            plan_payload["signal_snapshot"] = snap
            plan_payload["local_consumable_capabilities"] = list(local_consumable)
            stages["shared_capability_evidence"] = _stage(
                "shared_capability_evidence",
                status="SUCCEEDED",
                invoked=True,
                evidence_present=True,
                gate_passed=True,
                evidence_refs=[f"runtime:evidence_bundle:{evidence_bundle['bundle_hash'][:16]}"],
                baseline_hash=evidence_bundle["baseline_hash"],
                bundle_hash=evidence_bundle["bundle_hash"],
                real_success_count=evidence_bundle["summary"]["real_success_count"],
                stub_invoked=list(evidence_bundle["summary"]["stub_invoked"]),
            )

            local_stage = self._run_local(
                request,
                plan_payload,
                workforce_admission_required=workforce_admission_required,
                effect_journal=effect_journal,
                effect_dispatch=effect_dispatch,
                effect_reconcile=effect_reconcile,
                planner_decision_id=planner_decision_id,
                attempt_number=attempt_number,
                effect_journal_bindings=effect_journal_bindings,
                runtime_writer_factory=runtime_writer_factory,
                owner_context=owner_context,
            )
            if request.local_enabled:
                stages["local"] = local_stage
            else:
                stages["local"] = _stage("local", status="NOT_REQUESTED", reason="local_route_disabled")
            capability_context["local"] = local_stage
            capability_context["capability_evidence_bundle"] = evidence_bundle
            capability_context["baseline_hash"] = evidence_bundle["baseline_hash"]
            if "repair_loop" in plan.selected_capabilities:
                repair_result = _repair_loop_result_from_local_stage(
                    task_id=request.task_id,
                    local_stage=local_stage,
                )
                capability_results["repair_loop"] = _capability_stage(
                    "repair_loop",
                    request.task_id,
                    repair_result,
                    delegated_to="Local",
                )

            effective_online_prompt = request.online_prompt
            capability_context_compressed = False
            compression_stage = capability_results.get("prompt_compression", {})
            compression_data = compression_stage.get("response", {}) if isinstance(compression_stage, Mapping) else {}
            compression_response = compression_data.get("response", {}) if isinstance(compression_data, Mapping) else {}
            compressed_context = compression_response.get("compressed_context") if isinstance(compression_response, Mapping) else None
            if (
                compression_stage.get("status") == "SUCCEEDED"
                and isinstance(compressed_context, str)
                and compressed_context
            ):
                effective_online_prompt = compressed_context
                capability_context_compressed = True

            decision_from_context = _nexus_generated_bindings.decision_from_context
            resolve_online_execution_decision = _nexus_generated_bindings.resolve_online_execution_decision

            route_map = dict(request.route) if isinstance(request.route, Mapping) else {}
            prior = decision_from_context(route_map)
            if prior is None:
                task_policy = str(route_map.get("online_policy") or "").strip().lower()
                injected_flag = bool(route_map.get("injected_transport", False))
                # Fixture default: online_invoker without product policy → injected transport.
                # Product auto/require must NOT force inject (physical path needs real auth).
                if online_invoker is not None and not task_policy and not injected_flag:
                    task_policy = "auto"
                    injected_flag = not physical_online_required
                online_decision = resolve_online_execution_decision(
                    task_online_policy=task_policy,
                    project_root=str(route_map.get("workspace_root") or "."),
                    planner_online_needed=bool(request.online_enabled),
                    injected_transport=injected_flag,
                    requested_provider=str(route_map.get("provider") or ""),
                )
            else:
                online_decision = prior

            context: dict[str, Any] = {
                "schema": REQUEST_SCHEMA,
                "task_id": request.task_id,
                "workspace_revision": request.workspace_revision,
                "task_statement": request.task_statement,
                "task_type": request.task_type,
                "online_prompt": effective_online_prompt,
                "online_payload": request.online_payload,
                "online_phase": request.online_phase,
                "online_model_name": (
                    gateway_invocation_authority.get("resolved_model")
                    if isinstance(gateway_invocation_authority, Mapping)
                    and gateway_invocation_authority.get("gate_passed") is True
                    else request.online_model_name
                ),
                "online_enabled": request.online_enabled,
                "online_output_schema": dict(request.online_output_schema or {}),
                "planner": plan_payload,
                # with_nexus Online armor (World B) reads route + codeintel from context.
                # No new topology/RouteMode — existing request fields only.
                "route": dict(request.route) if isinstance(request.route, Mapping) else {},
                "codeintel": dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {},
                "pillars": dict(request.pillars) if isinstance(request.pillars, Mapping) else {},
                "local": local_stage,
                "capability_results": capability_results,
                "capability_context_compressed": capability_context_compressed,
                # P2: same immutable baseline for Online as Local saw after preflight.
                "capability_evidence_bundle": evidence_bundle,
                "baseline_hash": evidence_bundle["baseline_hash"],
                "planner_decision_id": planner_decision_id,
                "online_execution_decision": online_decision.to_dict(),
                "online_policy": online_decision.online_policy,
                "online_execution_requested": online_decision.online_execution_requested,
                "online_execution_authorized": online_decision.online_execution_authorized,
                "online_authorization_source": online_decision.online_authorization_source,
                "online_preflight_status": online_decision.preflight_status,
                "approved_online_providers": list(online_decision.approved_online_providers),
            }
            if canonical_execution is not None:
                context["canonical_execution"] = canonical_execution
            if gateway_invocation_authority is not None:
                context["gateway_invocation_authority"] = gateway_invocation_authority
            if local_model_invocation_authority is not None:
                context["local_model_invocation_authority"] = local_model_invocation_authority
            if effect_journal is not None and online_invoker is not None:
                original_online_invoker = online_invoker
                def _journaled_online(context: Mapping[str, Any]) -> Mapping[str, Any]:
                    import hashlib as _effect_hash
                    import json as _effect_json
                    identity = {
                        "task_id": request.task_id, "workspace_revision": request.workspace_revision,
                        "planner_decision_id": planner_decision_id, "attempt_number": attempt_number,
                        "action": "online_invocation", "subject_revision": request.workspace_revision,
                        "request_digest": _effect_hash.sha256(_effect_json.dumps(dict(context), sort_keys=True, default=str).encode()).hexdigest(),
                    }
                    req_digest = str(identity["request_digest"])
                    def dispatch():
                        return effect_dispatch.dispatch(lambda: original_online_invoker(context))
                    deterministic_effect_id = _nexus_generated_bindings.deterministic_effect_id
                    operation_digest = _nexus_generated_bindings.operation_digest
                    effect_journal_bindings.append({"effect_id": deterministic_effect_id(identity), "operation_id": operation_digest(identity), "action": identity["action"], "request_digest": req_digest, "project_root": str(effect_journal.project_root)})
                    with _runtime_write_context(runtime_writer_factory, task_id=request.task_id, role="effect_journal", path=effect_journal.path, owner_context=owner_context) as effect_context:
                        _assert_runtime_context_active(runtime_writer_factory, effect_context)
                        result = effect_journal.execute(identity=identity, subject=request.task_id, request_digest=req_digest, dispatch=dispatch, reconcile=lambda record: effect_reconcile.reconcile(record))
                    if effect_journal_bindings:
                        saved = effect_journal.get(deterministic_effect_id(identity)) or {}
                        effect_journal_bindings[-1].update({"generation": saved.get("generation"), "state": saved.get("state"), "result_digest": saved.get("result_digest", ""), "project_root": str(effect_journal.project_root)})
                    return result if isinstance(result, Mapping) else {"task_id": request.task_id, "invoked": True, "output_delivered": bool(result), "gate_passed": bool(result), "response": result}
                for attr in ("provider", "online_invoker_provider", "physical_provider_transport"):
                    if hasattr(original_online_invoker, attr): setattr(_journaled_online, attr, getattr(original_online_invoker, attr))
                online_invoker = _journaled_online
            online_stage = self._run_online(
                request,
                online_invoker,
                context,
                workforce_admission_required=workforce_admission_required,
            )
            if request.online_enabled:
                stages["online"] = online_stage
            else:
                stages["online"] = _stage("online", status="NOT_REQUESTED", reason="online_route_disabled")
            context["online"] = online_stage

            verifier_stage = self._run_callback("verifier", verifier, context, required=True)
            stages["verifier"] = verifier_stage
            context["verifier"] = verifier_stage

            replan_request = build_execution_replan_request(
                task_id=request.task_id,
                planner_decision_id=planner_decision_id,
                current_execution_depth=plan.execution_depth,
                verifier_stage=verifier_stage,
            )
            context["execution_replan_request"] = replan_request
            context["execution_attempt"] = execution_attempt
            if replan_authorization is not None:
                context["replan_authorization"] = replan_authorization.to_dict()
                context["source_replan_request_id"] = replan_authorization.source_replan_request_id
                context["parent_receipt_hash"] = replan_authorization.source_receipt_hash



            # Postflight gates after Online + verifier so proof fields are visible.
            # Online reply alone never satisfies artifact/claim/delivery.
            postflight_context = dict(context)
            postflight_context["route"] = dict(request.route)
            postflight_context["capability_results"] = capability_results
            postflight_context["capability_evidence_bundle"] = evidence_bundle
            postflight_context["source_hash"] = str(evidence_bundle.get("source_hash") or "")
            postflight_context["task_statement"] = request.task_statement
            for capability_name, invoker in postflight_items:
                _invoke_capability(capability_name, invoker, postflight_context)
                # Keep context capability_results live for subsequent stages.
                context["capability_results"] = capability_results

            learning_stage = self._run_callback("learning", learning, context, required=True)
            stages["learning"] = learning_stage
            context["learning"] = learning_stage

            required_stage_names = ["planner"]
            if request.local_enabled:
                required_stage_names.append("local")
            if request.online_enabled:
                required_stage_names.append("online")
            required_stage_names.extend(("verifier", "learning"))
            required_stages = [stages[name] for name in required_stage_names]
            # Final Gate: only planner.required_capabilities gate receipt_complete.
            # Optional selected caps may fail without blocking SUCCEEDED.
            # SKIPPED (policy) is coverage-ok; stub / SELECTED_NOT_EXECUTED on required must block.
            required_cap_names = {str(n) for n in (plan.required_capabilities or [])}
            for cap_name, cap_stage in capability_results.items():
                if str(cap_name) not in required_cap_names:
                    continue
                if str(cap_stage.get("status") or "") == "SKIPPED" or cap_stage.get("skipped"):
                    continue
                required_stages.append(cap_stage)

            def _stage_complete(stage: object) -> bool:
                if not isinstance(stage, dict):
                    return False
                if bool(stage.get("stub")) or (
                    isinstance(stage.get("response"), dict) and stage["response"].get("stub")
                ):
                    return False
                if str(stage.get("status") or "") in {"SELECTED_NOT_EXECUTED", "STUB_INVOKED"}:
                    return False
                return bool(stage.get("invoked") and stage.get("evidence_present") and stage.get("gate_passed"))

            receipt_complete = all(_stage_complete(stage) for stage in required_stages)
            outcome_contributed = any(bool(stage.get("outcome_contributed")) for stage in required_stages)
            # P4: full wiring closure — every selected capability must truly succeed.
            # SKIPPED / SELECTED_NOT_EXECUTED / STUB / FAILED / BLOCKED / invoked=false
            # / gate_passed=false all block capability_closure_complete.
            # receipt_complete may still be true when only required stages succeed.
            selected_names = [str(n) for n in plan.selected_capabilities]
            executed_capabilities: list[str] = []
            contributed_capabilities: list[str] = []
            closure_blockers: list[str] = []
            closure_skipped_count = 0
            # Materialize local-stage owned capabilities into the closure stage map.
            closure_stages: dict[str, Any] = dict(capability_results)
            local_action_for_closure = _local_action_from_request(request.local_request, local_stage)
            local_phys_for_closure = str(
                (local_stage.get("response") or {}).get("physical_callable", "")
                if isinstance(local_stage.get("response"), Mapping)
                else ""
            )
            for target_local_cap in ("local_model_executor", "repair_loop"):
                if target_local_cap in selected_names:
                    executor_proven = _local_executor_invoked_proven(
                        action=local_action_for_closure,
                        physical_callable=local_phys_for_closure,
                        local_stage=local_stage,
                    )
                    if not request.local_enabled:
                        closure_stages[target_local_cap] = {
                            "status": "SKIPPED",
                            "skipped": True,
                            "invoked": False,
                            "gate_passed": False,
                            "evidence_present": False,
                            "skip_reason": "local_route_disabled",
                            "reason": "local_route_disabled",
                        }
                    elif local_action_for_closure == "advisor":
                        # Advisor uses Provider.generate — selected executor was not executed.
                        closure_stages[target_local_cap] = {
                            "status": "SKIPPED",
                            "skipped": True,
                            "invoked": False,
                            "gate_passed": False,
                            "evidence_present": False,
                            "skip_reason": "selected_executor_not_invoked_advisor_path",
                            "reason": "selected_executor_not_invoked_advisor_path",
                        }
                    elif (executor_proven or bool(local_stage.get("status") in {"SUCCEEDED", "PASS", "COMPLETED"})) and bool(local_stage.get("invoked")) and bool(local_stage.get("gate_passed")):
                        closure_stages[target_local_cap] = {
                            "status": "SUCCEEDED",
                            "skipped": False,
                            "invoked": True,
                            "gate_passed": True,
                            "evidence_present": bool(
                                local_stage.get("evidence_present")
                                or local_stage.get("evidence_refs")
                                or (isinstance(local_stage.get("response"), Mapping)
                                    and local_stage["response"].get("evidence_refs"))
                            ),
                            "outcome_contributed": bool(local_stage.get("outcome_contributed")),
                            "reason": "",
                        }
                    elif bool(local_stage.get("invoked")):
                        closure_stages[target_local_cap] = {
                            "status": "SELECTED_NOT_EXECUTED",
                            "skipped": False,
                            "invoked": False,
                            "gate_passed": False,
                            "evidence_present": False,
                            "reason": "planner_selected_no_runtime_executor",
                        }
                    else:
                        prior = capability_results.get(target_local_cap) or {}
                        if prior.get("skipped") or str(prior.get("status") or "") == "SKIPPED":
                            closure_stages[target_local_cap] = {
                                "status": "SKIPPED",
                                "skipped": True,
                                "invoked": False,
                                "gate_passed": False,
                                "evidence_present": False,
                                "skip_reason": str(
                                    prior.get("skip_reason") or prior.get("reason") or "delegated_to_local_stage"
                                ),
                                "reason": str(
                                    prior.get("skip_reason") or prior.get("reason") or "delegated_to_local_stage"
                                ),
                            }
                        else:
                            closure_stages[target_local_cap] = {
                                "status": "SELECTED_NOT_EXECUTED",
                                "skipped": False,
                                "invoked": False,
                                "gate_passed": False,
                                "evidence_present": False,
                                "reason": "planner_selected_no_runtime_executor",
                            }

            _terminal_statuses = {
                "SKIPPED",
                "SELECTED_NOT_EXECUTED",
                "STUB_INVOKED",
                "FAILED",
                "BLOCKED",
            }
            for cap_name in selected_names:
                cap_stage = closure_stages.get(cap_name) or {}
                resp = cap_stage.get("response") if isinstance(cap_stage.get("response"), Mapping) else {}
                stage_status = str(cap_stage.get("status") or "")
                resp_status = str(resp.get("status") or "") if isinstance(resp, Mapping) else ""
                resp_status_u = resp_status.upper()
                # Prefer invoker-declared terminal status over normalized stage label.
                if resp_status_u in _terminal_statuses:
                    status = resp_status_u
                else:
                    status = stage_status.upper() if stage_status.upper() in _terminal_statuses else stage_status
                reason = str(
                    cap_stage.get("skip_reason")
                    or cap_stage.get("reason")
                    or (resp.get("skip_reason") if isinstance(resp, Mapping) else "")
                    or (resp.get("reason") if isinstance(resp, Mapping) else "")
                    or ""
                )
                skipped_flag = bool(cap_stage.get("skipped")) or status == "SKIPPED"
                if not skipped_flag and isinstance(resp, Mapping):
                    skipped_flag = bool(resp.get("skipped")) or str(resp.get("status") or "").upper() == "SKIPPED"
                stub_flag = bool(cap_stage.get("stub")) or (
                    isinstance(resp, Mapping) and bool(resp.get("stub"))
                )

                if skipped_flag or status == "SKIPPED":
                    closure_skipped_count += 1
                    closure_blockers.append(
                        f"{cap_name}:SKIPPED:{reason or 'skipped'}"
                    )
                    continue
                if status in _terminal_statuses or stub_flag:
                    label = status or ("STUB_INVOKED" if stub_flag else "incomplete")
                    closure_blockers.append(
                        f"{cap_name}:{label}:{reason or label}"
                    )
                    continue
                if not bool(cap_stage.get("invoked")):
                    closure_blockers.append(
                        f"{cap_name}:{status or 'SELECTED_NOT_EXECUTED'}:invoked=false"
                    )
                    continue
                if not bool(cap_stage.get("gate_passed")):
                    closure_blockers.append(
                        f"{cap_name}:{status or 'FAILED'}:gate_passed=false"
                    )
                    continue
                if not _stage_complete(cap_stage):
                    closure_blockers.append(
                        f"{cap_name}:{status or 'incomplete'}:{reason or 'incomplete'}"
                    )
                    continue
                executed_capabilities.append(cap_name)
                if bool(cap_stage.get("outcome_contributed")):
                    contributed_capabilities.append(cap_name)
            capability_closure_complete = not closure_blockers and bool(selected_names)
            closure_selected_count = len(selected_names)
            closure_executed_count = len(executed_capabilities)
            # Real consumed evidence IDs from Local/Online consumer records only.
            # Prefer IDs actually injected into Local context / with_nexus prompt lineage.
            consumed_evidence_ids: list[str] = []

            def _append_consumed(ids: object) -> None:
                if not isinstance(ids, (list, tuple)):
                    return
                for eid in ids:
                    s = str(eid).strip()
                    if s and not s.startswith("bundle:") and s not in consumed_evidence_ids:
                        consumed_evidence_ids.append(s)

            local_resp = (
                local_stage.get("response")
                if isinstance(local_stage.get("response"), Mapping)
                else {}
            )
            if isinstance(local_resp, Mapping):
                _append_consumed(local_resp.get("consumed_evidence_ids"))
                ec = local_resp.get("evidence_consumption")
                if isinstance(ec, Mapping):
                    _append_consumed(ec.get("consumed_evidence_ids"))
                # LocalAssistResponse nests consumption under local_outputs.
                local_outputs = local_resp.get("local_outputs")
                if isinstance(local_outputs, Mapping):
                    _append_consumed(local_outputs.get("consumed_evidence_ids"))
                    ec_nested = local_outputs.get("evidence_consumption")
                    if isinstance(ec_nested, Mapping):
                        _append_consumed(ec_nested.get("consumed_evidence_ids"))

            online_resp = (
                online_stage.get("response")
                if isinstance(online_stage.get("response"), Mapping)
                else {}
            )
            if isinstance(online_resp, Mapping):
                _append_consumed(online_resp.get("consumed_evidence_ids"))
                # with_nexus payload: IDs live under lineage (actually injected into prompt)
                with_nexus = online_resp.get("with_nexus")
                if isinstance(with_nexus, Mapping):
                    _append_consumed(with_nexus.get("consumed_evidence_ids"))
                    lineage = with_nexus.get("lineage")
                    if isinstance(lineage, Mapping):
                        _append_consumed(lineage.get("consumed_evidence_ids"))
                # Top-level lineage alias some invokers attach
                lineage_top = online_resp.get("lineage")
                if isinstance(lineage_top, Mapping):
                    _append_consumed(lineage_top.get("consumed_evidence_ids"))

            # Traceability: keep only IDs that exist on successful bundle entries.
            bundle_success_ids: set[str] = set()
            for ent in evidence_bundle.get("entries") or []:
                if not isinstance(ent, Mapping):
                    continue
                if not bool(ent.get("success") or ent.get("invoked_real")):
                    continue
                for eid in list(ent.get("evidence_ids") or []) + list(ent.get("evidence_refs") or []):
                    s = str(eid).strip()
                    if s:
                        bundle_success_ids.add(s)
            for eid in evidence_bundle.get("evidence_ids") or []:
                s = str(eid).strip()
                if s:
                    bundle_success_ids.add(s)
            if bundle_success_ids:
                consumed_evidence_ids = [i for i in consumed_evidence_ids if i in bundle_success_ids]
            evidence_refs = list(request.evidence_refs)
            for stage in stages.values():
                evidence_refs.extend(stage.get("evidence_refs", []))
            for capability_stage in capability_results.values():
                evidence_refs.extend(capability_stage.get("evidence_refs", []))
            claim_boundary = {
                "task_identity_shared": True,
                "planner_shared": planner_stage["invoked"],
                "local_online_continuation": bool(request.local_enabled and request.online_enabled and local_stage["invoked"] and online_stage["invoked"]),
                "receipt_complete": receipt_complete,
                "capability_closure_complete": capability_closure_complete,
                "outcome_contributed": outcome_contributed,
                "value_measured": False,
                "public_claim_allowed": False,
                "replan_required": replan_request["replan_required"],
                "requested_execution_depth": replan_request["requested_execution_depth"],
                "attempt_number": execution_attempt["attempt_number"],
                "max_attempts": execution_attempt["max_attempts"],
                "replan_attempt": execution_attempt["is_replan"],
                "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
                "source_replan_request_id": execution_attempt["source_replan_request_id"],
            }

            capability_receipts: list[dict[str, Any]] = []
            online_capabilities = set()
            if isinstance(request.route, Mapping):
                online_capabilities = {
                    str(name)
                    for name in request.route.get("online_capabilities", ()) or ()
                }
            local_action = _local_action_from_request(request.local_request, local_stage)
            local_physical_callable = str(
                (local_stage.get("response") or {}).get("physical_callable", "")
                if isinstance(local_stage.get("response"), Mapping)
                else ""
            )
            for name in plan.selected_capabilities:
                skip_reason = ""
                if name == "local_model_executor":
                    delegated_to = "Local"
                    stage = local_stage
                    stage_name = "local"
                    # Advisor uses LocalModelProvider.generate — not the executor.
                    # Only candidate/verified-subtask with executor proof may be INVOKED.
                    executor_proven = _local_executor_invoked_proven(
                        action=local_action,
                        physical_callable=local_physical_callable,
                        local_stage=local_stage,
                    )
                    if not request.local_enabled:
                        invoked = False
                        cap_status = "SKIPPED"
                        cap_reason = "local_route_disabled"
                        skip_reason = "local_route_disabled"
                    elif local_action == "advisor":
                        invoked = False
                        cap_status = "SKIPPED"
                        cap_reason = "selected_executor_not_invoked_advisor_path"
                        skip_reason = "selected_executor_not_invoked_advisor_path"
                    elif executor_proven:
                        invoked = True
                        cap_status = "INVOKED"
                        cap_reason = ""
                    elif bool(stage.get("invoked", False)) and local_action in {"candidate", "verified-subtask"}:
                        # Stage ran but lacked full executor proof → fail closed on identity.
                        invoked = False
                        cap_status = "SELECTED_NOT_EXECUTED"
                        if "Provider" in local_physical_callable and "Executor" not in local_physical_callable:
                            cap_reason = "selected_executor_not_invoked_advisor_path"
                        else:
                            cap_reason = "planner_selected_no_runtime_executor"
                    else:
                        invoked = False
                        # Prefer explicit skip row from registry if preflight skip was recorded.
                        if name in capability_results and capability_results[name].get("skipped"):
                            stage = capability_results[name]
                            stage_name = f"capability:{name}"
                            cap_status = "SKIPPED"
                            cap_reason = str(
                                capability_results[name].get("skip_reason")
                                or capability_results[name].get("reason")
                                or "delegated_to_local_stage"
                            )
                            skip_reason = cap_reason
                        else:
                            cap_status = "SELECTED_NOT_EXECUTED"
                            cap_reason = "planner_selected_no_runtime_executor"
                elif name in capability_results:
                    delegated_to = str(capability_results[name].get("delegated_to", "Local"))
                    stage = capability_results[name]
                    stage_name = f"capability:{name}"
                    stage_skipped = bool(stage.get("skipped", False)) or str(stage.get("status", "")) == "SKIPPED"
                    # Nested response may carry skip flags from registry invoker.
                    response = stage.get("response") if isinstance(stage.get("response"), Mapping) else {}
                    if not stage_skipped and isinstance(response, Mapping):
                        stage_skipped = bool(response.get("skipped", False))
                    stub_only = bool(response.get("stub", False)) if isinstance(response, Mapping) else False
                    # Route online_capabilities: attribute consumer to Online unless the
                    # invoker payload itself declared delegated_to (e.g. Local-bound memory).
                    # Note: _capability_stage defaults delegated_to=Local — that default is
                    # not treated as an explicit invoker declaration.
                    explicit_delegated = None
                    if isinstance(response, Mapping) and "delegated_to" in response:
                        explicit_delegated = str(response.get("delegated_to") or "")
                    if name in online_capabilities and (stage_skipped or stub_only):
                        delegated_to = "Online"
                        stage = online_stage
                        stage_name = "online"
                        invoked = bool(stage.get("invoked", False))
                        cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                        cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                        skip_reason = ""
                    elif name in online_capabilities and not stage_skipped and not explicit_delegated:
                        delegated_to = "Online"
                        invoked = bool(stage.get("invoked", False))
                        cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                        cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                        skip_reason = ""
                    elif name in online_capabilities and explicit_delegated:
                        delegated_to = explicit_delegated
                        invoked = bool(stage.get("invoked", False))
                        cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                        cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                        skip_reason = ""
                    elif stage_skipped:
                        invoked = False
                        cap_status = "SKIPPED"
                        cap_reason = str(
                            stage.get("skip_reason")
                            or stage.get("reason")
                            or response.get("skip_reason")
                            or "explicit_skip"
                        )
                        skip_reason = cap_reason
                    else:
                        invoked = bool(stage.get("invoked", False))
                        cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                        cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                elif name in online_capabilities:
                    delegated_to = "Online"
                    stage = online_stage
                    stage_name = "online"
                    invoked = bool(stage.get("invoked", False))
                    cap_status = "INVOKED" if invoked else "SELECTED_NOT_EXECUTED"
                    cap_reason = "" if invoked else "planner_selected_no_runtime_executor"
                else:
                    delegated_to = "PlannerOnly"
                    stage = {}
                    stage_name = ""
                    invoked = False
                    cap_status = "SKIPPED"
                    cap_reason = "caller_omitted_auto_skip"
                    skip_reason = "caller_omitted_auto_skip"
                capability_receipts.append(
                    {
                        "name": name,
                        "selected": True,
                        "selection_source": "CapabilityPlanner",
                        "delegated_to": delegated_to,
                        "stage": stage_name,
                        "invoked": invoked,
                        "skipped": cap_status == "SKIPPED",
                        "skip_reason": skip_reason if cap_status == "SKIPPED" else "",
                        "evidence_present": bool(stage.get("evidence_present", False))
                        or (cap_status == "SKIPPED" and bool(cap_reason)),
                        "gate_passed": bool(stage.get("gate_passed", False))
                        if cap_status != "SKIPPED"
                        else True,
                        "outcome_contributed": bool(stage.get("outcome_contributed", False)),
                        "evidence_refs": list(stage.get("evidence_refs", []) or [])
                        or (
                            [f"capability:{name}:{request.task_id}:skipped:{skip_reason}"]
                            if cap_status == "SKIPPED" and skip_reason
                            else []
                        ),
                        "task_id": request.task_id,
                        "planner_decision_id": planner_decision_id,
                        "status": cap_status,
                        "reason": cap_reason,
                        "physical_callable": (
                            local_physical_callable
                            if name == "local_model_executor"
                            else str(stage.get("physical_callable", "") or "")
                        ),
                    }
                )
            # FCM coverage fields on receipt (machine-checkable)
            _coverage_preview = _nexus_generated_bindings._coverage_preview

            # Temporary object for coverage helper — fill after append
            _coverage_tmp = {
                "context_trace": {"selected_capabilities": list(plan.selected_capabilities)},
                "capabilities": capability_receipts,
            }
            capability_coverage = _coverage_preview(_coverage_tmp)
            online_evidence_refs = [str(ref) for ref in online_stage.get("evidence_refs", []) or []]
            online_response = (
                online_stage.get("response")
                if isinstance(online_stage.get("response"), Mapping)
                else {}
            )
            with_nexus_lineage = (
                online_response.get("with_nexus")
                if isinstance(online_response.get("with_nexus"), Mapping)
                else {}
            )
            prompt_sections_present = [
                str(item)
                for item in (
                    online_response.get("prompt_sections_present")
                    or with_nexus_lineage.get("prompt_sections_present")
                    or []
                )
            ]
            # P1: Local VAP lineage + B/D treatment fingerprints (same plan_hash + codeintel_hash).
            local_response_map = (
                local_stage.get("response") if isinstance(local_stage.get("response"), Mapping) else {}
            )
            vap_packet = (
                local_response_map.get("verified_assist_packet")
                if isinstance(local_response_map.get("verified_assist_packet"), Mapping)
                else None
            )
            vap_packet_hash = str(
                (vap_packet or {}).get("packet_hash")
                or local_stage.get("verified_assist_packet_hash")
                or ""
            )
            online_safe_forward: dict[str, Any] = {}
            verified_assist_block: dict[str, Any] = {}
            if request.local_enabled and local_stage.get("invoked") and vap_packet:
                try:
                    build_online_safe_local_forward = _nexus_generated_bindings.build_online_safe_local_forward

                    # Prefer assembled with_nexus prompt for physical consumption binding.
                    with_nexus_prompt = ""
                    if isinstance(online_response.get("with_nexus"), Mapping):
                        with_nexus_prompt = str(online_response["with_nexus"].get("prompt") or "")
                    assembled = str(
                        online_response.get("assembled_online_prompt")
                        or with_nexus_prompt
                        or effective_online_prompt
                        or ""
                    )
                    # Re-run attach with final prompt so consumption_proof binds to Online injection.
                    forward_base = build_online_safe_local_forward(local_stage)
                    attach_verified_assist_to_forward = _nexus_generated_bindings.attach_verified_assist_to_forward

                    online_safe_forward = attach_verified_assist_to_forward(
                        forward_base,
                        vap_packet,
                        consume=bool(request.online_enabled and online_stage.get("invoked")),
                        consumed_by_stage="online_prompt_assembly",
                        final_prompt=assembled if assembled else str(
                            (forward_base.get("verified_assist") or {}).get("injection_fragment")
                            or ""
                        ),
                    )
                    verified_assist_block = dict(online_safe_forward.get("verified_assist") or {})
                    # Mark local substitution online_consumed when credit proves consumption.
                    credit = verified_assist_block.get("credit") if isinstance(verified_assist_block.get("credit"), Mapping) else {}
                    if credit.get("assist_credited") and isinstance(local_stage.get("substitution_trace"), Mapping):
                        local_stage = dict(local_stage)
                        trace = dict(local_stage.get("substitution_trace") or {})
                        trace["online_consumed"] = True
                        local_stage["substitution_trace"] = trace
                        stages["local"] = local_stage
                except Exception:
                    online_safe_forward = {}
                    verified_assist_block = {}

            codeintel_hash = _hash_json(dict(request.codeintel) if isinstance(request.codeintel, Mapping) else {})
            treatment_config = {
                "profile": "online_nexus_v1",
                "with_nexus": True,
                "plan_hash": plan_hash,
                "codeintel_hash": codeintel_hash,
                "planner_decision_id": planner_decision_id,
            }
            treatment_fingerprint_b: dict[str, Any] = {}
            treatment_fingerprint_d: dict[str, Any] = {}
            treatment_core_equal: dict[str, Any] = {}
            try:
                assert_treatment_core_equal = _nexus_generated_bindings.assert_treatment_core_equal
                build_treatment_fingerprint = _nexus_generated_bindings.build_treatment_fingerprint

                fp_b = build_treatment_fingerprint(
                    treatment_config=treatment_config,
                    assist_packet_attached=False,
                )
                fp_d = build_treatment_fingerprint(
                    treatment_config=treatment_config,
                    assist_packet_attached=bool(vap_packet_hash),
                )
                treatment_fingerprint_b = fp_b.to_dict()
                treatment_fingerprint_d = fp_d.to_dict()
                treatment_core_equal = assert_treatment_core_equal(fp_b, fp_d)
            except Exception:
                treatment_fingerprint_b = {}
                treatment_fingerprint_d = {}
                treatment_core_equal = {"equal": False, "reason": "fingerprint_unavailable"}

            context_trace = {
                "task_id": request.task_id,
                "workspace_revision": request.workspace_revision,
                "planner_decision_id": planner_decision_id,
                "execution_depth": plan.execution_depth,
                "execution_replan_request_id": replan_request["replan_request_id"],
                "execution_attempt": execution_attempt,
                "parent_receipt_hash": execution_attempt["parent_receipt_hash"],
                "source_replan_request_id": execution_attempt["source_replan_request_id"],
                "task_statement_hash": hashlib.sha256(request.task_statement.encode("utf-8")).hexdigest(),
                "online_prompt_hash": _hash_json(effective_online_prompt),
                "online_payload_hash": _hash_json(request.online_payload),
                "capability_results_hash": _hash_json(capability_results),
                "selected_capabilities": list(plan.selected_capabilities),
                "selection_authority": "CapabilityPlanner",
                "baseline_hash": evidence_bundle["baseline_hash"],
                "evidence_bundle_hash": evidence_bundle["bundle_hash"],
                "capability_context_compressed": capability_context_compressed,
                "codeintel_hash": codeintel_hash,
                "route": {
                    key: request.route.get(key)
                    for key in (
                        "mainchain_entry",
                        "mainchain_route_version",
                        "route_freeze",
                        "product_entry",
                        "with_nexus_armor",
                    )
                    if isinstance(request.route, Mapping) and key in request.route
                },
                "online_received_context": {
                    "local_context_forwarded": any("local_context_forwarded" in ref for ref in online_evidence_refs),
                    "capability_context_forwarded": any("capability_context_forwarded" in ref for ref in online_evidence_refs),
                    "compressed_context_applied": any("compressed_context_applied" in ref for ref in online_evidence_refs),
                    "with_nexus_armor": str(online_response.get("armor") or "") == "with_nexus"
                    or bool(with_nexus_lineage),
                    "prompt_sections_present": prompt_sections_present,
                    "codeintel_present": bool(with_nexus_lineage.get("codeintel_present", False)),
                    "with_nexus_plan_hash": str(with_nexus_lineage.get("plan_hash") or ""),
                    "vap_attached": bool(vap_packet_hash),
                    "vap_packet_hash": vap_packet_hash,
                    "local_forward_section": "local_forward" in prompt_sections_present
                    or bool(vap_packet_hash and request.local_enabled),
                },
            }
            if canonical_execution is not None:
                context_trace["canonical_execution"] = canonical_execution
            if gateway_invocation_authority is not None:
                context_trace["gateway_invocation_authority"] = gateway_invocation_authority
            if local_model_invocation_authority is not None:
                context_trace["local_model_invocation_authority"] = local_model_invocation_authority
            if isinstance(local_stage.get("formal_local_runtime_lineage"), Mapping):
                context_trace["formal_local_runtime_lineage"] = dict(
                    local_stage["formal_local_runtime_lineage"]
                )
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "task_id": request.task_id,
                "workspace_revision": request.workspace_revision,
                "planner_decision_id": planner_decision_id,
                "execution_depth": plan.execution_depth,
                "execution_replan_request": replan_request,
                "execution_attempt": execution_attempt,
                "task_statement_hash": hashlib.sha256(request.task_statement.encode("utf-8")).hexdigest(),


                "context_trace": context_trace,
                "planner": planner_stage,
                "capabilities": capability_receipts,
                "delegation": {
                    "planner": "Nexus",
                    "local_assist": "Local" if request.local_enabled else "NOT_REQUESTED",
                    "online_provider": "Online" if request.online_enabled else "NOT_REQUESTED",
                    "verifier": "Hybrid",
                    "learning": "Hybrid",
                },
                "local": local_stage,
                "capability_results": capability_results,
                "online": online_stage,
                "online_preflight": {
                    "status": online_decision.preflight_status,
                    "online_policy": online_decision.online_policy,
                    "online_execution_requested": online_decision.online_execution_requested,
                    "online_execution_authorized": online_decision.online_execution_authorized,
                    "online_authorization_source": online_decision.online_authorization_source,
                    "approved_online_providers": list(online_decision.approved_online_providers),
                    "reason": online_decision.reason,
                    "physical_invocation_allowed": online_decision.physical_invocation_allowed,
                },
                "verifier": verifier_stage,
                "learning": learning_stage,
                "stages": list(stages.values()),
                "evidence_refs": sorted(set(evidence_refs)),
                "receipt_complete": receipt_complete,
                "capability_closure_complete": capability_closure_complete,
                "capability_closure_blockers": closure_blockers,
                "closure_selected_count": closure_selected_count,
                "closure_executed_count": closure_executed_count,
                "closure_skipped_count": closure_skipped_count,
                "selected_capabilities": selected_names,
                "executed_capabilities": executed_capabilities,
                "consumed_evidence_ids": consumed_evidence_ids,
                "contributed_capabilities": contributed_capabilities,
                "terminal_status": "SUCCEEDED" if receipt_complete else "INCOMPLETE",
                "claim_boundary": claim_boundary,
                "capability_coverage": capability_coverage,
                "capability_evidence_bundle": evidence_bundle,
                "selection_authority": "CapabilityPlanner",
                "public_claim_allowed": False,
                "verified_assist": verified_assist_block,
                "online_safe_local_forward": {
                    "schema": online_safe_forward.get("schema", ""),
                    "forward_keys": sorted((online_safe_forward.get("forward") or {}).keys())
                    if isinstance(online_safe_forward.get("forward"), Mapping)
                    else [],
                    "public_claim_allowed": False,
                }
                if online_safe_forward
                else {},
                "treatment_fingerprint_b": treatment_fingerprint_b,
                "treatment_fingerprint_d": treatment_fingerprint_d,
                "treatment_core_equal": treatment_core_equal,
            }
            if canonical_execution is not None:
                receipt["canonical_execution"] = canonical_execution
                receipt["execution_world"] = canonical_execution["execution_world"]
                receipt["canonical_execution_topology"] = canonical_execution[
                    "canonical_execution_topology"
                ]
            if workforce_admission_payload is not None:
                context_trace["workforce_admission_lineage"] = workforce_lineage
                receipt["context_trace"] = context_trace
                receipt["workforce_admission"] = workforce_admission_payload
                receipt["workforce_admission_lineage"] = workforce_lineage
                receipt["plan_payload"] = plan_payload
            if gateway_invocation_authority is not None:
                receipt["gateway_invocation_authority"] = gateway_invocation_authority
            if local_model_invocation_authority is not None:
                receipt["local_model_invocation_authority"] = local_model_invocation_authority
            receipt["effect_journal_bindings"] = list(effect_journal_bindings)
            if receipt["effect_journal_bindings"] and any(item.get("state") != "COMPLETED" for item in receipt["effect_journal_bindings"]):
                receipt["receipt_complete"] = False
                receipt["terminal_status"] = "INCOMPLETE"
                receipt["public_claim_allowed"] = False
                receipt.setdefault("claim_boundary", {})["receipt_complete"] = False
                receipt["claim_boundary"]["public_claim_allowed"] = False
            build_root_receipt = _nexus_generated_bindings.build_root_receipt

            receipt["root_receipt"] = build_root_receipt(receipt)
            attach_failure_diagnostics(receipt)
            # RC-1: additive JSON-safe receipt_base + acyclic run_anchor hash DAG
            attach_r3_receipt_base(receipt)
            if receipt_path is not None:
                path = Path(receipt_path)
                receipt["receipt_path"] = str(path)
                with _runtime_write_context(runtime_writer_factory, task_id=request.task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                    _assert_runtime_context_active(runtime_writer_factory, write_context)
                    _assert_runtime_receipt_owner(write_context, path)
                    _write_receipt_atomic(path, receipt)
            return receipt

        def finalize_receipt(
            self,
            receipt: Mapping[str, Any],
            *,
            verifier: Mapping[str, Any],
            learning: Mapping[str, Any],
            outcome: Mapping[str, Any] | None = None,
            receipt_path: str | Path | None = None,
            effect_journal: Any = None,
            owner_context: Any = None,
            runtime_writer_factory: Any = None,
        ) -> dict[str, Any]:
            """Attach final verifier/learning evidence to an existing task receipt.

            Candidate-generation receipts intentionally stop before semantic
            completion.  This method closes that same receipt only when callers
            provide observed final-stage payloads; it never invokes a provider or
            infers success from a missing stage.
            """
            if not isinstance(receipt, Mapping) or receipt.get("schema") != RECEIPT_SCHEMA:
                raise ValueError("unsupported_receipt_schema")
            if effect_journal is not None:
                EffectJournal = _nexus_generated_bindings.EffectJournal
                if not isinstance(effect_journal, EffectJournal): raise ValueError("canonical_effect_journal_required")
            _validate_runtime_writer_entry(
                runtime_writer_factory,
                owner_context=owner_context,
                receipt_path=receipt_path or receipt.get("receipt_path"),
                effect_journal=effect_journal,
            )
            if (owner_context is not None or runtime_writer_factory is not None) and receipt.get("effect_journal_bindings") and effect_journal is None:
                raise ValueError("runtime_effect_journal_required_for_owner_receipt")
            _validate_runtime_owner_context(owner_context, receipt_path or receipt.get("receipt_path"))
            _validate_runtime_effect_owner(owner_context, effect_journal)

            finalized = dict(receipt)

            def final_stage(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
                data = _mapping(payload)
                response_task_id = str(data.get("task_id", "") or "")
                task_identity_valid = not response_task_id or response_task_id == str(finalized.get("task_id", ""))
                invoked = bool(data.get("invoked", True))
                passed = bool(
                    data.get(
                        "gate_passed",
                        str(data.get("status", "")).lower() in {"ok", "pass", "passed", "succeeded", "success"},
                    )
                )
                refs = [str(ref) for ref in data.get("evidence_refs", []) or []]
                return _stage(
                    name,
                    status="SUCCEEDED" if task_identity_valid and invoked and passed else "FAILED",
                    invoked=invoked,
                    evidence_present=bool(refs or data.get("evidence")),
                    gate_passed=task_identity_valid and passed,
                    outcome_contributed=bool(data.get("outcome_contributed", False)),
                    evidence_refs=refs,
                    task_id=str(finalized.get("task_id", "")),
                    response_task_id=response_task_id,
                    task_identity_shared=task_identity_valid,
                    reason=f"{name}_task_id_mismatch" if not task_identity_valid else "",
                    response=data,
                )

            verifier_stage = final_stage("verifier", verifier)
            learning_stage = final_stage("learning", learning)
            finalized["verifier"] = verifier_stage
            finalized["learning"] = learning_stage

            task_id = str(finalized.get("task_id", ""))
            planner_decision_id = str(finalized.get("planner_decision_id", ""))
            current_depth = str(finalized.get("execution_depth") or "LIGHT")

            replan_request = build_execution_replan_request(
                task_id=task_id,
                planner_decision_id=planner_decision_id,
                current_execution_depth=current_depth,
                verifier_stage=verifier_stage,
            )
            finalized["execution_replan_request"] = replan_request

            context_trace = dict(finalized.get("context_trace", {}) or {})
            context_trace["execution_replan_request_id"] = replan_request["replan_request_id"]
            finalized["context_trace"] = context_trace

            stages = [dict(stage) for stage in finalized.get("stages", []) if isinstance(stage, Mapping)]
            replaced: set[str] = set()
            for index, stage in enumerate(stages):
                name = str(stage.get("name", ""))
                if name == "verifier":
                    stages[index] = verifier_stage
                    replaced.add(name)
                elif name == "learning":
                    stages[index] = learning_stage
                    replaced.add(name)
            if "verifier" not in replaced:
                stages.append(verifier_stage)
            if "learning" not in replaced:
                stages.append(learning_stage)
            finalized["stages"] = stages

            required_names = ["planner"]
            for name in ("local", "online"):
                stage = finalized.get(name, {})
                if isinstance(stage, Mapping) and stage.get("status") != "NOT_REQUESTED":
                    required_names.append(name)
            required_names.extend(("verifier", "learning"))
            required_stages = [finalized.get(name, {}) for name in required_names]
            capability_results = finalized.get("capability_results", {})
            planner_stage_data = finalized.get("planner", {})
            required_cap_names: set[str] = set()
            if isinstance(planner_stage_data, Mapping):
                required_cap_names = {
                    str(n) for n in (planner_stage_data.get("required_capabilities") or [])
                }
            if isinstance(capability_results, Mapping):
                for cap_name, stage in capability_results.items():
                    if not isinstance(stage, Mapping):
                        continue
                    # Final Gate: optional selected caps do not block receipt_complete.
                    if str(cap_name) not in required_cap_names:
                        continue
                    # FCM: explicit SKIPPED is coverage-ok, not a completion blocker.
                    if str(stage.get("status") or "") == "SKIPPED" or stage.get("skipped"):
                        continue
                    required_stages.append(stage)
            receipt_complete = all(
                isinstance(stage, Mapping)
                and not bool(stage.get("stub"))
                and not (
                    isinstance(stage.get("response"), Mapping) and stage["response"].get("stub")
                )
                and str(stage.get("status") or "") not in {"SELECTED_NOT_EXECUTED", "STUB_INVOKED"}
                and bool(stage.get("invoked"))
                and bool(stage.get("evidence_present"))
                and bool(stage.get("gate_passed"))
                for stage in required_stages
            )
            outcome_contributed = any(bool(stage.get("outcome_contributed")) for stage in required_stages if isinstance(stage, Mapping))
            bindings = finalized.get("effect_journal_bindings")
            if isinstance(bindings, list) and bindings:
                if effect_journal is None:
                    receipt_complete = False
                else:
                    for item in bindings:
                        if not isinstance(item, Mapping) or item.get("state") != "COMPLETED":
                            receipt_complete = False; break
                        record = effect_journal.get(str(item.get("effect_id") or ""))
                        if not isinstance(record, Mapping):
                            receipt_complete = False; break
                        for key in ("operation_id", "effect_id", "request_digest", "generation", "state", "result_digest", "project_root"):
                            if record.get(key) != item.get(key):
                                receipt_complete = False; break
                        if not receipt_complete:
                            break
            evidence_refs: set[str] = set(str(ref) for ref in finalized.get("evidence_refs", []) or [])
            for stage in stages:
                evidence_refs.update(str(ref) for ref in stage.get("evidence_refs", []) or [])
            if isinstance(capability_results, Mapping):
                for stage in capability_results.values():
                    if isinstance(stage, Mapping):
                        evidence_refs.update(str(ref) for ref in stage.get("evidence_refs", []) or [])
            finalized["evidence_refs"] = sorted(evidence_refs)
            finalized["receipt_complete"] = receipt_complete
            finalized["terminal_status"] = "SUCCEEDED" if receipt_complete else "INCOMPLETE"
            claim_boundary = dict(finalized.get("claim_boundary", {}) or {})
            claim_boundary.update(
                {
                    "receipt_complete": receipt_complete,
                    "outcome_contributed": outcome_contributed,
                    "value_measured": bool(outcome and outcome.get("value_measured", outcome.get("score") is not None)),
                    "public_claim_allowed": False,
                    "finalized": True,
                    "replan_required": replan_request["replan_required"],
                    "requested_execution_depth": replan_request["requested_execution_depth"],
                }
            )

            finalized["claim_boundary"] = claim_boundary
            finalized["finalization"] = {"verifier": "observed_payload", "learning": "observed_payload"}
            build_root_receipt = _nexus_generated_bindings.build_root_receipt

            finalized["root_receipt"] = build_root_receipt(finalized)
            attach_failure_diagnostics(finalized)
            # RC-1: recompute receipt_base after finalization mutations (still acyclic)
            attach_r3_receipt_base(finalized)

            target = receipt_path or finalized.get("receipt_path")
            if target is not None:
                path = Path(target)
                finalized["receipt_path"] = str(path)
                with _runtime_write_context(runtime_writer_factory, task_id=task_id, role="runtime_receipt", path=path, owner_context=owner_context) as write_context:
                    _assert_runtime_context_active(runtime_writer_factory, write_context)
                    _assert_runtime_receipt_owner(write_context, path)
                    _write_receipt_atomic(path, finalized)
            return finalized

        def _run_local(
            self,
            request: UnifiedRuntimeRequest,
            plan: Mapping[str, Any],
            *,
            workforce_admission_required: bool | None = None,
            effect_journal: Any = None,
            effect_dispatch: Any = None,
            effect_reconcile: Callable[[Mapping[str, Any]], Any] | None = None,
            planner_decision_id: str = "",
            attempt_number: int = 1,
            effect_journal_bindings: list[dict[str, Any]] | None = None,
            runtime_writer_factory: Any = None,
            owner_context: Any = None,
        ) -> dict[str, Any]:
            if not request.local_enabled:
                return _stage("local", status="NOT_REQUESTED", reason="local_route_disabled")
            if workforce_admission_required is None:
                workforce_admission_required = _workforce_admission_required(
                    request,
                    physical_local=_physical_local_service(self._local_service),
                )
            workforce_admission_enabled = workforce_admission_required
            local_authority: Mapping[str, Any] | None = None
            if workforce_admission_enabled:
                candidate_authority = plan.get("local_model_invocation_authority")
                if isinstance(candidate_authority, Mapping):
                    local_authority = candidate_authority
                else:
                    local_authority = {
                        "schema": LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA,
                        "status": "BLOCKED",
                        "gate_passed": False,
                        "failure_reason": "workforce_admission_local_authority_missing",
                        "demand_id": "",
                        "resolved_worker_id": "",
                        "resolved_provider": "",
                        "resolved_model": "",
                        "policy_hash": "",
                        "binding_hash": "",
                        "aggregate_binding_hash": "",
                        "admission_record_decision": "",
                    }
                if local_authority.get("gate_passed") is not True:
                    return _local_authority_failure_stage(
                        task_id=request.task_id,
                        authority=local_authority,
                    )
            local_context_trace: dict[str, Any] = {}
            canonical_execution = plan.get("canonical_execution")
            if isinstance(canonical_execution, Mapping):
                local_context_trace["canonical_execution"] = dict(canonical_execution)
            local_authority_stage_fields: dict[str, Any] = {}
            if local_context_trace:
                local_authority_stage_fields["context_trace"] = local_context_trace
            if local_authority is not None:
                local_context_trace["local_model_invocation_authority"] = dict(local_authority)
                local_authority_stage_fields["context_trace"] = local_context_trace
                local_authority_stage_fields["local_model_invocation_authority"] = dict(local_authority)
            selected = set(plan.get("selected_capabilities", []) or [])
            if "local_model_executor" not in selected:
                return _stage(
                    "local",
                    status="BLOCKED",
                    reason="local_capability_not_selected",
                    **local_authority_stage_fields,
                )
            local_request = request.local_request
            shared_snapshot = dict(plan.get("signal_snapshot", {}) or {})
            # Legacy Local Assist may overlay identity and normalize a bare :7b.
            # Workforce admission takes the exact admitted identity instead.
            orig_snapshot: dict[str, Any] = {}
            if isinstance(local_request, Mapping):
                raw_snap = local_request.get("planner_snapshot")
                if isinstance(raw_snap, Mapping):
                    orig_snapshot = dict(raw_snap)
            elif hasattr(local_request, "planner_snapshot"):
                raw_snap = getattr(local_request, "planner_snapshot", None)
                if isinstance(raw_snap, Mapping):
                    orig_snapshot = dict(raw_snap)
            if workforce_admission_enabled and local_authority is not None:
                for key in (
                    "execution_topology",
                    "protocol_mode",
                    "model_call_allowed",
                ):
                    value = orig_snapshot.get(key)
                    if value not in (None, "", "unknown"):
                        shared_snapshot[key] = value
                shared_snapshot["executor_provider"] = str(local_authority["resolved_provider"])
                shared_snapshot["executor_model"] = str(local_authority["resolved_model"])
                # Workforce admission is the sole authority for the physical Local
                # model edge.  Keep delegated retry candidate discovery bounded to
                # that exact admitted identity; a Planner/default/request list must
                # never widen the authority-bound provider guard's input set.
                shared_snapshot["delegated_retry_candidate_models"] = [
                    str(local_authority["resolved_model"])
                ]
                shared_snapshot["route_truth_source"] = "CapabilityPlanner"
                shared_snapshot["local_model_invocation_authority"] = dict(local_authority)
            else:
                for key in (
                    "executor_model",
                    "executor_provider",
                    "model_call_allowed",
                    "execution_topology",
                    "protocol_mode",
                    "route_truth_source",
                ):
                    if key in orig_snapshot and orig_snapshot[key] not in (None, "", "unknown"):
                        shared_snapshot[key] = orig_snapshot[key]
                model = str(shared_snapshot.get("executor_model") or "").strip()
                if model.endswith(":7b") and "instruct" not in model:
                    shared_snapshot["executor_model"] = "qwen2.5-coder:7b-instruct"
            if isinstance(local_request, Mapping):
                local_request = dict(local_request)
                local_request["planner_snapshot"] = shared_snapshot
            elif hasattr(local_request, "planner_snapshot"):
                try:
                    local_request = replace(local_request, planner_snapshot=shared_snapshot)
                except TypeError:
                    return _stage("local", status="BLOCKED", reason="local_request_not_replaceable")
            local_payload = _mapping(local_request)
            local_task_id = str(local_payload.get("task_id") or getattr(local_request, "task_id", ""))
            if local_task_id != request.task_id:
                return _stage(
                    "local",
                    status="BLOCKED",
                    reason="local_task_id_mismatch",
                    **local_authority_stage_fields,
                )
            if self._local_service is None:
                return _stage(
                    "local",
                    status="NOT_RUN",
                    reason="local_service_not_supplied",
                    **local_authority_stage_fields,
                )
            try:
                if effect_journal is not None:
                    import hashlib as _effect_hash
                    import json as _effect_json
                    local_context = {"task_id": request.task_id, "workspace_revision": request.workspace_revision, "planner_decision_id": planner_decision_id, "attempt_number": attempt_number, "action": "local_model_invocation", "subject_revision": request.workspace_revision, "local_request": local_request}
                    request_digest = _effect_hash.sha256(_effect_json.dumps(local_context, sort_keys=True, default=str).encode()).hexdigest()
                    local_identity = {key: value for key, value in local_context.items() if key != "local_request"}
                    local_identity["request_digest"] = request_digest
                    deterministic_effect_id = _nexus_generated_bindings.deterministic_effect_id
                    operation_digest = _nexus_generated_bindings.operation_digest
                    def _local_dispatch():
                        if hasattr(self._local_service, "handle"):
                            return self._local_service.handle(local_request)
                        if callable(self._local_service):
                            return self._local_service(local_request)
                        raise TypeError("local_service_not_callable")
                    with _runtime_write_context(runtime_writer_factory, task_id=request.task_id, role="effect_journal", path=effect_journal.path, owner_context=owner_context) as effect_context:
                        _assert_runtime_context_active(runtime_writer_factory, effect_context)
                        response = effect_journal.execute(identity=local_identity, subject=request.task_id, request_digest=request_digest, dispatch=lambda: effect_dispatch.dispatch(_local_dispatch), reconcile=lambda record: effect_reconcile.reconcile(record))
                    deterministic_effect_id = _nexus_generated_bindings.deterministic_effect_id
                    operation_digest = _nexus_generated_bindings.operation_digest
                    binding = {"effect_id": deterministic_effect_id(local_identity), "operation_id": operation_digest(local_identity), "action": local_identity["action"], "request_digest": request_digest}
                    saved = effect_journal.get(binding["effect_id"]) or {}
                    binding.update({"generation": saved.get("generation"), "state": saved.get("state"), "result_digest": saved.get("result_digest", ""), "project_root": str(effect_journal.project_root)})
                    if effect_journal_bindings is not None:
                        effect_journal_bindings.append(binding)
                elif hasattr(self._local_service, "handle"):
                    response = self._local_service.handle(local_request)
                elif callable(self._local_service):
                    response = self._local_service(local_request)
                else:
                    return _stage(
                        "local",
                        status="BLOCKED",
                        reason="local_service_not_callable",
                        **local_authority_stage_fields,
                    )
            except Exception as exc:
                return _stage(
                    "local",
                    status="FAILED",
                    reason=f"local_exception:{exc}",
                    **local_authority_stage_fields,
                )
            payload = _mapping(response)
            formal_lineage: dict[str, Any] = {}
            if workforce_admission_enabled:
                if local_authority is not None and local_authority.get("mutation_intent") is False:
                    formal_lineage = _formal_local_advisor_lineage(payload)
                else:
                    formal_lineage = _formal_local_runtime_lineage(payload)
            if formal_lineage and formal_lineage.get("gate_passed") is not True:
                lineage_fields = {
                    "formal_local_runtime_lineage": formal_lineage,
                    "context_trace": {
                        **dict(local_authority_stage_fields.get("context_trace") or {}),
                        "formal_local_runtime_lineage": formal_lineage,
                    },
                }
                if local_authority is not None:
                    lineage_fields["local_model_invocation_authority"] = dict(local_authority)
                return _stage(
                    "local",
                    status="FAILED",
                    invoked=False,
                    evidence_present=True,
                    gate_passed=False,
                    evidence_refs=[f"local:{request.task_id}:formal_runtime:{formal_lineage['failure_reason']}"],
                    reason=str(formal_lineage["failure_reason"]),
                    response=payload,
                    provider_call_count=int(payload.get("provider_call_count") or 0),
                    model_call_count=int(payload.get("model_call_count") or 0),
                    **lineage_fields,
                )
            if formal_lineage:
                local_authority_stage_fields = {
                    **local_authority_stage_fields,
                    "context_trace": {
                        **dict(local_authority_stage_fields.get("context_trace") or {}),
                        "formal_local_runtime_lineage": formal_lineage,
                    },
                }
            response_task_id = str(payload.get("task_id", "") or "")
            task_identity_valid = not response_task_id or response_task_id == request.task_id
            invoked = bool(payload.get("local_model_invoked", payload.get("invoked", False)))
            delivered = bool(payload.get("output_delivered", False))
            refs = [str(ref) for ref in payload.get("evidence_refs", []) or []]
            verifier_payload = payload.get("verifier_summary")
            local_verifier_status = ""
            if isinstance(verifier_payload, Mapping):
                local_verifier_status = str(verifier_payload.get("verifier_status", "") or "").lower()
            # Verified-subtask: verifier fail is not partial success.
            action = str(payload.get("action") or "")
            if action == "verified-subtask" and local_verifier_status not in {"", "not_run", "pass", "passed"}:
                delivered = False
            local_boundary_passed = task_identity_valid and invoked and delivered and (
                local_verifier_status in {"", "not_run", "pass", "passed"}
            )
            # Attach substitution stage bits when Local assist provides them.
            stage_bits = {}
            if isinstance(payload.get("verified_artifact"), Mapping):
                stage_bits["verified_artifact"] = dict(payload["verified_artifact"])
            # Prefer explicit substitution_stages from response if present via nested keys.
            candidate_summary = payload.get("candidate_summary") if isinstance(payload.get("candidate_summary"), Mapping) else {}
            stage_bits["substitution_trace"] = {
                "model_invoked": invoked,
                "output_delivered": delivered,
                "candidate_isolated": str(candidate_summary.get("isolation_status", "")) == "isolated",
                "hash_matched": bool(candidate_summary.get("selected_candidate_hash_matches_applied")),
                "verifier_reached": bool(
                    isinstance(verifier_payload, Mapping) and verifier_payload.get("verifier_reached")
                ),
                "verifier_passed": local_verifier_status in {"pass", "passed"},
                "online_consumed": False,
                "final_outcome_contributed": bool(payload.get("outcome_contributed", False)),
                "partial_success_claimed": False,
                "fallback_reason": str(payload.get("fallback_reason") or ""),
            }
            # P1: produce VerifiedAssistPacket from real Local receipt (not hand-written pilot).
            # Packet rides on response so build_online_safe_local_forward can attach consumption.
            if invoked and delivered and task_identity_valid and not payload.get("verified_assist_packet"):
                try:
                    build_vap_from_local_receipt = _nexus_generated_bindings.build_vap_from_local_receipt

                    planner_decision_id = str(
                        shared_snapshot.get("planner_decision_id")
                        or plan.get("planner_decision_id")
                        or plan.get("plan_hash")
                        or ""
                    )
                    plan_hash = str(plan.get("plan_hash") or planner_decision_id or "")
                    vap = build_vap_from_local_receipt(
                        payload,
                        planner_decision_id=planner_decision_id,
                        task_contract_hash=plan_hash,
                        treatment_run_id=str(request.task_id),
                        plan_hash=plan_hash,
                    )
                    if vap is not None:
                        payload = dict(payload)
                        payload["verified_assist_packet"] = vap.to_dict()
                        payload["consume_verified_assist"] = True
                        payload["verified_assist_stage"] = "online_prompt_assembly"
                        refs = list(refs) + [f"local:{request.task_id}:vap:{vap.packet_hash[:16]}"]
                        stage_bits["verified_assist_packet_hash"] = vap.packet_hash
                        stage_bits["verified_assist_packet_id"] = vap.packet_id
                except Exception as exc:
                    # Local native outcome retained, but VAP/consumer closure fail-closed incomplete
                    payload = dict(payload)
                    payload["consume_verified_assist"] = False
                    payload["verified_assist_build_error"] = str(exc)
                    stage_bits["substitution_trace"]["online_consumed"] = False
                    stage_bits["substitution_trace"]["local_consumed"] = False
                    stage_bits["substitution_trace"]["vap_closure_status"] = "incomplete"
            return _stage(
                "local",
                status="SUCCEEDED" if task_identity_valid and invoked and delivered else "FAILED",
                invoked=invoked,
                evidence_present=bool(payload.get("receipt_path") or refs or payload.get("verified_assist_packet")),
                gate_passed=local_boundary_passed,
                outcome_contributed=bool(payload.get("outcome_contributed", False)),
                evidence_refs=refs,
                planner_snapshot_hash=_hash_json(shared_snapshot),
                task_id=request.task_id,
                response_task_id=response_task_id,
                task_identity_shared=task_identity_valid,
                provider_call_count=int(payload.get("provider_call_count") or 0),
                model_call_count=int(payload.get("model_call_count") or 0),
                reason=(
                    "local_task_id_mismatch"
                    if not task_identity_valid
                    else str(payload.get("fallback_reason") or "")
                ),
                response=payload,
                formal_local_runtime_lineage=formal_lineage,
                **local_authority_stage_fields,
                **stage_bits,
            )

        @staticmethod
        def _run_online(
            request: UnifiedRuntimeRequest,
            invoker: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
            context: Mapping[str, Any],
            *,
            workforce_admission_required: bool | None = None,
        ) -> dict[str, Any]:
            if not request.online_enabled:
                return _stage("online", status="NOT_REQUESTED", reason="online_route_disabled")
            if workforce_admission_required is None:
                workforce_admission_required = _workforce_admission_required(
                    request,
                    physical_online=_physical_online_invoker(invoker),
                )

            if workforce_admission_required:
                authority = context.get("gateway_invocation_authority")
                if not isinstance(authority, Mapping):
                    authority = {
                        "schema": GATEWAY_INVOCATION_AUTHORITY_SCHEMA,
                        "status": "BLOCKED",
                        "gate_passed": False,
                        "failure_reason": "workforce_admission_missing",
                    }
                if authority.get("gate_passed") is not True:
                    return _online_authority_failure_stage(
                        task_id=str(context.get("task_id", "")),
                        authority=authority,
                    )

            if invoker is None:
                return _stage("online", status="NOT_RUN", reason="online_invoker_not_supplied")

            # Product deny / unauthorized decisions must never invoke Online transport
            # (including custom repair-style callables). Injected fixtures authorize
            # via online_execution_authorized=true with injected_test_transport source.
            decision_from_context = _nexus_generated_bindings.decision_from_context

            decision = decision_from_context(context)
            if decision is not None and not decision.online_execution_authorized:
                return _stage(
                    "online",
                    status="FAILED",
                    invoked=False,
                    evidence_present=True,
                    gate_passed=False,
                    reason=str(decision.reason or decision.preflight_status or "online_execution_not_authorized"),
                    response={
                        "provider": str(decision.requested_provider or ""),
                        "task_id": str(context.get("task_id", "")),
                        "invoked": False,
                        "output_delivered": False,
                        "gate_passed": False,
                        "provider_call_count": 0,
                        "response": "",
                        "raw_response": "",
                        "usage": {},
                        "error": "online_execution_not_authorized",
                        "online_preflight_status": decision.preflight_status,
                        "online_authorization_source": decision.online_authorization_source,
                        "evidence_refs": [
                            f"online:{context.get('task_id')}:authorization_denied"
                        ],
                    },
                    evidence_refs=[f"online:{context.get('task_id')}:authorization_denied"],
                    task_id=str(context.get("task_id", "")),
                )

            try:
                payload = _mapping(invoker(context))
            except Exception as exc:
                return _stage("online", status="FAILED", reason=f"online_exception:{exc}")
            response_provider_failure = ""
            if workforce_admission_required:
                authority = context.get("gateway_invocation_authority")
                admitted_provider = (
                    str(authority.get("resolved_provider") or "")
                    if isinstance(authority, Mapping)
                    else ""
                )
                response_provider = payload.get("provider")
                if not isinstance(response_provider, str) or not response_provider.strip():
                    response_provider_failure = "online_response_provider_missing"
                elif response_provider != admitted_provider:
                    response_provider_failure = "online_response_provider_mismatch"
                if response_provider_failure:
                    payload = dict(payload)
                    payload["output_delivered"] = False
                    payload["gate_passed"] = False
                    payload["error"] = response_provider_failure
            response_task_id = str(payload.get("task_id", "") or "")
            task_identity_valid = not response_task_id or response_task_id == str(context.get("task_id", ""))
            invoked = bool(payload.get("invoked", False))
            delivered = bool(payload.get("output_delivered", False))
            # Auth/error stdout is not delivery (e.g. IneligibleTierError from provider CLI).
            if online_payload_indicates_non_delivery(payload):
                delivered = False
                payload = dict(payload)
                payload["output_delivered"] = False
                payload["gate_passed"] = False
                if not payload.get("error"):
                    payload["error"] = "online_non_delivery_detected"
            refs = [str(ref) for ref in payload.get("evidence_refs", []) or []]
            return _stage(
                "online",
                status=(
                    "SUCCEEDED"
                    if task_identity_valid and not response_provider_failure and invoked and delivered
                    else "FAILED"
                ),
                invoked=invoked,
                evidence_present=bool(refs or payload.get("provider_call_count", 0) or payload.get("error")),
                gate_passed=task_identity_valid and delivered and bool(payload.get("gate_passed", False)),
                outcome_contributed=bool(payload.get("outcome_contributed", False)),
                evidence_refs=refs,
                task_id=str(context.get("task_id", "")),
                response_task_id=response_task_id,
                task_identity_shared=task_identity_valid,
                reason=(
                    "online_task_id_mismatch"
                    if not task_identity_valid
                    else response_provider_failure
                ),
                context_trace={
                    "gateway_invocation_authority": dict(context.get("gateway_invocation_authority"))
                    if isinstance(context.get("gateway_invocation_authority"), Mapping)
                    else {},
                },
                response=payload,
            )

        @staticmethod
        def _run_callback(
            name: str,
            callback: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
            context: Mapping[str, Any],
            *,
            required: bool,
        ) -> dict[str, Any]:
            if callback is None:
                return _stage(name, status="NOT_RUN" if required else "NOT_REQUESTED", reason=f"{name}_callback_not_supplied")
            try:
                payload = _mapping(callback(context))
            except Exception as exc:
                return _stage(name, status="FAILED", reason=f"{name}_exception:{exc}")
            response_task_id = str(payload.get("task_id", "") or "")
            task_identity_valid = not response_task_id or response_task_id == str(context.get("task_id", ""))
            invoked = bool(payload.get("invoked", True))
            passed = bool(payload.get("gate_passed", payload.get("status", "").lower() in {"ok", "pass", "passed", "succeeded"}))
            refs = [str(ref) for ref in payload.get("evidence_refs", []) or []]
            return _stage(
                name,
                status="SUCCEEDED" if task_identity_valid and invoked and passed else "FAILED",
                invoked=invoked,
                evidence_present=bool(refs or payload.get("evidence")),
                gate_passed=task_identity_valid and passed,
                outcome_contributed=bool(payload.get("outcome_contributed", False)),
                evidence_refs=refs,
                task_id=str(context.get("task_id", "")),
                response_task_id=response_task_id,
                task_identity_shared=task_identity_valid,
                reason=f"{name}_task_id_mismatch" if not task_identity_valid else "",
                response=payload,
            )
    excluded = {'bindings', '_nexus_generated_bindings', 'RuntimeBindings', 'TransportBindings', 'require_complete_bindings'}
    values = {k: v for k, v in locals().items() if k not in excluded and not k.startswith('__')}
    return RuntimeExports(values)
