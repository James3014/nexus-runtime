from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def _required_identity(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}_missing")
    return value


def _optional_identity(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text if text.strip() else ""


def _safe_task_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or any(char in raw for char in "/\\\x00"):
        raise ValueError("task_id_invalid")
    return raw


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    return {"status": "FAILED", "error": "stage_result_not_mapping"}
