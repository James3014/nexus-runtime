from __future__ import annotations

from typing import Any


JUDGE_PANEL = "judge_panel"
LEGACY_LLM_JUDGE_PANEL = "llm_judge_panel"

CAPABILITY_ALIASES = {
    LEGACY_LLM_JUDGE_PANEL: JUDGE_PANEL,
}


def normalize_capability_name(name: Any) -> str:
    raw = str(name or "").strip()
    return CAPABILITY_ALIASES.get(raw, raw)


def normalize_capability_names(names: Any) -> list[str]:
    out: list[str] = []
    for item in names or []:
        normalized = normalize_capability_name(item)
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def normalize_capability_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    out = dict(receipt)
    out["name"] = normalize_capability_name(out.get("name"))
    return out
