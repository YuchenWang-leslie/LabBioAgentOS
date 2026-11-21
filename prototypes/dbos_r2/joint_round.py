"""Isolated DBOS -> Native tool -> user Apptainer round, not an App entrypoint.

The provider is synthetic. The tool, execution ledger, scoped observations and
container are real. Only infrastructure counters are executed by these probes.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from time import monotonic
from uuid import UUID

from dbos import DBOS, SetWorkflowID

from labbioagentos import (
    AccessService, ApprovedImage, ApprovedImageRegistry, ArtifactExposureService,
    ArtifactRegistrationPolicy, AuthorizationPolicy, ContainerExecutor,
    ExecutionPolicy, ExecutionRuntime, ExecutionSubmissionService,
    ExecutionWorkspaceManager, InMemoryProjectStore, JsonlTraceSink,
    LocalArtifactStore, MountResolver, OutputCollector, Project,
    RunTraceRecorder, RuntimeCapabilityServices,
)
from labbioagentos.artifacts import ExposurePolicy
from labbioagentos.execution import ApptainerCommandBuilder, SifStore, SubprocessApptainerRunner
from labbioagentos.execution.apptainer_probe import probe_capabilities
from labbioagentos.execution.job_ledger import JobLedger
from labbioagentos.execution.models import ExecutionPlan
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher, SystemdProbeRunner
from labbioagentos.kernel import FakeProvider
from labbioagentos.kernel.contracts import (
    FinishReason, ModelRequest, ModelTurn, ProviderBinding, PublicMessage, ToolCall,
)
from labbioagentos.kernel.loop import AgentLoop
from labbioagentos.kernel.errors import ToolExecutionFailure
from labbioagentos.kernel.tooling import ModelResultProjector, ToolDispatcher, current_tool_execution_context
from labbioagentos.local_config import LocalSettings
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
from labbioagentos.runtime.observations import ObservationStore, canonical_bytes

from controlled_round import command, worker
from child_cancellation import cancel_child
from execution_reconciliation import execution_binding, reconcile_execution
from round_trip import RUN_ID, binding, crash, identity, native_record, receipt_ref, remote_acceptance, verify
from runtime_binding import runtime_identity


def settings_for(root):
    return LocalSettings.model_validate_json((root / "case.json").read_text())


def runtime_for(settings):
    config = settings.execution.apptainer
    if not config.sif_store.is_dir():
        raise ValueError("Bound SIF storage is missing")
    return runtime_identity(settings, sif_path=SifStore(config.sif_store).path_for(
        settings.execution.image_reference), executable_path=config.binary)


def call_name(kind, call_id):
    # Stable per-call identity within the immutable run/stage binding. A random
    # dispatch UUID is not a recovery key and is never supplied by the model.
    return "joint-" + kind + "-" + hashlib.sha256(call_id.encode()).hexdigest()


class BoundExecutor:
    def __init__(self, root, settings, executor):
        self.root, self.settings, self.executor = root, settings, executor

    async def execute_async(self, plan):
        name = call_name("execution", current_tool_execution_context().call_id)
        native_record(str(self.root), name, execution_binding(self.settings, plan))
        crash("tool-plan-before-submit")
        return await self.executor.execute_async(plan)


class FaultLauncher(SystemdLocalLauncher):
    async def submit(self, spec):
        handle = await super().submit(spec)
        if os.environ.get("R2_CRASH_POINT") == "execution-accepted-before-receipt":
            for _ in range(300):
                if (spec.workspace_root / "outputs/started").exists():
                    crash("execution-accepted-before-receipt")
                await asyncio.sleep(0.02)
            raise AssertionError("No real execution start before acknowledgement fault")
        return handle


class RecordedObservations(ObservationStore):
    def save(self, context, item, public_response):
        name = call_name("observation", current_tool_execution_context().call_id)
        native_record(str(self.root.parent), name, {
            "observation_id": str(item.capability_invocation_id),
            "sha256": hashlib.sha256(canonical_bytes(public_response)).hexdigest(),
        })
        crash("native-receipt-before-observation")
        super().save(context, item, public_response)
        crash("observation-before-dbos")


class RecoveryHost(NativeRuntimeCapabilityHost):
    async def invoke(self, name, arguments):
        runtime = self.runtime
        self._host._guard(name)  # Restoring a receipt cannot bypass current capability authority.
        call_id = current_tool_execution_context().call_id
        request = {"name": name, "arguments": arguments.model_dump(mode="json")}
        key = call_name("intent", call_id)
        previous = native_record(str(runtime.root), key)
        if previous is not None:
            if previous != request:
                raise ValueError("Stable tool identity has different arguments")
            return await runtime.restore(call_id)
        if any(native_record(str(runtime.root), call_name(kind, call_id)) is not None
               for kind in ("execution", "observation")):
            raise ValueError("Native tool intent is missing; replay refused")
        native_record(str(runtime.root), key, request)
        crash("tool-intent-before-submit")
        return await super().invoke(name, arguments)


class NativeServices:
    def __init__(self, root, settings):
        self.root = root
        self.context = binding()
        calls = json.loads((root / "request.json").read_text())["calls"]
        for kind, path in (("execution", root / "native-jobs.sqlite"),
                           ("observation", root / "observations")):
            if not path.exists() and any(native_record(str(root), call_name(kind, call["call_id"]))
                                        is not None for call in calls):
                raise ValueError(f"Native {kind} storage is missing; recovery must not recreate it")
        self.trace = RunTraceRecorder(JsonlTraceSink(root / "trace.jsonl"))
        store = LocalArtifactStore(root / "artifacts", trace_recorder=self.trace)
        projects = InMemoryProjectStore()
        projects.register(Project(project_id="r2", lab_id="r2-lab", owner_user_id="r2-user"))
        access = AccessService(projects, AuthorizationPolicy(), trace_recorder=self.trace)
        self.ledger = JobLedger(root / "native-jobs.sqlite")
        known = {UUID(record["request"]["execution_id"]) for call in calls
            if (record := native_record(str(root), call_name("execution", call["call_id"]))) is not None}
        previous = (set(self.ledger.execution_submission_ids(self.context.run_id))
            | set(self.ledger.unreceipted_executions(self.context.run_id)))
        if previous - known:
            self.ledger.close()
            raise ValueError("Native execution exists without its stable call binding; replay refused")
        self.observations = RecordedObservations(root / "observations")
        config = settings.execution.apptainer
        runner = SubprocessApptainerRunner(max_output_file_bytes=1048576)
        self.launcher = FaultLauncher(runner, resource_enforcement="systemd-affinity")
        sif = SifStore(config.sif_store)
        (root / "probe").mkdir(exist_ok=True)
        capabilities = probe_capabilities(
            binary=config.binary, sif_path=sif.verify(settings.execution.image_reference),
            executable="python", runner=SystemdProbeRunner(SystemdLocalLauncher(runner,
                resource_enforcement="systemd-affinity"), settings.execution.resources),
            scratch_root=root / "probe",
        )
        capabilities.require(resource_enforcement="systemd-affinity")
        executor = ContainerExecutor(
            store=store, image_registry=ApprovedImageRegistry((ApprovedImage(
                key=settings.execution.image_key, reference=settings.execution.image_reference,
                runtime=ExecutionRuntime.PYTHON,
            ),)), execution_policy=ExecutionPolicy(),
            mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
            workspace_manager=ExecutionWorkspaceManager(root / "executions"),
            output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
            process_runner=runner, launcher=self.launcher, job_ledger=self.ledger,
            trace_recorder=self.trace, command_builder=ApptainerCommandBuilder(sif,
                scratch_root=root / "scratch", resource_enforcement="systemd-affinity",
                apptainer_binary=config.binary),
            isolation_record=capabilities.trace_record(resource_enforcement="systemd-affinity"),
        )
        self.submission = ExecutionSubmissionService(artifact_store=store, access_service=access,
            executor=BoundExecutor(root, settings, executor), trace_recorder=self.trace, job_ledger=self.ledger)
        self.host = RecoveryHost(self.context, RuntimeCapabilityServices(
            artifact_store=store, artifact_exposure=ArtifactExposureService(store, ExposurePolicy(),
                access_service=access, trace_recorder=self.trace),
            execution_submission=self.submission, trace_recorder=self.trace,
            observation_store=self.observations,
        ))
        self.host.runtime = self
        self.registry = build_labbio_tool_registry(self.host)

    async def restore(self, call_id):
        record = native_record(str(self.root), call_name("observation", call_id))
        execution = native_record(str(self.root), call_name("execution", call_id))
        if record is None or execution is None:
            raise ToolExecutionFailure(code="RECOVERY_OUTCOME_UNKNOWN",
                safe_message="Native tool evidence is incomplete; automatic execution replay is refused.")
        plan = ExecutionPlan.model_validate_json(canonical_bytes(execution["request"]))
        if execution != execution_binding(settings_for(self.root), plan):
            raise ValueError("Execution binding differs")
        saved = self.observations.load(self.context, UUID(record["observation_id"]))
        if ((saved.stage_id, saved.invocation_id, saved.item.actor_profile_key,
             saved.item.actor_agent_name, saved.item.capability_name) !=
            (self.context.stage_id, self.context.invocation_id, self.context.actor_profile_key,
             self.context.actor_agent_name, "execution_submit")
                or hashlib.sha256(canonical_bytes(saved.public_response)).hexdigest() != record["sha256"]):
            raise ValueError("Native observation binding differs")
        original = self.submission.inspect(plan.execution_id, principal=self.context.principal,
            workspace=self.context.workspace, run_id=self.context.run_id)
        if saved.public_response.get("success") is not True or saved.public_response["data"] != original["receipt"]:
            raise ValueError("Native observation and execution receipt differ")
        return saved.public_response

    def close(self):
        self.ledger.close()


@DBOS.step(retries_allowed=False)
def model_step(root, request):
    if native_record(root, "joint-provider-response") is not None:
        return receipt_ref(root, "joint-provider-response")
    if native_record(root, "joint-provider-intent") is not None:
        raise RuntimeError("PROVIDER_OUTCOME_UNKNOWN: usage unknown; replay refused")
    native_record(root, "joint-provider-intent", {"request": request, "usage": None})
    remote_acceptance(root, "provider")
    turn = ModelTurn(tool_calls=tuple(ToolCall.model_validate_json(canonical_bytes(call))
        for call in request["calls"]), finish_reason=FinishReason.TOOL_CALLS)
    crash("provider-return-before-native")
    native_record(root, "joint-provider-response", turn.model_dump(mode="json"))
    crash("provider-native-before-dbos")
    return receipt_ref(root, "joint-provider-response")


async def dispatch(root, provider_ref):
    turn = ModelTurn.model_validate_json(canonical_bytes(verify(str(root), provider_ref)))
    services = NativeServices(root, settings_for(root))
    try:
        if native_record(str(root), "joint-tools-response") is not None:
            return receipt_ref(str(root), "joint-tools-response")
        request = json.loads((root / "request.json").read_text())
        if request.get("mode") == "child-cancel":
            result = await cancel_child(root, services, turn)
        else:
            dispatcher = ToolDispatcher(services.registry, ModelResultProjector(max_bytes=60000),
                stop_requested=lambda: "USER_STOP" if (root / "stop-requested").exists() else None)
            outcomes = await dispatcher.dispatch_batch(turn.tool_calls, finish_reason=turn.finish_reason)
            result = {"results": [outcome.result.model_dump(mode="json") for outcome in outcomes]}
        native_record(str(root), "joint-tools-response", result)
        crash("tool-native-before-dbos")
        return receipt_ref(str(root), "joint-tools-response")
    finally:
        services.close()


@DBOS.step(retries_allowed=False)
def execution_step(root, provider_ref):
    return asyncio.run(dispatch(Path(root), provider_ref))


@DBOS.step(retries_allowed=False)
def feedback_step(root, provider_ref, tools_ref):
    """Confirm exact public feedback at a fake provider, never credit an Agent read."""
    result = verify(root, tools_ref)
    asyncio.run(validate_delivery(Path(root), json.loads((Path(root) / "request.json").read_text()), result))
    if native_record(root, "joint-feedback") is not None:
        return receipt_ref(root, "joint-feedback")
    if result.get("cancelled") or any(row["status"] != "SUCCEEDED" or not row["content"]["success"]
                                     for row in result["results"]):
        native_record(root, "joint-feedback", {"status": "NOT_SENT", "tools_ref": tools_ref})
        return receipt_ref(root, "joint-feedback")
    if native_record(root, "joint-feedback-intent") is not None:
        raise RuntimeError("PROVIDER_FEEDBACK_OUTCOME_UNKNOWN: usage unknown; replay refused")
    from labbioagentos.kernel.contracts import ToolResult
    turn = ModelTurn.model_validate_json(canonical_bytes(verify(root, provider_ref)))
    provider = FakeProvider(binding=ProviderBinding(provider="fixture", model="r2-synthetic",
        endpoint="https://r2-fake.invalid/v1", transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="DISABLED"),
        turns=(ModelTurn(content="Infrastructure feedback received.", finish_reason=FinishReason.STOP),))
    request = ModelRequest(binding=provider.binding, messages=(
        PublicMessage(role="SYSTEM", content="Synthetic infrastructure feedback probe."),
        PublicMessage(role="ASSISTANT", tool_calls=turn.tool_calls),
        *(AgentLoop._tool_feedback(ToolResult.model_validate_json(canonical_bytes(row)), {})
          for row in result["results"])),
        max_output_tokens=128, deadline_monotonic=monotonic() + 30,
        kernel_run_id=identity("joint-kernel"), invocation_id=binding().invocation_id, turn_index=2)
    request_hash = hashlib.sha256(canonical_bytes(request.model_dump(mode="json",
        exclude={"deadline_monotonic"}))).hexdigest()
    native_record(root, "joint-feedback-intent", {"request_sha256": request_hash, "usage": None})
    remote_acceptance(root, "feedback")
    response = asyncio.run(provider.generate(request))
    crash("feedback-return-before-native")
    native_record(root, "joint-feedback", {"status": "RECEIVED", "request_sha256": request_hash,
        "tools_ref": tools_ref, "observation_ids": [row["content"]["capability_invocation_id"]
            for row in result["results"]], "response": response.model_dump(mode="json")})
    crash("feedback-native-before-dbos")
    return receipt_ref(root, "joint-feedback")


@DBOS.workflow(name="labbio-r2-joint-round")
def joint_round(root, request):
    provider = model_step(root, request)
    tools = execution_step(root, provider)
    return {"provider": provider, "tools": tools, "feedback": feedback_step(root, provider, tools)}


def run_joint(root):
    settings = settings_for(root)
    request = json.loads((root / "request.json").read_text())
    frozen = {"version": "r2-joint-1", "runtime": runtime_for(settings), "request": request}
    if native_record(str(root), "joint-binding") is None and (
            (root / "joint.sqlite").exists() or (root / "native-jobs.sqlite").exists()
            or any((root / "native-artifacts").glob("*.json"))):
        raise ValueError("Native run binding is missing; recovery cannot rebind existing history")
    native_record(str(root), "joint-binding", frozen)
    asyncio.run(retire_executions(root))
    if native_record(str(root), "joint-cancellation") is not None:
        raise RuntimeError("CANCELLED: Native cancellation is not permission to replay")
    DBOS(config={"name": "labbio-r2-joint",
        "application_version": hashlib.sha256(canonical_bytes(frozen)).hexdigest(),
        "system_database_url": f"sqlite:///{root / 'joint.sqlite'}",
        "executor_id": "r2-joint-local", "enable_otlp": False, "log_level": "ERROR"})
    DBOS.launch()
    try:
        with SetWorkflowID(str(RUN_ID)):
            refs = joint_round(str(root), request)
        for ref in refs.values():
            verify(str(root), ref)
        result = verify(str(root), refs["tools"])
        asyncio.run(validate_delivery(root, request, result))
        print(json.dumps(refs, sort_keys=True), flush=True)
        return 0 if not result.get("cancelled") and all(row["status"] == "SUCCEEDED" and row["content"]["success"]
            for row in result["results"]) else 2
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=5)


async def validate_delivery(root, request, result):
    if result.get("cancelled"):
        if native_record(str(root), "joint-cancellation") != result:
            raise ValueError("Cancellation evidence differs")
        return
    services = NativeServices(root, settings_for(root))
    try:
        rows = result["results"]
        parsed = services.registry.validate_batch(tuple(ToolCall.model_validate_json(canonical_bytes(call))
            for call in request["calls"]), finish_reason=FinishReason.TOOL_CALLS, max_calls=2)
        if len(rows) != len(parsed):
            raise ValueError("DBOS result differs from the bound model tool call")
        for row, call in zip(rows, parsed):
            if (row["call_id"], row["name"]) != (call.request.call_id, call.request.name):
                raise ValueError("DBOS result differs from the bound model tool call")
            if row["status"] == "SUCCEEDED" and row["content"].get("success"):
                if native_record(str(root), call_name("intent", call.request.call_id)) != {
                    "name": call.request.name, "arguments": call.arguments.model_dump(mode="json")}:
                    raise ValueError("Native tool intent differs from frozen call")
                if row["content"] != await services.restore(call.request.call_id):
                    raise ValueError("Scheduler result differs from Native observation")
    finally:
        services.close()


async def retire_executions(root):
    path = root / "native-jobs.sqlite"
    if not path.exists():
        return
    settings = settings_for(root)
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1048576),
        resource_enforcement="systemd-affinity")
    # The already-bound model calls are authoritative; never scan unrelated
    # ledgers or infer that a vanished service means no side effect occurred.
    ledger = JobLedger(path)
    try:
        for call in json.loads((root / "request.json").read_text())["calls"]:
            name = call_name("execution", call["call_id"])
            execution = native_record(str(root), name)
            if execution is None:
                continue
            plan = ExecutionPlan.model_validate_json(canonical_bytes(execution["request"]))
            if ledger.get(plan.execution_id) is None:
                continue
            state = await reconcile_execution(root, launcher, plan, settings, binding_name=name)
            handle = await launcher.find(plan.execution_id)
            if handle is not None:
                await launcher.cancel(handle)
            # A cleanup receipt, NOT a replacement for a Native execution result.
            with (root / "retirement.jsonl").open("a") as stream:
                stream.write(json.dumps({"execution_id": str(plan.execution_id), "before": state,
                    "stop_confirmed": True, "replay_allowed": False}) + "\n")
    finally:
        ledger.close()


async def managed(root):
    extra = ("--fault", os.environ["R2_CRASH_POINT"]) if os.environ.get("R2_CRASH_POINT") else ()
    async with worker(root, command(Path(__file__), root, "--child", *extra)) as (launcher, handle):
        try:
            await launcher.wait(handle)
            outcome = await launcher.collect(handle)
            sys.stdout.buffer.write(outcome.stdout)
            sys.stderr.buffer.write(outcome.stderr)
            return outcome.exit_code if outcome.exit_code is not None else 2
        finally:
            async def cleanup():
                await launcher.cancel(handle)
                await retire_executions(root)
            stopping = asyncio.create_task(cleanup())
            interrupted = False
            while True:
                try:
                    await asyncio.shield(stopping)
                    break
                except asyncio.CancelledError:
                    if stopping.cancelled():
                        raise
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--fault")
    args = parser.parse_args()
    if args.child:
        if args.fault:
            os.environ["R2_CRASH_POINT"] = args.fault
        raise SystemExit(run_joint(args.root.resolve()))
    else:
        raise SystemExit(asyncio.run(managed(args.root.resolve())))
