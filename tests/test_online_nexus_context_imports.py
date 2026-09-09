from __future__ import annotations

import importlib.util

from nexus_runtime_support_candidate.services.online_nexus_context import (
    build_online_nexus_context,
    build_online_nexus_context_from_runtime,
    build_plan_gated_postflight_invokers,
    compact_capability_evidence_for_prompt,
)


def test_online_context_delayed_owner_calls_work_without_nexus_package():
    assert importlib.util.find_spec("nexus") is None
    bundle = {
        "bundle_hash": "bundle-1",
        "entries": [
            {
                "name": "memory",
                "success": True,
                "invoked_real": True,
                "consumer_payload": {"summary": "bounded result"},
            }
        ],
    }
    compact = compact_capability_evidence_for_prompt(bundle)
    assert compact["consumer_payloads"]

    context = build_online_nexus_context(
        task_statement="inspect repository",
        task_id="online-imports",
        route={"selected_capabilities": ["memory"]},
        capability_evidence_bundle=bundle,
    )
    assert context.lineage["consumer_execution_modes"]["memory"]

    from_runtime = build_online_nexus_context_from_runtime(
        {
            "task_id": "online-runtime-imports",
            "task_statement": "inspect repository",
            "route": {"selected_capabilities": ["memory"]},
            "capability_evidence_bundle": bundle,
            "local": {
                "invoked": True,
                "task_id": "online-runtime-imports",
                "response": {
                    "action": "advisor",
                    "output_delivered": True,
                    "evidence_refs": ["local-evidence-1"],
                },
            },
        }
    )
    assert "[LOCAL_ASSIST_CONTEXT]" in from_runtime.prompt
    assert "local-evidence-1" in from_runtime.prompt

    postflight = build_plan_gated_postflight_invokers()["artifact_gate"](
        {"task_id": "online-postflight", "artifact": {}}
    )
    assert postflight["invoked"] is True
    assert "consumer_payload" in postflight
