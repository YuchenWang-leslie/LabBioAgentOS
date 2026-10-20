"""Service main process: verify cgroup limits and watch the caller's lifetime pipe.

This standalone stdlib-only entrypoint runs inside a systemd transient service.
Returning from its main process makes KillMode=control-group stop descendants,
including children that detach from the original process group.
"""

import json
import selectors
import subprocess
import sys
from pathlib import Path


def read_limits():
    membership = Path("/proc/self/cgroup").read_text().splitlines()
    relative = next(line[3:] for line in membership if line.startswith("0::"))
    root = Path("/sys/fs/cgroup")
    directory = root / relative.lstrip("/")
    if not directory.is_relative_to(root) or ".." in directory.parts:
        raise ValueError("Invalid cgroup membership")
    return {name: (directory / name).read_text().strip()
            for name in ("cpu.max", "memory.max", "pids.max")}


def enforced(limits, cpus, memory_bytes, pids):
    try:
        quota, period = map(int, limits["cpu.max"].split())
        memory, tasks = int(limits["memory.max"]), int(limits["pids.max"])
        return (0 < quota / period <= cpus and 0 < memory <= memory_bytes and 0 < tasks <= pids)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def main():
    cpus, memory_bytes, pids = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    try:
        limits = read_limits()
        if not enforced(limits, cpus, memory_bytes, pids):
            raise ValueError("Resource limits are absent or exceed the requested limits")
    except (OSError, StopIteration, ValueError):
        print("LOCAL_RESOURCE_ENFORCEMENT_UNAVAILABLE", file=sys.stderr, flush=True)
        return 125
    if sys.argv[4:] == ["--probe"]:
        print(json.dumps(limits))
        return 0
    # Check parent lifetime before starting anything with side effects.
    with selectors.DefaultSelector() as watch:
        watch.register(sys.stdin, selectors.EVENT_READ)
        if watch.select(timeout=0) and not sys.stdin.buffer.read(1):
            return 143
        try:
            child = subprocess.Popen(sys.argv[4:], stdin=subprocess.DEVNULL)
        except OSError:
            print("LOCAL_COMMAND_START_FAILED", file=sys.stderr, flush=True)
            return 127
        while child.poll() is None:
            if watch.select(timeout=0.1) and not sys.stdin.buffer.read(1):
                # The service manager owns whole-cgroup termination on main exit.
                return 143
        return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
