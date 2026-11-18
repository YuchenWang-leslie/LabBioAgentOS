"""Finite projection of a loaded terminal systemd unit before it is reset."""

import re
from collections.abc import Mapping

from .models import ExecutionHostTermination


def _number(value, maximum):
    if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]{0,18}", value) is None:
        return None
    parsed = int(value)
    return parsed if parsed <= maximum else None


def systemd_host_termination(
    state: Mapping[str, str], *, stop_requested: bool,
) -> ExecutionHostTermination | None:
    # systemctl can return success/0 defaults after a transient unit disappears.
    if (state.get("LoadState") != "loaded"
            or state.get("ActiveState") not in {"inactive", "failed"}
            or state.get("MainPID") != "0"):
        return None
    result = state.get("Result")
    if not isinstance(result, str) or result not in {
        "success", "exit-code", "signal", "core-dump", "timeout",
        "watchdog", "oom-kill", "resources",
    }:
        result = None
    raw_code = state.get("ExecMainCode")
    code = ({"1": "EXITED", "2": "KILLED", "3": "DUMPED"}.get(raw_code)
            if isinstance(raw_code, str) else None)
    status = _number(state.get("ExecMainStatus"), 255) if code is not None else None
    if code in {"KILLED", "DUMPED"} and status is not None and not 1 <= status <= 64:
        status = None
    peak = _number(state.get("MemoryPeak"), 9_223_372_036_854_775_807)
    if all(value is None for value in (result, code, status, peak)):
        return None
    return ExecutionHostTermination(
        service_result=result, service_main_code=code, service_main_status=status,
        memory_peak_bytes=peak, stop_requested_by_launcher=stop_requested,
    )
