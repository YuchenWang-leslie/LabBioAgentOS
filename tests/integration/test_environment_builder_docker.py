"""Opt-in data-free build/probe acceptance against the real local Docker daemon."""

import json
import os
import re
import subprocess
from uuid import uuid4

import pytest

from labbioagentos import ApprovedImage, ApprovedImageRegistry, ExecutionRuntime
from labbioagentos.execution.environment_builder import DockerEnvironmentBuilder
from labbioagentos.execution.environments import EnvironmentService


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_ENV_DOCKER") != "1",
    reason="set LABBIO_RUN_ENV_DOCKER=1 for real Docker environment-build acceptance",
)


@pytest.mark.asyncio
async def test_real_docker_build_and_probe_respect_the_environment_contract(tmp_path):
    base_id = os.environ.get("LABBIO_ENV_BASE_IMAGE", "")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", base_id)
    assert subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", base_id],
        capture_output=True, text=True, check=True, timeout=15,
    ).stdout.strip() == base_id
    base = ApprovedImage(key="base", reference=base_id, runtime=ExecutionRuntime.PYTHON)
    registry = ApprovedImageRegistry((base,))
    service = EnvironmentService(
        root=tmp_path / "environments", image_registry=registry,
        builder=DockerEnvironmentBuilder(timeout_seconds=180, cpus=1),
        owner_user_id="environment-test-user",
    )
    image_id = None
    try:
        result = await service.build_environment("base", (), (), run_id=uuid4())
        assert result["status"] == "SUCCEEDED"
        assert result["cache_hit"] is False
        image_id = result["image_reference"]
        assert image_id != base_id
        assert registry.resolve(result["image_key"]).resolved_reference == image_id
        attempt = next((service.attempts_root).iterdir())
        receipt = json.loads((attempt / "receipt.json").read_text())
        assert receipt["status"] == "SUCCEEDED" and receipt["image_id"] == image_id
        label = subprocess.run(
            ["docker", "image", "inspect", "--format",
             '{{ index .Config.Labels "labbio.environment.build" }}', image_id],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
        assert label == attempt.name
    finally:
        if image_id is not None:
            subprocess.run(
                ["docker", "image", "rm", image_id],
                capture_output=True, check=True, timeout=30,
            )
