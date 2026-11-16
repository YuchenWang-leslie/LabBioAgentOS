"""C15 step 8c: an image declared able to use GPUs must contain a CUDA runtime (DECIDED-C15-F)."""

from __future__ import annotations

import json
import platform
import subprocess
import sys

import pytest

from labbioagentos.execution.admission import (
    CUDA_RUNTIME_GLOBS,
    PROBE_PROGRAM,
    SHELL_PROBE_PROGRAM,
    AdmissionError,
    parse_probe,
    parse_shell_probe,
)
from labbioagentos.execution.catalog import GpuSupport
from labbioagentos.execution.models import ExecutionRuntime

from test_c15_admission import FakeRealizer, _admission, _admit, _probe
from test_c15_member_locks import _base, _built, _lock_admission

CUDART = ["libcudart.so.12", "libcudart.so.12.6.77"]


# --- Reading the probes ---


def test_the_python_probe_reports_cuda_runtime_names():
    assert parse_probe(_probe(cuda_runtime=CUDART)).cuda_runtime == tuple(CUDART)
    assert parse_probe(_probe()).cuda_runtime == ()  # An older probe, or nothing found.


@pytest.mark.parametrize("value", ["libcudart.so.12", {"libcudart.so.12": True}, [5],
                                   ["libcudart.so.12; reboot"], ["libcuda.so.1"], ["libcudart.so.12"] * 33])
def test_python_probe_cuda_names_are_checked(value):
    with pytest.raises(AdmissionError) as raised:
        parse_probe(_probe(cuda_runtime=value))
    assert raised.value.code == "PROBE_OUTPUT_INVALID"


def test_the_shell_probe_reports_cuda_runtime_lines_apart_from_conda_names():
    result = parse_shell_probe(b"x86_64\nsamtools-1.21-h_0.json\nlabbio-cudart libcudart.so.12\n")
    assert result.cuda_runtime == ("libcudart.so.12",)
    assert result.inventory.packages == {"samtools": "1.21"} and result.inventory.invalid_count == 0
    for stdout in (b"x86_64\nlabbio-cudart ../../etc/passwd\n",
                   b"x86_64\n" + b"labbio-cudart libcudart.so.12\n" * 33):
        with pytest.raises(AdmissionError):
            parse_shell_probe(stdout)


# --- The probe programs themselves ---


def test_the_search_covers_system_conda_and_pip_locations():
    assert "/usr/lib/*-linux-gnu/libcudart*.so*" in CUDA_RUNTIME_GLOBS
    assert "/usr/local/cuda*/targets/*/lib/libcudart*.so*" in CUDA_RUNTIME_GLOBS
    assert "/opt/conda/envs/*/lib/libcudart*.so*" in CUDA_RUNTIME_GLOBS
    assert "/usr/local/lib/python3*/site-packages/nvidia/*/lib/libcudart*.so*" in CUDA_RUNTIME_GLOBS
    assert all(pattern.startswith("/") and "'" not in pattern and '"' not in pattern
               for pattern in CUDA_RUNTIME_GLOBS)


def test_the_python_probe_finds_a_pip_cuda_runtime_on_its_path(tmp_path):
    for relative in ("nvidia/cuda_runtime/lib/libcudart.so.12", "torch/lib/libcudart-9335f6a2.so.12"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(b"")
    completed = subprocess.run([sys.executable, "-c", PROBE_PROGRAM], capture_output=True, timeout=120, check=True,
                               env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"})

    payload = json.loads(completed.stdout.decode().splitlines()[-1])
    assert {"libcudart.so.12", "libcudart-9335f6a2.so.12"} <= set(payload["cuda_runtime"])
    assert parse_probe(completed.stdout).machine == platform.machine()


def test_the_shell_probe_finds_a_cuda_runtime_and_ignores_unmatched_patterns(tmp_path):
    library = tmp_path / "cuda-12.6" / "lib64"
    library.mkdir(parents=True)
    (library / "libcudart.so.12").write_bytes(b"")
    # The same program with one toolkit directory moved under tmp_path.
    program = SHELL_PROBE_PROGRAM.replace("/usr/local/cuda*/lib64/", f"{tmp_path}/cuda*/lib64/")
    assert program != SHELL_PROBE_PROGRAM

    result = parse_shell_probe(subprocess.run(["sh", "-c", program], capture_output=True, timeout=60,
                                              check=True).stdout)

    assert "libcudart.so.12" in result.cuda_runtime and result.machine == platform.machine()


# --- Admission ---


def _probe_with(cuda=(), runtime_cuda=None):
    def probe(image, runtime):
        found = runtime_cuda.get(runtime, ()) if runtime_cuda is not None else cuda
        if runtime is ExecutionRuntime.PYTHON:
            return _probe(python={"numpy": "2.1.3"}, cuda_runtime=list(found))
        return ("x86_64\n" + "".join(f"labbio-cudart {name}\n" for name in found)).encode()
    return probe


@pytest.mark.parametrize("gpu", [GpuSupport.SUPPORTED, GpuSupport.REQUIRED])
def test_an_image_declared_for_gpus_without_a_cuda_runtime_is_not_admitted(tmp_path, gpu):
    realizer = FakeRealizer()
    admission, registry, personal = _admission(tmp_path, probe=_probe_with(), realizer=realizer)

    with pytest.raises(AdmissionError) as raised:
        _admit(admission, registry, gpu=gpu)

    assert raised.value.code == "CUDA_RUNTIME_MISSING" and "--gpu NONE" in raised.value.hint
    assert personal.records() == {} and realizer.discarded == realizer.realized


@pytest.mark.parametrize("gpu, cuda", [(GpuSupport.NONE, ()), (GpuSupport.NONE, CUDART),
                                       (GpuSupport.SUPPORTED, CUDART), (GpuSupport.REQUIRED, CUDART)])
def test_an_image_with_a_cuda_runtime_or_declared_without_gpus_is_admitted(tmp_path, gpu, cuda):
    admission, registry, _personal = _admission(tmp_path, probe=_probe_with(cuda))
    assert _admit(admission, registry, gpu=gpu).gpu is gpu


def test_any_runtime_probe_may_find_the_cuda_runtime(tmp_path):
    admission, registry, _personal = _admission(
        tmp_path, probe=_probe_with(runtime_cuda={ExecutionRuntime.SHELL: CUDART[:1]}))
    record = _admit(admission, registry, gpu=GpuSupport.SUPPORTED,
                    runtimes=(ExecutionRuntime.PYTHON, ExecutionRuntime.SHELL))
    assert record.gpu is GpuSupport.SUPPORTED


def test_a_lock_built_image_declared_for_gpus_needs_a_cuda_runtime_too(tmp_path):
    admission, store, realizer = _lock_admission(tmp_path, probe=_probe_with())
    with pytest.raises(AdmissionError) as raised:
        admission.admit_locked(key="six", purpose="six.", gpu=GpuSupport.SUPPORTED, base=_base(),
                               build=lambda _image: _built())
    assert raised.value.code == "CUDA_RUNTIME_MISSING" and store.records() == {} and realizer.discarded

    (tmp_path / "with").mkdir()
    admission, store, _realizer = _lock_admission(tmp_path / "with", probe=_probe_with(CUDART))
    record = admission.admit_locked(key="six", purpose="six.", gpu=GpuSupport.SUPPORTED, base=_base(),
                                    build=lambda _image: _built())
    assert record.gpu is GpuSupport.SUPPORTED
