"""NVIDIA GPU inventory for Docker executions (DECIDED-C15-F).

The host learns its GPUs once, before the first execution, by running
`nvidia-smi -L` in a data-free container with every GPU attached. Without a
working GPU runtime, or with fewer GPUs than the policy allows, executions are
refused; nothing ever falls back to CPU. Each execution then names its
devices explicitly, so trace can record exactly which GPUs it received.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from .errors import ContainerStartError

MAX_GPU_LIST_BYTES = 65_536
_GPU_LINE = re.compile(r"GPU (\d{1,3}): ([A-Za-z0-9 ._()/+-]{1,128}) \(UUID: (GPU-[0-9a-f-]{36})\)")


@dataclass(frozen=True)
class GpuDevice:
    index: int
    name: str
    uuid: str

    def record(self) -> dict:
        return {"index": self.index, "name": self.name, "uuid": self.uuid}


class GpuUnavailableError(ContainerStartError):
    """The GPU runtime is missing, or reports fewer GPUs than the policy allows."""


def parse_gpu_list(text: str) -> tuple[GpuDevice, ...]:
    """Every line of `nvidia-smi -L` names one GPU; anything else is refused."""

    if len(text.encode("utf-8")) > MAX_GPU_LIST_BYTES:
        raise ValueError("The GPU list is too long")
    devices = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _GPU_LINE.fullmatch(line.strip())
        if match is None:
            raise ValueError("The GPU list has an unexpected line")
        devices.append(GpuDevice(int(match.group(1)), match.group(2), match.group(3)))
    if len({device.uuid for device in devices}) != len(devices):
        raise ValueError("The GPU list repeats a device")
    return tuple(sorted(devices, key=lambda device: device.index))


class DockerGpuInventory:
    """The host's GPUs as Docker sees them, learned once by a probe container."""

    def __init__(self, image_reference: str, *, minimum: int, docker_binary: str = "docker",
                 run: Callable = subprocess.run):
        if minimum < 1:
            raise ValueError("A GPU inventory is only needed when GPUs are allowed")
        self.image_reference, self.minimum = image_reference, minimum
        self.docker_binary, self._run = docker_binary, run
        self._devices: tuple[GpuDevice, ...] | None = None

    def probe_argv(self) -> tuple[str, ...]:
        # nvidia-smi is injected by the GPU runtime, so any image can host the probe.
        return (self.docker_binary, "run", "--rm", "--pull", "never", "--name", "labbio-gpu-" + uuid4().hex,
                "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", "65534:65534", "--memory", "256m", "--cpus", "1", "--pids-limit", "32",
                "--gpus", "all", "--entrypoint", "nvidia-smi", self.image_reference, "-L")

    def probe(self) -> dict:
        """Learn the GPUs or raise GpuUnavailableError; returns the trace record."""

        try:
            result = self._run(list(self.probe_argv()), capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            raise GpuUnavailableError("The GPU probe could not run") from None
        if result.returncode != 0:
            raise GpuUnavailableError("The Docker GPU runtime is not available")
        try:
            devices = parse_gpu_list(result.stdout)
        except ValueError:
            raise GpuUnavailableError("The GPU probe output is invalid") from None
        if len(devices) < self.minimum:
            raise GpuUnavailableError("The host has fewer GPUs than the policy allows")
        self._devices = devices
        return {"backend": "docker", "gpus": [device.record() for device in devices]}

    def assign(self, count: int) -> tuple[GpuDevice, ...]:
        """The first `count` GPUs; the probe must have run."""

        if count < 1:
            return ()
        if self._devices is None or len(self._devices) < count:
            raise GpuUnavailableError("No GPU inventory covers this request")
        return self._devices[:count]
