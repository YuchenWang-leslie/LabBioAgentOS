"""Isolated R2 process boundary; not a production recovery scheduler.

Only this owner holds the run lease. A stable service identity lets a replacement
retire an orphan before it starts a new worker. Cancellation failure propagates;
it is not a successful stop or permission to dispatch a replacement.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys
from uuid import uuid5

from labbioagentos import RequestedResources
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.launchers.base import JobSpec
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher
from labbioagentos.local_continuation import run_writer

from round_trip import RUN_ID, run_round


def owner():
    return SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1048576),
                               resource_enforcement="systemd-affinity")


def worker_id(root):
    return uuid5(RUN_ID, str(root.resolve()))


def command(script, root, *extra):
    # The launcher's clean environment deliberately does not inherit PYTHONPATH.
    source = Path(__file__).resolve().parents[2] / "src"
    bootstrap = ("import runpy,sys;from pathlib import Path;sys.path.insert(0,sys.argv.pop(1));"
                 "script=sys.argv.pop(1);sys.path.insert(0,str(Path(script).parent));"
                 "runpy.run_path(script,run_name='__main__')")
    return (sys.executable, "-c", bootstrap, str(source), str(script), str(root), *extra)


@asynccontextmanager
async def worker(root, argv, *, timeout=90):
    root = root.absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with run_writer(root):
        launcher = owner()
        previous = await launcher.find(worker_id(root))
        if previous is not None:
            await launcher.cancel(previous)
            launcher = owner()  # Reconnected stop state must not describe the new job.
        handle = await launcher.submit(JobSpec(
            worker_id(root), argv, timeout,
            RequestedResources(cpus=4, memory_mb=4096, pids_limit=128,
                               timeout_seconds=timeout), root,
        ))
        try:
            yield launcher, handle
        finally:
            # Hold the lease until the existing launcher confirms tree termination.
            cleanup = asyncio.create_task(launcher.cancel(handle))
            interrupted = False
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError


async def run(root, version, request, fault):
    argv = command(Path(__file__), root, "--child", "--version", version, "--request", request)
    if fault:
        argv += ("--fault", fault)
    async with worker(root, argv) as (launcher, handle):
        await launcher.wait(handle)
        outcome = await launcher.collect(handle)
        sys.stdout.buffer.write(outcome.stdout)
        sys.stderr.buffer.write(outcome.stderr)
        return outcome.exit_code if outcome.exit_code is not None else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--version", default="r2-prototype-1")
    parser.add_argument("--request", default="synthetic")
    parser.add_argument("--fault", choices=("provider-return-before-native", "provider-native-before-dbos",
                                          "tool-intent-before-submit", "tool-accepted-before-receipt",
                                          "tool-native-before-dbos"))
    args = parser.parse_args()
    if args.child:
        if args.fault:
            os.environ["R2_CRASH_POINT"] = args.fault
        run_round(args.root.absolute(), args.version, args.request)
    else:
        raise SystemExit(asyncio.run(run(args.root, args.version, args.request, args.fault)))
