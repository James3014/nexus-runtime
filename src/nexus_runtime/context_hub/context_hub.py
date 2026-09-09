"""Explicit, deterministic ContextHub assembly facade.

This facade owns context projection and pack shapes. It does not select routes,
write learning state, or discover services; all such behavior is injected.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nexus_runtime.task_context import (
    StatelessContextCoordinator,
    build_context_assembly_contract,
    build_context_budget_receipt,
)

from .ports import (
    DialoguePruner,
    HandoffReader,
    KnowledgeReader,
    LearningWriter,
    MemoryReader,
    PolicyReader,
    Renderer,
    StateCompactor,
    StateReader,
    TextReader,
    WikiReader,
)


@dataclass(frozen=True)
class ContextHubDependencies:
    state_reader: StateReader
    text_reader: TextReader
    memory_reader: MemoryReader
    wiki_reader: WikiReader
    renderer: Renderer
    dialogue_pruner: DialoguePruner
    compactor: StateCompactor
    handoff_reader: HandoffReader | None = None
    knowledge_reader: KnowledgeReader | None = None
    belief_reader: Any | None = None
    learning_writer: LearningWriter | None = None
    policy_reader: PolicyReader | None = None
    clock: Any | None = None


class ContextHub:
    """Context assembly with explicit dependencies and strict missing-port denial."""

    def __init__(
        self, *, deps: ContextHubDependencies, strict_deps: bool = True
    ) -> None:
        if deps is None:
            raise ValueError("strict_deps_requires_context_dependencies")
        required = (
            "state_reader",
            "text_reader",
            "memory_reader",
            "wiki_reader",
            "renderer",
            "dialogue_pruner",
            "compactor",
        )
        if strict_deps and any(
            not callable(getattr(deps, name, None)) for name in required
        ):
            raise ValueError("context_hub_required_dependency_missing")
        self.deps = deps
        self.strict_deps = strict_deps

    def _state(self) -> Any:
        return self.deps.state_reader()

    def _timestamp(self) -> str:
        clock = self.deps.clock
        if callable(clock):
            return str(clock())
        return datetime.now(UTC).isoformat()

    def load_program_rules(self, md_path: str = "program.md") -> str:
        return self.deps.text_reader(md_path)

    def make_pre_routing_decision(
        self,
        task_id: str,
        context: Mapping[str, Any] | None = None,
        *,
        state_view: Any = None,
    ) -> dict[str, Any]:
        context = dict(context or {})
        if context.get("benchmark_run"):
            return {
                "external_needed": False,
                "mode": "benchmark",
                "priority": "normal",
                "audit_level": "full",
                "nas_autotune_needed": False,
            }
        state = state_view or self._state()
        metadata = dict(getattr(state, "metadata", {}) or {})
        task_type = str(metadata.get("task_type", "standard"))
        complexity = float(context.get("complexity_score", 0.0) or 0.0)
        autotune = complexity > 0.7 or any(
            k in task_id.lower() for k in ("0-day", "blackhole", "critical", "hardest")
        )
        decision = {
            "external_needed": bool(context.get("force_external", False))
            or any(
                k in task_id.lower() for k in ("fix", "error", "bug", "issue", "sdk")
            ),
            "mode": task_type,
            "priority": "high" if autotune else "normal",
            "audit_level": "full",
            "nas_autotune_needed": autotune,
        }
        if hasattr(state, "receipt_summary"):
            decision["receipt_summary"] = dict(state.receipt_summary())
        return decision

    def _inject_memory_reminders(self, phase: str) -> Mapping[str, Any]:
        try:
            return dict(self.deps.memory_reader(phase))
        except Exception:
            return {"reminders": [], "total_sources": 0}

    def _retrieve_wiki_context(
        self, query: str, *, max_results: int = 3
    ) -> Mapping[str, Any]:
        try:
            return dict(self.deps.wiki_reader(query, max_results=max_results))
        except Exception:
            return {
                "status": "RETURN",
                "context": "",
                "selected_sources": [],
                "blockers": ["wiki_runtime_exception"],
            }

    def assemble_diag_pack(
        self, violations: list[dict[str, Any]], summary: str
    ) -> dict[str, Any]:
        state = self._state()
        hotspots = sorted(
            {
                str(v.get("file", ""))
                for v in violations
                if isinstance(v, dict) and v.get("file")
            }
        )
        pack = {
            "task_id": getattr(state, "task_id", ""),
            "failure_summary": summary,
            "violations": violations[:10],
            "hotspots": hotspots,
            "history_summary": [
                getattr(s, "summary", "")
                for s in list(getattr(state, "steps_history", []) or [])[-3:]
            ],
            "contract_version": "1.5.2",
            "memory_reminders": self._inject_memory_reminders("D"),
        }
        wiki = self._retrieve_wiki_context(summary)
        pack["wiki_context"] = wiki.get("context", "")
        pack["wiki_retrieval"] = wiki
        if self.deps.knowledge_reader:
            pack["recommended_skills"] = self.deps.knowledge_reader.recommend_skills(
                summary, hotspots[:5]
            )
            pack["wisdom_prior"] = self.deps.knowledge_reader.inject_wisdom_prior(
                summary, hotspots[:5]
            )
        if self.deps.belief_reader:
            confidence = float(self.deps.belief_reader("AUDIT_FAILURE_1") or 0.0)
            if confidence < 0.5:
                pack["belief_warning"] = "low system confidence"
        return pack

    def build_context_budget_receipt(
        self,
        *,
        task_id: str,
        token_budget: int = 4000,
        state_view: Any = None,
        extra_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        state = state_view or self._state()
        l0 = "L0: [BOUNDARIES: core, metrics] [PROHIBITED: delete-history, skip-verify]"
        l1 = f"L1: [TASK: {getattr(state, 'task_id', 'New Task')}]"
        history = (
            getattr(state, "metadata", {}).get("chat_history", [])
            if state is not None
            else []
        )
        sources = [
            {
                "source_id": "L0:rules",
                "kind": "L0",
                "estimated_tokens": max(1, int(len(l0) / 3.8)),
                "priority": 0,
                "required": True,
            },
            {
                "source_id": "L1:index",
                "kind": "L1",
                "estimated_tokens": max(1, int(len(l1) / 3.8)),
                "priority": 1,
                "required": True,
            },
        ]
        if history:
            sources.append(
                {
                    "source_id": "history:recent",
                    "kind": "history",
                    "estimated_tokens": max(1, int(len(str(history[-5:])) / 3.8)),
                    "priority": 20,
                }
            )
        sources.extend(extra_sources or [])
        payload = build_context_budget_receipt(
            sources, token_budget=token_budget
        ).to_dict()
        payload["task_id"] = task_id
        return payload

    def build_context_assembly_contract(self, **kwargs: Any) -> dict[str, Any]:
        receipt = self.build_context_budget_receipt(**kwargs)
        sources = list(receipt.get("kept_sources", [])) + list(
            receipt.get("dropped_sources", [])
        )
        return build_context_assembly_contract(
            task_id=kwargs["task_id"],
            sources=sources,
            token_budget=kwargs.get("token_budget", 4000),
        )

    def build_runtime_context_adapter_receipt(self, **kwargs: Any) -> dict[str, Any]:
        contract = self.build_context_assembly_contract(**kwargs)
        blockers = list(contract.get("blockers", []) or [])
        return {
            "schema": "nexus.runtime_context_adapter_receipt.v1",
            "status": "PASS" if not blockers else "RETURN",
            "task_id": kwargs["task_id"],
            "context_assembly_status": contract.get("status"),
            "runtime_dispatch_changed": False,
            "public_benchmark_allowed": False,
            "runtime_update_allowed": False,
            "contract": contract,
            "blockers": blockers,
        }

    def assemble_context_with_runtime_contract(
        self,
        task_id: str,
        layers: list[int],
        *,
        budget: int = 4000,
        bayesian_params: dict[str, Any] | None = None,
        state_view: Any = None,
        extra_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return StatelessContextCoordinator(
            self.build_runtime_context_adapter_receipt, self.assemble_context
        ).assemble(
            task_id=task_id,
            layers=layers,
            budget=budget,
            bayesian_params=bayesian_params,
            state_view=state_view,
            extra_sources=extra_sources,
        )

    def assemble_context(
        self,
        task_id: str,
        layers: list[int],
        budget: int = 4000,
        bayesian_params: dict[str, Any] | None = None,
    ) -> str:
        state = self._state()
        params = bayesian_params or {}
        # A caller supplied value is an explicit override.  Otherwise the
        # policy port is the source of the global NAS setting; a missing
        # optional policy port deliberately retains the conservative default.
        aggression_value = params.get("nas_aggression")
        if aggression_value is None and self.deps.policy_reader is not None:
            policy = self.deps.policy_reader()
            aggression_value = policy.get("global_nas_aggression", 0.0)
        aggression = float(aggression_value if aggression_value is not None else 0.0)
        summary = self.deps.renderer(state, aggression=aggression)
        state_dict = vars(state) if hasattr(state, "__dict__") else dict(state)
        compact = self.deps.compactor(
            state_dict,
            confidence=float(params.get("confidence", 0.5)),
        )
        l0 = "L0: [BOUNDARIES: core, metrics] [PROHIBITED: delete-history, skip-verify]"
        if self.deps.handoff_reader is not None:
            handoff = self.deps.handoff_reader()
        else:
            handoff = {}
        l1 = (
            f"L1: [TASK: {handoff.get('task_id', 'New Task')}] "
            f"[PHASE: {handoff.get('phase', os.environ.get('NEXUS_PHASE', 'P'))}] "
            f"[TOKEN: {handoff.get('state_token', 'INITIAL')}] [AOS: 131.5]"
        )
        history = getattr(state, "metadata", {}).get("chat_history", [])
        estimated_total = sum(
            len(str(value)) for value in (l0, l1, history, summary, json.dumps(compact))
        ) // 3.8
        threshold = budget * (1.0 - (aggression * 0.2))
        parts = [
            l0,
            l1,
            "--- STRUCTURED CONTEXT (L5-Addressable) ---",
            json.dumps(compact, indent=2),
            "--- TOON-2.0 SUMMARY ---",
            summary,
        ]
        if estimated_total > threshold:
            parts.extend(["--- COMPACT HISTORY ---", self.deps.dialogue_pruner(history)])
        elif history:
            parts.append(str(history[-5:]))
        return "\n".join(parts)

    def assemble_research_pack(
        self, query: str, results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "query": query,
            "results": results,
            "fact_count": len(results),
            "relevance_gate": True,
            "memory_reminders": self._inject_memory_reminders("X"),
        }

    def assemble_feature_pack(
        self, plan: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        state = self._state()
        wiki = self._retrieve_wiki_context(
            str(
                getattr(state, "metadata", {}).get("task_description")
                or getattr(state, "task_id", "")
            )
        )
        return {
            "task": getattr(state, "task_id", ""),
            "plan": plan or {},
            "TOON_SUMMARY": self.deps.renderer(state),
            "memory": dict(self.deps.memory_reader("feature")),
            "rules": self.load_program_rules(),
            "wiki_context": wiki.get("context", ""),
            "wiki_retrieval": wiki,
            "timestamp": self._timestamp(),
        }

    def assemble_conversation_pack(self, audit_mode: bool = False) -> dict[str, Any]:
        state = self._state()
        meta = (
            dict(state.get_conversation_metadata())
            if hasattr(state, "get_conversation_metadata")
            else dict(getattr(state, "metadata", {}).get("conversation", {}) or {})
        )
        history = getattr(state, "metadata", {}).get("chat_history", [])
        steps = list(getattr(state, "steps_history", []) or [])
        step_summary = (
            [
                {
                    "phase": getattr(step, "phase", ""),
                    "summary": getattr(step, "summary", ""),
                }
                for step in steps[-2:]
            ]
            if audit_mode
            else [getattr(step, "summary", "") for step in steps[-5:]]
        )
        pack = {
            "conversation_id": meta.get("conversation_id"),
            "user_goal": meta.get("user_goal"),
            "current_question": meta.get("current_question"),
            "confirmed_constraints": meta.get("confirmed_constraints", []),
            "key_context_facts": meta.get("key_context_facts", {}),
            "user_corrections": meta.get("user_corrections", []),
            "unresolved_points": meta.get("unresolved_points", []),
            "answer_draft_status": meta.get("answer_draft_status"),
            "steps_history_summary": step_summary,
            "pruned_history": self.deps.dialogue_pruner(history),
            "memory_reminders": self._inject_memory_reminders("conversation"),
            "timestamp": self._timestamp(),
        }
        if "last_audit_feedback" in getattr(state, "metadata", {}):
            pack["prior_audit_feedback"] = state.metadata["last_audit_feedback"]
        if not audit_mode and meta.get("needs_research"):
            for step in reversed(steps):
                if (
                    getattr(step, "phase", "") == "X"
                    and getattr(step, "status", "") == "completed"
                ):
                    pack["research_findings"] = getattr(step, "metadata", {}).get(
                        "findings", []
                    )
                    break
        return pack

    def assemble_repair_pack(
        self, diagnosis: Any, reflections: list[dict[str, Any]], research: Any = None
    ) -> dict[str, Any]:
        state = self._state()
        hotspots = list(getattr(diagnosis, "hotspots", []) or [])
        summary = str(getattr(diagnosis, "summary", ""))
        pack = {
            "root_cause": summary,
            "repair_strategy": getattr(diagnosis, "pseudo_flows", []),
            "target_files": hotspots,
            "recent_reflections": reflections[-2:],
            "external_research": getattr(research, "key_findings", [])
            if research
            else [],
            "superpowers_plan": getattr(state, "superpowers_plan", {}),
            "tdd_status": getattr(state, "tdd_status", ""),
            "worktree_uuid": getattr(state, "metadata", {}).get(
                "worktree_uuid", "main-branch"
            ),
            "memory_reminders": self._inject_memory_reminders("R"),
        }
        if self.deps.knowledge_reader:
            pack["recommended_skills"] = self.deps.knowledge_reader.recommend_skills(
                summary, hotspots
            )
            pack["wisdom_prior"] = self.deps.knowledge_reader.inject_wisdom_prior(
                summary, hotspots
            )
        return pack

    def record_crystal_lesson(
        self,
        failure_signature: str,
        root_cause: str,
        lesson: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        if self.deps.learning_writer is None:
            raise ValueError("learning_writer_required")
        return self.deps.learning_writer(
            failure_signature=failure_signature,
            root_cause=root_cause,
            lesson=lesson,
            metadata=dict(metadata or {}),
        )
