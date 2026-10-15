"""Shared Apptainer test fixtures; no Apptainer installation is required."""

from __future__ import annotations

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    ContainerExecutor,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionWorkspaceManager,
    InMemoryTraceSink,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    RunTraceRecorder,
)
from labbioagentos.execution import ApptainerCommandBuilder, SifStore


def register_sif(tmp_path, content: bytes = b"synthetic SIF fixture\n"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.sif"
    source.write_bytes(content)
    store = SifStore(tmp_path / "sif-store")
    return store, store.register(source)


def apptainer_image(reference: str, *, network_allowed: bool = False) -> ApprovedImage:
    return ApprovedImage(
        key="python-analysis",
        reference=reference,
        runtime=ExecutionRuntime.PYTHON,
        executable=("python",),
        network_allowed=network_allowed,
    )


def apptainer_environment(
    tmp_path,
    runner,
    *,
    resource_enforcement="scheduler",
    verify_sif_on_execute=False,
    allow_network=False,
    image_network_allowed=False,
    traced=False,
    isolation_record=None,
    workspace_root=None,
):
    sif_store, reference = register_sif(tmp_path)
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink) if traced else None
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    builder = ApptainerCommandBuilder(
        sif_store,
        scratch_root=tmp_path / "scratch",
        resource_enforcement=resource_enforcement,
        verify_sif_on_execute=verify_sif_on_execute,
    )
    executor = ContainerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry(
            (apptainer_image(reference, network_allowed=image_network_allowed),)
        ),
        execution_policy=ExecutionPolicy(allow_network=allow_network),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(workspace_root or tmp_path / "executions"),
        output_collector=OutputCollector(
            store, ArtifactRegistrationPolicy(), trace_recorder=recorder,
        ),
        process_runner=runner,
        command_builder=builder,
        trace_recorder=recorder,
        isolation_record=isolation_record,
    )
    return store, executor, sink, sif_store, reference


def bind_fields(argv):
    """Return (source, target, mode) for every --bind argument."""

    binds = []
    for index, argument in enumerate(argv[:-1]):
        if argument == "--bind":
            source, target, mode = argv[index + 1].split(":")
            binds.append((source, target, mode))
    return binds
