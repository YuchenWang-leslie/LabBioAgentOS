"""Bounded traceback structure and data-free, immutable-image import verification."""

import json
import math
import re
from uuid import uuid4

from .errors import ContainerStartError


def reported_numerical_condition(exception_type: str, message: str) -> str | None:
    """Recognize complete technical messages; never release text or fitted values."""
    exact = {
        ("LinAlgError", "Singular matrix"): "SINGULAR_MATRIX",
        ("numpy.linalg.LinAlgError", "Singular matrix"): "SINGULAR_MATRIX",
        ("LinAlgError", "SVD did not converge"): "DECOMPOSITION_DID_NOT_CONVERGE",
        ("numpy.linalg.LinAlgError", "SVD did not converge"): "DECOMPOSITION_DID_NOT_CONVERGE",
        ("ValueError", "array must not contain infs or NaNs"): "NON_FINITE_INPUT",
    }
    if (exception_type, message) in exact:
        return exact[exception_type, message]
    if exception_type == "ValueError" and len(message) <= 128:
        match = re.fullmatch(
            r"b(['\"])reciprocal condition number +"
            r"([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)\1", message,
        )
        if match is not None and math.isfinite(float(match.group(2))):
            return "ILL_CONDITIONED_FIT"
    return None


def traceback_chain(stderr: bytes) -> tuple[tuple[str, str | None], ...]:
    """Terminal traceback first, then at most three explicitly linked predecessors."""
    text = stderr[-32_768:].decode("utf-8", errors="replace")
    parts = text.split("Traceback (most recent call last):")
    if len(parts) < 2:
        return ()
    result = [(parts[-1], None)]
    connectors = {
        "The above exception was the direct cause of the following exception:": "DIRECT_CAUSE",
        "During handling of the above exception, another exception occurred:": "CONTEXT",
    }
    for part in reversed(parts[1:-1]):
        body, separator, connector = part.rstrip().rpartition("\n\n")
        if not separator or connector not in connectors or len(result) == 4:
            break
        result.append((body, connectors[connector]))
    return tuple(result)


# This fixed probe reads only Python sources inside the immutable image's own
# installed-library roots. No user mounts, imports of those libraries, or code
# from the traceback are executed. Only AST import identifiers leave the probe.
IMPORT_PROBE = '''import ast, json, pathlib, sys, sysconfig
roots = {pathlib.Path(sysconfig.get_path(key)).resolve() for key in ('purelib', 'platlib')}
modules = set()
for name, line in json.loads(sys.argv[1])[:8]:
    path = pathlib.Path(name)
    try:
        path = path.resolve(strict=True)
        if path.suffix != '.py' or not any(path.is_relative_to(root) for root in roots):
            continue
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            source = stream.read(1048577)
        if len(source) > 1048576:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if getattr(node, 'lineno', None) != line:
                continue
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module)
    except (OSError, ValueError, SyntaxError, RecursionError):
        continue
print(json.dumps(sorted(modules)[:128]))
'''


def verified_image_imports(stderr: bytes, image, runner, docker_binary: str) -> frozenset[str]:
    frames = []
    for text, _ in traceback_chain(stderr):
        if not re.search(r"\nModuleNotFoundError: No module named '[A-Za-z_][A-Za-z0-9_.]{0,127}'\s*$", text):
            continue
        for path, line in re.findall(r'^\s*File "([^"\n]{1,512}\.py)", line ([0-9]{1,9}),', text, re.MULTILINE):
            if path != "/labbio/script.py" and (path, int(line)) not in frames:
                frames.append((path, int(line)))
    if not frames:
        return frozenset()
    argv = (
        docker_binary, "run", "--rm", "--pull", "never", "--name", "labbio-" + uuid4().hex,
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534",
        "--memory", "256m", "--cpus", "1", "--pids-limit", "32",
        "--entrypoint", image.executable[0], image.resolved_reference,
        "-I", "-S", "-c", IMPORT_PROBE, json.dumps(frames[:8]),
    )
    try:
        outcome = runner.run(argv, timeout_seconds=15)
    except (OSError, ContainerStartError):
        return frozenset()
    if outcome.exit_code != 0 or outcome.timed_out or len(outcome.stdout) > 32_768:
        return frozenset()
    try:
        names = json.loads(outcome.stdout)
    except (ValueError, UnicodeError):
        return frozenset()
    if not isinstance(names, list) or len(names) > 128 or any(
        not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", name) is None
        for name in names
    ):
        return frozenset()
    return frozenset(names)
