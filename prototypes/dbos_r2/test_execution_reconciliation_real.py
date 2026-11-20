"""Real local Apptainer, synthetic infrastructure programs, no Agent/model.

Exercise Native execution_submit -> SubmissionService -> ContainerExecutor.
These are not scientific programs and not a DBOS production-stage integration.
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from labbioagentos import (
    AccessService, ApprovedImage, ApprovedImageRegistry, ArtifactExposureService,
    ArtifactRegistrationPolicy, AuthorizationPolicy, ContainerExecutor,
    ExecutionPolicy, ExecutionRuntime, ExecutionSubmissionService,
    ExecutionWorkspaceManager, InMemoryProjectStore, InMemoryTraceSink,
    LabBioRuntimeToolSet, LocalArtifactStore, MountResolver, OutputCollector,
    Project, RunTraceRecorder, RuntimeCapabilityServices,
)
from labbioagentos.artifacts import ExposurePolicy
from labbioagentos.execution import ApptainerCommandBuilder, SifStore, SubprocessApptainerRunner
from labbioagentos.execution.apptainer_probe import probe_capabilities
from labbioagentos.execution.job_ledger import JobLedger, JobLedgerState
from labbioagentos.execution.launchers.base import JobLauncherError
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher, SystemdProbeRunner
from labbioagentos.runtime.observations import ObservationStore
from execution_reconciliation import execution_binding, reconcile_execution
from round_trip import binding, native_record
from runtime_binding import file_digest, probe_settings


pytestmark = pytest.mark.skipif(os.environ.get("LABBIO_RUN_APPTAINER") != "1",
                               reason="Explicit user-level Apptainer engineering replay")

# Engineering side-effect counter and finish gate, not an analysis program.
PROGRAM = (
    "import os,time\nfrom pathlib import Path\n"
    "root=Path(os.environ['LABBIO_OUTPUT_DIR'])\n"
    "with (root/'started').open('a') as stream: stream.write('accepted\\n')\n"
    "while not (root/'finish').exists(): time.sleep(0.05)\n"
    "print('r2-engineering-finished')\n"
)


@pytest.mark.parametrize("fault", ["ack", "outcome", "receipt", "normal"])
@pytest.mark.asyncio
async def test_real_acceptance_loss_is_queried_not_reexecuted(tmp_path, monkeypatch, fault):
    root = tmp_path
    binary = os.environ["LABBIO_APPTAINER_BINARY"]
    source = Path(os.environ["LABBIO_APPTAINER_SIF"])
    raw = probe_settings(root).model_dump(mode="json", by_alias=True)
    raw["execution"].update(image_key="r2-real-python", image_reference="sha256:" + file_digest(source))
    raw["execution"]["apptainer"].update(binary=binary, sif_store=str(source.parent))
    raw["execution"]["resources"]["timeout_seconds"] = 45
    settings = type(probe_settings(root)).model_validate(raw)
    runner = SubprocessApptainerRunner(max_output_file_bytes=1048576)

    class LostAcknowledgement(SystemdLocalLauncher):
        accepted = None
        submissions = 0

        async def submit(self, spec):
            self.submissions += 1
            self.accepted = await super().submit(spec)
            for _ in range(200):
                if (spec.workspace_root / "outputs/started").exists():
                    if fault in {"ack", "outcome"}:
                        raise JobLauncherError("synthetic acknowledgement loss after actual container start")
                    (spec.workspace_root / "outputs/finish").touch()
                    return self.accepted
                await asyncio.sleep(0.05)
            raise AssertionError("Apptainer program did not start")

    owner = LostAcknowledgement(runner, resource_enforcement="systemd-affinity")
    assert owner.probe()["status"] == "OK"
    sif = SifStore(source.parent)
    reference = settings.execution.image_reference
    (root / "probe").mkdir()
    capabilities = probe_capabilities(
        binary=binary, sif_path=sif.verify(reference), executable="python",
        runner=SystemdProbeRunner(owner, settings.execution.resources), scratch_root=root / "probe",
    )
    capabilities.require(resource_enforcement="systemd-affinity")
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    store = LocalArtifactStore(root / "artifacts", trace_recorder=recorder)
    ledger = JobLedger(root / "native-jobs.sqlite")

    class WitnessExecutor(ContainerExecutor):
        plan = None

        async def execute_async(self, plan):
            self.plan = plan
            native_record(str(root), "run-binding", execution_binding(settings, plan))
            return await super().execute_async(plan)

    executor = WitnessExecutor(
        store=store, image_registry=ApprovedImageRegistry((ApprovedImage(
            key=settings.execution.image_key, reference=reference, runtime=ExecutionRuntime.PYTHON,
        ),)), execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(root / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
        process_runner=runner, launcher=owner, job_ledger=ledger, trace_recorder=recorder,
        command_builder=ApptainerCommandBuilder(sif, scratch_root=root / "scratch",
            resource_enforcement="systemd-affinity", apptainer_binary=binary),
        isolation_record=capabilities.trace_record(resource_enforcement="systemd-affinity"),
    )
    projects = InMemoryProjectStore()
    projects.register(Project(project_id="r2", lab_id="r2-lab", owner_user_id="r2-user"))
    access = AccessService(projects, AuthorizationPolicy(), trace_recorder=recorder)
    context = binding()
    observations = ObservationStore(root / "observations")

    def services():
        submission = ExecutionSubmissionService(artifact_store=store, access_service=access,
            executor=executor, trace_recorder=recorder, job_ledger=ledger)
        tools = LabBioRuntimeToolSet(context, RuntimeCapabilityServices(
            artifact_store=store, artifact_exposure=ArtifactExposureService(store, ExposurePolicy(),
                access_service=access, trace_recorder=recorder), execution_submission=submission,
            trace_recorder=recorder, observation_store=observations,
        ))
        return tools, submission

    if fault == "receipt":
        def fail_receipt(**_):
            raise OSError("synthetic native receipt commit loss")
        monkeypatch.setattr(ledger, "record_execution_submission", fail_receipt)
    tools, service = services()
    arguments = dict(image_key=settings.execution.image_key, script_content=PROGRAM,
                     resources=settings.execution.resources.model_dump(mode="json"))
    witness = {"fault": fault, "model_requests": 0, "scientific_result": False}
    try:
        result = await tools.execution_submit(**arguments)
        assert executor.plan is not None, result
        plan = executor.plan
        started = root / "executions" / str(plan.execution_id) / "outputs/started"
        assert started.read_text() == "accepted\n"
        witness.update(execution_id=str(plan.execution_id), original_result=result,
                       runtime_binding=execution_binding(settings, plan))
        if fault == "normal":
            assert result["success"] is True and result["data"]["status"] == "SUCCEEDED"
            saved = observations.load(context, UUID(result["capability_invocation_id"]))
            assert saved.public_response == result
            assert ledger.get(plan.execution_id).state is JobLedgerState.COLLECTED
            assert ledger.execution_submission(plan.run_id, plan.execution_id) is not None
            _, restored = services()
            inspected = restored.inspect(plan.execution_id, principal=context.principal,
                workspace=context.workspace, run_id=plan.run_id)
            assert inspected["receipt"]["execution_id"] == str(plan.execution_id)
            assert owner.submissions == 1
            witness["restored_without_execution"] = True
        else:
            assert result["success"] is False, result
            assert ledger.execution_submission(plan.run_id, plan.execution_id) is None
            fresh = SystemdLocalLauncher(runner, resource_enforcement="systemd-affinity")
            before = await reconcile_execution(root, fresh, plan, settings)
            witness["reconciliation_before_cleanup"] = before
            if fault in {"ack", "outcome"}:
                assert before["status"] == "WAITING" and before["reason"] == "EXECUTION_ACCEPTED"
                assert ledger.get(plan.execution_id).state is JobLedgerState.SUBMITTING
                if fault == "outcome":
                    (started.parent / "finish").touch()
                    await owner.wait(owner.accepted)
                    assert (await owner.collect(owner.accepted)).exit_code == 0
                else:
                    await owner.cancel(owner.accepted)
            else:
                assert ledger.get(plan.execution_id).state is JobLedgerState.COLLECTED
                assert before["status"] == "BLOCKED"
            after = await reconcile_execution(root, fresh, plan, settings)
            assert after["status"] == "BLOCKED"
            assert after["replay_allowed"] is after["scientific_result_available"] is False
            witness["reconciliation_after_stop"] = after
            next_tools, _ = services()
            rejected = await next_tools.execution_submit(**arguments)
            assert rejected["success"] is False
            assert owner.submissions == 1 and started.read_text() == "accepted\n"
            witness["retry_rejected_without_launch"] = rejected
        assert owner.submissions == 1
    finally:
        if owner.accepted is not None:
            await owner.cancel(owner.accepted)
        witness["service_stop_confirmed"] = True
        (root / "REAL_EXECUTION_WITNESS.json").write_text(json.dumps(witness, indent=2))
        (root / "TRACE.json").write_text(json.dumps([event.model_dump(mode="json") for event in sink.read()]))
        ledger.close()
