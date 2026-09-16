"""Chained failures retain technical evidence, not arbitrary stderr content."""

import json
import subprocess
import sys

import pytest

from labbioagentos import DockerExecutor, ExecutionReceipt, ProcessOutcome
from labbioagentos.execution.diagnostic_sources import IMPORT_PROBE, verified_image_imports
from test_phase6_docker_execution import FakeDockerRunner, _environment, _image, _plan


def wrapped_failure(module="optional_backend", connector="The above exception was the direct cause of the following exception:"):
    return (
        'Traceback (most recent call last):\n'
        '  File "/usr/local/lib/python3.11/site-packages/library/backend.py", line 7, in operation\n'
        f'    from {module}.core import operation\n'
        f"ModuleNotFoundError: No module named '{module}'\n\n"
        f'{connector}\n\n'
        'Traceback (most recent call last):\n'
        '  File "/labbio/script.py", line 2, in <module>\n'
        '    library.operation()\n'
        'ImportError: PRIVATE_MESSAGE /private/path token=PRIVATE_SECRET\n'
    ).encode()


@pytest.mark.parametrize("connector, relation", [
    ("The above exception was the direct cause of the following exception:", "DIRECT_CAUSE"),
    ("During handling of the above exception, another exception occurred:", "CONTEXT"),
])
def test_wrapped_missing_dependency_keeps_separate_cause(connector, relation):
    diagnostics = DockerExecutor._safe_python_diagnostics(
        wrapped_failure(connector=connector), script_content="import library\nlibrary.operation()\n",
        verified_imports=frozenset({"optional_backend.core"}),
    )
    assert len(diagnostics) == 2
    terminal, cause = diagnostics
    assert terminal.exception_type == "ImportError"
    assert terminal.script_line_numbers == (2,)
    assert terminal.missing_module is None
    assert terminal.chain_relation is None
    assert cause.exception_type == "ModuleNotFoundError"
    assert cause.missing_module == "optional_backend"
    assert cause.chain_relation == relation
    assert cause.script_line_numbers == ()
    assert "PRIVATE" not in "".join(item.model_dump_json() for item in diagnostics)


def test_unverified_transitive_name_is_not_released():
    diagnostics = DockerExecutor._safe_python_diagnostics(
        wrapped_failure(module="PRIVATE_IDENTIFIER"), script_content="import library\n",
    )
    assert len(diagnostics) == 2
    assert diagnostics[1].exception_type == "ModuleNotFoundError"
    assert diagnostics[1].missing_module is None
    assert "PRIVATE" not in "".join(item.model_dump_json() for item in diagnostics)


def test_unlinked_earlier_traceback_is_not_a_cause():
    diagnostics = DockerExecutor._safe_python_diagnostics(
        wrapped_failure(connector="ordinary logging separator"), script_content="import library\n",
    )
    assert len(diagnostics) == 1
    assert diagnostics[0].exception_type == "ImportError"


def test_chain_is_bounded_and_keeps_terminal_first():
    connector = "\n\nDuring handling of the above exception, another exception occurred:\n\n"
    trace = "Traceback (most recent call last):\nKeyError: 'PRIVATE'"
    diagnostics = DockerExecutor._safe_python_diagnostics(connector.join([trace] * 12).encode())
    assert len(diagnostics) == 4
    assert diagnostics[0].chain_relation is None
    assert all(item.chain_relation == "CONTEXT" for item in diagnostics[1:])
    assert "PRIVATE" not in str(diagnostics)


class ProbeRunner(FakeDockerRunner):
    def run(self, argv, *, timeout_seconds):
        if IMPORT_PROBE in argv:
            self.calls.append((argv, timeout_seconds))
            return ProcessOutcome(exit_code=0, stdout=b'["optional_backend.core"]',
                                  stderr=b'', duration_seconds=0.1)
        return super().run(argv, timeout_seconds=timeout_seconds)


def test_receipt_preserves_chain_and_probe_has_no_data_or_network(tmp_path):
    runner = ProbeRunner(exit_code=1, stderr=wrapped_failure())
    _, executor, _ = _environment(tmp_path, runner)
    result = executor.execute(_plan(script_content="import library\nlibrary.operation()\n"))
    receipt = ExecutionReceipt.from_result(result)
    assert receipt.status.value == "FAILED"
    assert receipt.diagnostics[1].missing_module == "optional_backend"
    assert receipt.diagnostics == result.diagnostics
    assert len(runner.calls) == 2  # One submitted program; one data-free source probe.
    argv, timeout = runner.calls[1]
    assert "--mount" not in argv and "--volume" not in argv and "--env" not in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "--rm" in argv
    assert argv[argv.index("--pull") + 1] == "never"
    assert _image().resolved_reference in argv
    assert timeout == 15
    assert all(word not in receipt.model_dump_json() for word in ("PRIVATE", "/usr/local", "library.operation"))


@pytest.mark.parametrize("stdout, code, timed_out", [
    (b'not json', 0, False), (b'["/private/path"]', 0, False),
    (b'["valid"]', 1, False), (b'["valid"]', None, True),
    (b'"not a list"', 0, False), (b'x' * 32769, 0, False),
])
def test_failed_or_invalid_probe_does_not_release_names(stdout, code, timed_out):
    runner = FakeDockerRunner(stdout=stdout, exit_code=code, timed_out=timed_out)
    assert verified_image_imports(wrapped_failure(), _image(), runner, "docker") == frozenset()
    assert len(runner.calls) == 1


def test_probe_reads_static_import_at_exact_line_only(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    library = root / "module.py"
    library.write_text("raise RuntimeError('MUST_NOT_EXECUTE')\nfrom optional_backend.core import method\n")
    outside = tmp_path / "private.py"
    outside.write_text("import PRIVATE_VALUE\n")
    (root / "link.py").symlink_to(outside)
    # Test the exact probe body, replacing only its trusted installation roots.
    setup = "import sysconfig\nsysconfig.get_path = lambda key: " + repr(str(root)) + "\n"
    completed = subprocess.run([sys.executable, "-I", "-S", "-c", setup + IMPORT_PROBE,
        json.dumps([(str(library), 2), (str(library), 1), (str(outside), 1), (str(root / "link.py"), 1)])],
        capture_output=True, check=True)
    assert json.loads(completed.stdout) == ["optional_backend.core"]
    assert not completed.stderr


@pytest.mark.asyncio
async def test_tool_receipt_evidence_and_inspection_keep_same_chain(inspection, boundary, monkeypatch):
    from dataclasses import replace
    from pantheon.providers import LocalProvider
    from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
    from test_runtime_milestone_b import _toolset

    service, executor, run_id = inspection
    original = executor.execute
    diagnostics = DockerExecutor._safe_python_diagnostics(
        wrapped_failure(), verified_imports=frozenset({"optional_backend.core"}),
    )
    def execute(plan):
        from labbioagentos import ExecutionStatus, ExecutionFailureClass
        return original(plan).model_copy(update={"diagnostics": diagnostics,
            "status": ExecutionStatus.FAILED, "exit_code": 1,
            "error_class": ExecutionFailureClass.NON_ZERO_EXIT, "output_artifact_refs": ()})
    monkeypatch.setattr(executor, "execute", execute)
    tools = _toolset(boundary, WorkflowStage.EXECUTE,
                     ("execution_submit", "execution_inspect"), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    await provider.list_tools()
    response = await provider.call_tool("execution_submit", {"image_key": "approved", "script_content": "pass\n"})
    receipt = response["data"]
    assert receipt["diagnostics"][1]["missing_module"] == "optional_backend"
    inspected = await provider.call_tool("execution_inspect", {"execution_id": receipt["execution_id"]})
    assert inspected["data"]["receipt"] == receipt
    evidence = tools.evidence_items()
    assert evidence[0].safe_result == receipt
    assert evidence[1].safe_result["receipt"] == receipt
    assert len(executor.plans) == 1
    assert "PRIVATE" not in json.dumps([item.model_dump(mode="json") for item in evidence])


from test_execution_inspection import inspection  # noqa: F401,E402
from test_runtime_milestone_b import boundary  # noqa: F401,E402
