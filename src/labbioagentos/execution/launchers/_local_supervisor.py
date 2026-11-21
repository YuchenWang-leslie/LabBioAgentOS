"""Service main process: verify cgroup limits and watch the caller's lifetime pipe.

This standalone stdlib-only entrypoint runs inside a systemd transient service.
Returning from its main process makes KillMode=control-group stop descendants,
including children that detach from the original process group.
"""

import errno
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

# Must match SCRATCH_POLL_SECONDS in base.py; this file imports no LabBio module.
SCRATCH_POLL_SECONDS = 5


def read_limits(names=("cpu.max", "memory.max", "pids.max")):
    membership = Path("/proc/self/cgroup").read_text().splitlines()
    relative = next(line[3:] for line in membership if line.startswith("0::"))
    root = Path("/sys/fs/cgroup")
    directory = root / relative.lstrip("/")
    if not directory.is_relative_to(root) or ".." in directory.parts:
        raise ValueError("Invalid cgroup membership")
    return {name: (directory / name).read_text().strip() for name in names}


def enforced(limits, cpus, memory_bytes, pids):
    try:
        quota, period = map(int, limits["cpu.max"].split())
        memory, tasks = int(limits["memory.max"]), int(limits["pids.max"])
        return (0 < quota / period <= cpus and 0 < memory <= memory_bytes and 0 < tasks <= pids)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def used_bytes(root):
    """Allocated bytes under root on its own filesystem; links are never followed."""
    total, pending = 0, [root]
    try:
        device = os.lstat(root).st_dev
    except OSError:
        return 0
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            continue  # Removed or unreadable during the walk.
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if info.st_dev != device:
                continue
            total += info.st_blocks * 512
            if entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)
    return total


def over_limit(limit, paths):
    return limit > 0 and sum(used_bytes(path) for path in paths) > limit


def stop(child):
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def read_affinity_limits():
    limits = read_limits(("memory.max", "memory.swap.max", "pids.max"))
    affinity = sorted(os.sched_getaffinity(0))
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
                  if ":" in line)
    locked = False
    try:
        # Denying even a no-op proves mutation is blocked, without widening the mask
        # if the configured syscall filter was silently ignored.
        os.sched_setaffinity(0, set(affinity))
    except OSError as exc:
        locked = exc.errno == errno.EPERM
    return {**limits, "cpu.affinity": affinity, "cpu.affinity_locked": locked,
            "no_new_privs": status.get("NoNewPrivs", "").strip() == "1",
            "seccomp_filter": status.get("Seccomp", "").strip() == "2"}


def affinity_enforced(limits, cpus, memory_bytes, pids):
    try:
        affinity = limits["cpu.affinity"]
        return (float(cpus).is_integer() and isinstance(affinity, list)
                and 0 < len(affinity) <= cpus and len(set(affinity)) == len(affinity)
                and all(type(cpu) is int and cpu >= 0 for cpu in affinity)
                and limits["cpu.affinity_locked"] is True
                and limits["no_new_privs"] is True and limits["seccomp_filter"] is True
                and 0 < int(limits["memory.max"]) <= memory_bytes
                and int(limits["memory.swap.max"]) == 0
                and 0 < int(limits["pids.max"]) <= pids)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def main():
    cpus, memory_bytes, pids = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
    # Scratch limit in bytes (0: unwatched), the marker path, the watched paths, then the
    # supervisor options and the command.
    limit, marker, count = int(sys.argv[4]), sys.argv[5], int(sys.argv[6])
    scratch, command = sys.argv[7:7 + count], sys.argv[7 + count:]
    try:
        if command[:1] == ["--systemd-affinity"]:
            expected = [int(cpu) for cpu in command[1].split(",")]
            command = command[2:]
            limits = read_affinity_limits()
            valid = (limits["cpu.affinity"] == expected
                     and affinity_enforced(limits, cpus, memory_bytes, pids))
        else:
            limits = read_limits()
            valid = enforced(limits, cpus, memory_bytes, pids)
        if not valid:
            raise ValueError("Resource limits are absent or exceed the requested limits")
    except (OSError, StopIteration, ValueError, IndexError, KeyError, AttributeError):
        print("LOCAL_RESOURCE_ENFORCEMENT_UNAVAILABLE", file=sys.stderr, flush=True)
        return 125
    if command == ["--probe"]:
        print(json.dumps(limits))
        return 0
    # Check parent lifetime before starting anything with side effects.
    with selectors.DefaultSelector() as watch:
        watch.register(sys.stdin, selectors.EVENT_READ)
        if watch.select(timeout=0) and not sys.stdin.buffer.read(1):
            return 143
        try:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        except OSError:
            print("LOCAL_COMMAND_START_FAILED", file=sys.stderr, flush=True)
            return 127
        checked = time.monotonic()
        while child.poll() is None:
            if watch.select(timeout=0.1) and not sys.stdin.buffer.read(1):
                # The service manager owns whole-cgroup termination on main exit.
                return 143
            if time.monotonic() - checked >= SCRATCH_POLL_SECONDS:
                checked = time.monotonic()
                if over_limit(limit, scratch):
                    Path(marker).touch()
                    stop(child)
                    return 137
        return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
