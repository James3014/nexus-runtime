"""Shared, fail-closed execution identity values.

This module is data-only.  It defines the vocabulary consumed by
``CanonicalTaskContext``, ``CapabilityPlanner`` and route/receipt contracts;
it does not select a route.
"""

from __future__ import annotations

from enum import Enum


class ExecutionWorld(str, Enum):
    PRODUCT_RUNTIME = "product_runtime"
    BENCHMARK_INSTRUMENT = "benchmark_instrument"
    LOCAL_ARMOR = "local_armor"
    DEVELOPMENT_TASK = "development_task"


class CanonicalExecutionTopology(str, Enum):
    DIRECT_CANONICAL = "DIRECT_CANONICAL"
    ISOLATED_TARGET = "ISOLATED_TARGET"
    ASSISTED_CANONICAL = "ASSISTED_CANONICAL"


class TransportIngress(str, Enum):
    MCP = "mcp"
    CLI = "cli"
    DIRECT = "direct"
    BENCH = "bench"


def require_execution_world(value: object) -> str:
    try:
        return ExecutionWorld(str(value)).value
    except ValueError as exc:
        raise ValueError(f"invalid_execution_world:{value}") from exc


def require_execution_topology(value: object) -> str:
    try:
        return CanonicalExecutionTopology(str(value)).value
    except ValueError as exc:
        raise ValueError(f"invalid_execution_topology:{value}") from exc


def require_transport_ingress(value: object) -> str:
    try:
        return TransportIngress(str(value)).value
    except ValueError as exc:
        raise ValueError(f"invalid_transport_ingress:{value}") from exc
