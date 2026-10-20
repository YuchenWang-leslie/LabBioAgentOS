"""Exec-time limits, without Python callbacks in a multithreaded fork."""

import argparse
import ctypes
import os
import resource
import signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file-bytes", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--nice", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_FSIZE, (args.file_bytes, args.file_bytes))
    if args.cpu_seconds is not None:
        resource.setrlimit(resource.RLIMIT_CPU, (args.cpu_seconds, args.cpu_seconds))
    if args.nice is not None:
        os.nice(args.nice)
    if args.parent_pid is not None:
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Could not set parent-death signal")
        if os.getppid() != args.parent_pid:
            return 143
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    raise SystemExit(main())
