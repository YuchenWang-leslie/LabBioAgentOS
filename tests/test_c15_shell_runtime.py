"""C15 step 6: the SHELL runtime and images that declare several runtimes (DECIDED-C15-C)."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import ApprovedImage, ApprovedImageRegistry, ExecutionPlanDraft, WorkflowStage
from labbioagentos.execution import ImageNotApprovedError
from labbioagentos.execution.admission import AdmissionError, parse_shell_probe
from labbioagentos.execution.errors import ExecutionScriptValidationError
from labbioagentos.execution.inventory import InventoryOrigin
from labbioagentos.execution.models import ExecutionRuntime, RuntimeSource

from test_execution_inspection import inspection  # noqa: F401
from test_runtime_milestone_b import boundary  # noqa: F401

PYTHON, SHELL = ExecutionRuntime.PYTHON, ExecutionRuntime.SHELL
REFERENCE = "sha256:" + "1" * 64


def _image(entry_points=None, key="tools") -> ApprovedImage:
    return ApprovedImage(key=key, reference=REFERENCE, entry_points=entry_points or {SHELL: ("sh",)})


# --- Images declare a set of runtimes ---


def test_the_single_runtime_form_of_older_records_reads_as_one_entry_point():
    image = ApprovedImage(key="tools", reference=REFERENCE, runtime=PYTHON, executable=("python", "-I"))
    assert image.entry_points == {PYTHON: ("python", "-I")} and image.runtimes == {PYTHON}
    stored = json.dumps({"key": "tools", "reference": REFERENCE, "runtime": "PYTHON", "executable": ["python"]})
    assert ApprovedImage.model_validate_json(stored).entry_points == {PYTHON: ("python",)}


def test_an_image_with_two_runtimes_round_trips():
    image = _image({PYTHON: ("python",), SHELL: ("sh",)})
    assert ApprovedImage.model_validate_json(image.model_dump_json()) == image


@pytest.mark.parametrize("values", [
    {"runtime": PYTHON, "entry_points": {PYTHON: ("python",)}},
    {"entry_points": {}},
    {"entry_points": {SHELL: ()}},
    {"entry_points": {SHELL: ("-c",)}},
    {"entry_points": {SHELL: ("s h",)}},
])
def test_malformed_entry_points_are_refused(values):
    with pytest.raises(ValidationError):
        ApprovedImage(key="tools", reference=REFERENCE, **values)


def test_the_registry_checks_membership_not_equality():
    registry = ApprovedImageRegistry((_image({PYTHON: ("python",), SHELL: ("sh",)}), _image(key="shell-only")))
    assert registry.resolve("tools", runtime=SHELL).key == "tools"
    assert registry.resolve("tools", runtime=PYTHON).key == "tools"
    with pytest.raises(ImageNotApprovedError):
        registry.resolve("shell-only", runtime=PYTHON)


# --- Both backends run the declared entry point for the requested runtime ---


@pytest.mark.parametrize("backend", ["docker", "apptainer"])
def test_a_shell_plan_runs_sh_on_a_script_mounted_as_shell(tmp_path, backend):
    from apptainer_fixtures import register_sif
    from labbioagentos import DockerCommandBuilder
    from labbioagentos.execution import ApptainerCommandBuilder
    from labbioagentos.execution.mounts import ExecutionWorkspaceManager
    from test_phase6_docker_execution import _plan

    plan = _plan().model_copy(update={"runtime": SHELL})
    workspace = ExecutionWorkspaceManager(tmp_path / "work").prepare(plan)
    if backend == "docker":
        image = _image({PYTHON: ("python",), SHELL: ("sh",)}).model_copy(
            update={"reference": "local/tools", "digest": "sha256:" + "2" * 64})
        argv = DockerCommandBuilder().build(plan, image, workspace, ())
        assert argv[argv.index("--entrypoint") + 1] == "sh" and argv[-1] == "/labbio/script.sh"
        assert any("target=/labbio/script.sh" in item for item in argv)
    else:
        store, reference = register_sif(tmp_path)
        image = _image({PYTHON: ("python",), SHELL: ("sh",)}).model_copy(update={"reference": reference})
        builder = ApptainerCommandBuilder(store, scratch_root=tmp_path / "scratch",
                                          resource_enforcement="scheduler")
        argv = builder.build(plan, image, workspace, ())
        assert argv[-2:] == ("sh", "/labbio/script.sh")
        assert any(item.endswith(":/labbio/script.sh:ro") for item in argv)


def test_a_probe_uses_the_entry_point_of_the_probed_runtime():
    from labbioagentos import DockerCommandBuilder

    image = _image({PYTHON: ("python",), SHELL: ("sh",)}).model_copy(
        update={"reference": "local/tools", "digest": "sha256:" + "2" * 64})
    builder = DockerCommandBuilder()
    shell = builder.import_probe_argv(image, ("-c", "uname -m"), SHELL)
    python = builder.import_probe_argv(image, ("-I", "-c", "pass"))
    assert shell[shell.index("--entrypoint") + 1] == "sh" and python[python.index("--entrypoint") + 1] == "python"


# --- Submission: the default is recorded as a default, and SHELL is never parsed as Python ---


async def _submit_draft(inspection, boundary, **draft):
    service, _, run_id = inspection
    _, _, _, principal, workspace, _, _ = boundary
    return await service.submit(ExecutionPlanDraft(image_key="approved", **draft), principal=principal,
                                workspace=workspace, run_id=run_id, stage_id=WorkflowStage.EXECUTE,
                                invocation_id=uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime, source", [(None, RuntimeSource.DEFAULT), (PYTHON, RuntimeSource.EXPLICIT)])
async def test_the_runtime_source_reaches_plan_and_receipt(inspection, boundary, runtime, source):
    await _submit_draft(inspection, boundary, script_content="pass\n", runtime=runtime)
    plan = inspection[1].plans[-1]
    assert plan.runtime is PYTHON and plan.runtime_source is source


def test_the_executor_result_and_receipt_carry_the_runtime_and_its_source(tmp_path):
    from labbioagentos import ExecutionReceipt
    from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan

    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    image = executor.image_registry.resolve("python-analysis")
    executor.image_registry = ApprovedImageRegistry((image.model_copy(update={"entry_points": {
        PYTHON: ("python",), SHELL: ("sh",)}}),))

    result = executor.execute(_plan().model_copy(update={"runtime": SHELL,
                                                         "runtime_source": RuntimeSource.EXPLICIT}))

    assert result.runtime is SHELL and result.runtime_source is RuntimeSource.EXPLICIT
    receipt = ExecutionReceipt.from_result(result)
    assert receipt.runtime is SHELL and receipt.runtime_source is RuntimeSource.EXPLICIT


@pytest.mark.asyncio
async def test_a_shell_script_is_not_checked_as_python(inspection, boundary):
    await _submit_draft(inspection, boundary, script_content="for x in a b; do echo $x; done\n", runtime=SHELL)
    assert inspection[1].plans[-1].runtime is SHELL
    with pytest.raises(ExecutionScriptValidationError):
        await _submit_draft(inspection, boundary, script_content="for x in a b; do echo $x; done\n")


# --- Configuration ---


def test_configured_images_declare_runtimes_and_the_default_must_run_python(tmp_path):
    from labbioagentos.local_config import load_settings
    from test_c15_image_identity import TWO_IMAGES, _write

    shell_tools = TWO_IMAGES.replace('key = "tools"', 'key = "tools"\nruntimes = ["SHELL"]')
    settings = load_settings(_write(tmp_path, images=shell_tools))
    images = {image.key: image for image in settings.execution.approved_images()}
    assert images["tools"].entry_points == {SHELL: ("sh",)}
    assert images["python-base"].entry_points == {PYTHON: ("python",)}

    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, execution='default_image = "tools"', images=shell_tools))


# --- Admission of images without Python ---


def test_the_shell_probe_reads_the_architecture_and_conda_file_names():
    result = parse_shell_probe(b"x86_64\nsamtools-1.21-h50ea8bc_0.json\nlibzlib-1.3.1-hb9d3cd8_2.json\nhistory\n")
    assert result.machine == "x86_64"
    assert result.inventory.origin is InventoryOrigin.CONDA_META
    assert result.inventory.packages == {"samtools": "1.21", "libzlib": "1.3.1"}
    assert result.inventory.invalid_count == 1
    empty = parse_shell_probe(b"x86_64\n")
    assert empty.inventory.origin is InventoryOrigin.UNKNOWN


@pytest.mark.parametrize("stdout", [b"", b"\n", b"two words\n", b"\xff\n"])
def test_malformed_shell_probe_output_is_refused(stdout):
    with pytest.raises(AdmissionError) as raised:
        parse_shell_probe(stdout)
    assert raised.value.code == "PROBE_OUTPUT_INVALID"


def _shell_admission(tmp_path, probed):
    from test_c15_admission import _admission, _probe

    def probe(image, runtime):
        probed.append(runtime)
        return _probe(python={"numpy": "2.1.3"}) if runtime is PYTHON else b"x86_64\nsamtools-1.21-h_0.json\n"

    return _admission(tmp_path, probe=probe)


def test_every_declared_runtime_is_probed_and_python_reports_the_inventory(tmp_path):
    from test_c15_admission import _admit

    probed = []
    admission, registry, _ = _shell_admission(tmp_path, probed)
    record = _admit(admission, registry, runtimes=(SHELL, PYTHON))

    assert sorted(probed) == [PYTHON, SHELL]
    assert record.entry_points == {SHELL: ("sh",), PYTHON: ("python",)}
    assert record.inventory.packages == {"numpy": "2.1.3"}


def test_a_shell_only_image_takes_its_inventory_from_conda_metadata(tmp_path):
    from test_c15_admission import _admit

    probed = []
    admission, registry, _ = _shell_admission(tmp_path, probed)
    record = _admit(admission, registry, runtimes=(SHELL,))
    assert probed == [SHELL] and record.inventory.packages == {"samtools": "1.21"}


@pytest.mark.parametrize("runtimes", [(), (SHELL, SHELL)])
def test_runtimes_must_be_declared_once_each(tmp_path, runtimes):
    from test_c15_admission import _admit

    admission, registry, _ = _shell_admission(tmp_path, [])
    with pytest.raises(AdmissionError) as raised:
        _admit(admission, registry, runtimes=runtimes)
    assert raised.value.code == "RUNTIMES_INVALID"


def test_a_promotion_must_keep_the_member_record_runtimes(tmp_path):
    from test_c15_admission import _admit

    # Python reports the inventory in both cases, so only the declared runtimes differ.
    admission, registry, _ = _shell_admission(tmp_path, [])
    personal = _admit(admission, registry, runtimes=(PYTHON,))
    (tmp_path / "lab").mkdir()
    second, registry2, _ = _shell_admission(tmp_path / "lab", [])
    with pytest.raises(AdmissionError) as raised:
        _admit(second, registry2, key="lab-tools", runtimes=(PYTHON, SHELL), expected=personal)
    assert raised.value.code == "INVENTORY_MISMATCH"
    assert _admit(second, registry2, key="lab-tools", runtimes=(PYTHON,), expected=personal).key == "lab-tools"
