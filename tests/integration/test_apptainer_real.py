"""Opt-in C13 phase 2 check against a real Apptainer installation on Linux.

Run on a node with Apptainer (not through the scheduler, which is phase 3):

    LABBIO_RUN_APPTAINER=1 LABBIO_APPTAINER_SIF=/path/to/python.sif \
        python -m pytest tests/integration/test_apptainer_real.py

Optional: LABBIO_APPTAINER_BINARY (default "apptainer") and
LABBIO_APPTAINER_RESOURCE_ENFORCEMENT ("scheduler" or "apptainer-cgroups").
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    ContainerExecutor,
    ExecutionPlan,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionStatus,
    ExecutionWorkspaceManager,
    InMemoryTraceSink,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    RequestedResources,
    RunTraceRecorder,
    TraceEventType,
    WorkflowStage,
)
from labbioagentos.execution import ApptainerCommandBuilder, SifStore, SubprocessApptainerRunner
from labbioagentos.execution.apptainer_probe import probe_capabilities

pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_APPTAINER") != "1" or not sys.platform.startswith("linux"),
    reason="set LABBIO_RUN_APPTAINER=1 and LABBIO_APPTAINER_SIF on Linux for real Apptainer checks",
)

SENTINEL = "PRIVATE_C13_APPTAINER_SENTINEL_5381"
BINARY = os.environ.get("LABBIO_APPTAINER_BINARY", "apptainer")
ENFORCEMENT = os.environ.get("LABBIO_APPTAINER_RESOURCE_ENFORCEMENT", "scheduler")


@pytest.fixture
def backend(tmp_path):
    source = Path(os.environ["LABBIO_APPTAINER_SIF"])
    sif_store = SifStore(tmp_path / "sif")
    reference = sif_store.register(source)
    runner = SubprocessApptainerRunner(max_output_file_bytes=16_777_216)
    (tmp_path / "probe").mkdir()
    capabilities = probe_capabilities(
        binary=BINARY, sif_path=sif_store.path_for(reference), executable="python",
        runner=runner, scratch_root=tmp_path / "probe",
    )
    capabilities.require(resource_enforcement=ENFORCEMENT)
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    executor = ContainerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry((ApprovedImage(
            key="python-real", reference=reference, runtime=ExecutionRuntime.PYTHON,
        ),)),
        execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
        process_runner=runner,
        command_builder=ApptainerCommandBuilder(
            sif_store, scratch_root=tmp_path / "scratch",
            resource_enforcement=ENFORCEMENT, apptainer_binary=BINARY,
        ),
        trace_recorder=recorder,
        isolation_record=capabilities.trace_record(resource_enforcement=ENFORCEMENT),
    )
    return executor, sink, tmp_path


def _plan(script: str) -> ExecutionPlan:
    return ExecutionPlan(
        run_id=uuid4(), stage_id=WorkflowStage.EXECUTE, image_key="python-real",
        script_content=script, resources=RequestedResources(timeout_seconds=120),
    )


def test_minimal_mock_task_runs_through_container_executor(backend):
    executor, sink, _ = backend

    result = executor.execute(_plan("print('c13 apptainer mock task')\n"))

    assert result.status is ExecutionStatus.SUCCEEDED and result.exit_code == 0
    assert Path(result.stdout_ref.storage_locator).read_bytes() == b"c13 apptainer mock task\n"
    planned = next(event for event in sink.read() if event.event_type is TraceEventType.EXECUTION_PLANNED)
    assert planned.payload["isolation"]["backend"] == "apptainer"


def test_sandbox_denies_network_secrets_and_writes_outside_outputs(backend, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)
    executor, _, tmp_path = backend
    script = (
        "import json, os, socket\n"
        "report = {'secret_visible': any('" + SENTINEL + "' in value for value in os.environ.values())}\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    report['network'] = True\n"
        "except OSError:\n"
        "    report['network'] = False\n"
        "try:\n"
        "    open('/labbio/script.py', 'a').write('#')\n"
        "    report['script_writable'] = True\n"
        "except OSError:\n"
        "    report['script_writable'] = False\n"
        "report['host_root_visible'] = os.path.exists(" + repr(str(tmp_path)) + ")\n"
        "open(os.path.join(os.environ['LABBIO_OUTPUT_DIR'], 'written.txt'), 'w').write('ok')\n"
        "print(json.dumps(report))\n"
    )

    result = executor.execute(_plan(script))

    assert result.status is ExecutionStatus.SUCCEEDED
    report = json.loads(Path(result.stdout_ref.storage_locator).read_text())
    assert report == {"secret_visible": False, "network": False,
                      "script_writable": False, "host_root_visible": False}
    outputs = list((tmp_path / "executions").glob("*/outputs/written.txt"))
    assert len(outputs) == 1 and outputs[0].read_text() == "ok"
