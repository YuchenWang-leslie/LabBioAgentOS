"""Small test facade over the real Native Kernel capability contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace

from labbioagentos.runtime.native_tools import (
    CAPABILITY_ARGUMENT_MODELS,
    build_labbio_tool_registry,
)


class _BoundHost:
    def __init__(self, operations):
        self.operations = operations

    async def invoke(self, capability_name, arguments):
        method = getattr(self.operations, capability_name)
        return await method(**arguments.model_dump(mode="json"))


class LocalProvider:
    """Expose native schemas and dispatch to existing bound test operations."""

    def __init__(self, operations):
        self.operations = operations
        self.registry = build_labbio_tool_registry(_BoundHost(operations))

    async def initialize(self):
        return None

    async def list_tools(self):
        allowed = frozenset(self.operations.binding.capability_allowlist)
        return [
            SimpleNamespace(
                name=spec.name,
                inputSchema={
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            )
            for spec in self.registry.specs
            if spec.name in allowed
        ]

    async def call_tool(self, name, arguments):
        if name not in self.operations.binding.capability_allowlist:
            raise ValueError("Capability is not allowed")
        model = CAPABILITY_ARGUMENT_MODELS[name]
        validated = model.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        )
        return await _BoundHost(self.operations).invoke(name, validated)
