"""Provider-visible primitive and nested tool-schema fidelity."""

import pytest

from .contracts import KernelContractBackend


@pytest.mark.asyncio
async def test_primitive_constraints_reach_the_provider_schema(
    kernel_backend: KernelContractBackend,
):
    schema = await kernel_backend.tool_schema("inspect")
    parameters = schema["parameters"]

    assert parameters["additionalProperties"] is False
    assert parameters["required"] == ["view"]
    view = parameters["properties"]["view"]
    assert view["enum"] == ["SUMMARY", "TOP_N"]
    assert view["type"] == "string"
    assert parameters["properties"]["limit"]["anyOf"] == [
        {"maximum": 100, "minimum": 1, "type": "integer"},
        {"type": "null"},
    ]


@pytest.mark.asyncio
async def test_nested_models_arrays_enums_and_bounds_are_preserved(
    kernel_backend: KernelContractBackend,
):
    schema = await kernel_backend.tool_schema("submit")
    draft = schema["parameters"]["properties"]["draft"]

    assert draft["type"] == "object"
    assert draft["additionalProperties"] is False
    assert set(draft["required"]) == {"image_key", "script_label", "requested_outputs"}
    assert draft["properties"]["runtime"]["enum"] == ["PYTHON"]
    assert draft["properties"]["image_key"]["pattern"] == "^[a-z0-9-]+$"
    script_label = draft["properties"]["script_label"]
    assert script_label["maxLength"] == 12
    assert script_label["minLength"] == 2
    assert script_label["pattern"] == "^[A-Z_]+$"
    assert script_label["type"] == "string"
    outputs = draft["properties"]["requested_outputs"]
    assert outputs["minItems"] == 1
    assert outputs["maxItems"] == 4
    assert outputs["items"]["additionalProperties"] is False
    assert outputs["items"]["properties"]["artifact_type"]["enum"] == [
        "TABLE",
        "REPORT",
    ]
    cpu = draft["properties"]["resources"]["properties"]["cpu_cores"]
    assert cpu["maximum"] == 8
    assert cpu["minimum"] == 1
    assert cpu["type"] == "integer"
    serialized = repr(schema)
    assert "$ref" not in serialized
    assert "$defs" not in serialized
