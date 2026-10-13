from __future__ import annotations


REQUEST_SCHEMA = "nexus.unified_runtime.request.v1"

RECEIPT_SCHEMA = "nexus.unified_runtime.receipt.v1"

LOCAL_MODEL_INVOCATION_AUTHORITY_SCHEMA = "nexus.local_model_invocation_authority.v1"

RUNTIME_WORKFORCE_ADMISSION_SCHEMA = "nexus.runtime_workforce_admission.v1"

RUNTIME_WORKFORCE_ADMISSION_RECORD_SCHEMA = "nexus.runtime_workforce_admission_record.v1"

WORKFORCE_ADMISSION_DECISION_SCHEMA = "nexus.workforce_admission_decision.v1"

GATEWAY_INVOCATION_AUTHORITY_SCHEMA = "nexus.gateway_invocation_authority.v1"


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
        "default_model": "",  # resolved from workforce policy (MODEL_DEFAULT_NOT_ADMITTED if none)
    },
}


REGISTERED_CLI_MODEL_BINDING_UNSUPPORTED_PROVIDERS: frozenset[str] = frozenset(
    {"grok", "openai"}
)


REGISTERED_CLI_MODEL_BINDING_FLAGS: dict[str, tuple[str, str]] = {
    "agy": ("", "--model"),
    "codex": ("exec", "-m"),
    "opencode": ("run", "--model"),
    "cline": ("", "--model"),
    "mimo": ("run", "--model"),
    "ollama": ("run", ""),
}


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
