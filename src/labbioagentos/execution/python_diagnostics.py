"""Runtime-neutral projection of Python failures into identifiers and script coordinates."""

from __future__ import annotations

import ast
import builtins
import re

from .diagnostic_sources import reported_numerical_condition, traceback_chain
from .models import (
    ExecutionDiagnostic,
    ExecutionDiagnosticCode,
    ExecutionScriptLocation,
)


SAFE_PYTHON_EXCEPTION_TYPES = frozenset(
    value.__name__
    for value in vars(builtins).values()
    if isinstance(value, type)
    and issubclass(value, Exception)
    and value.__module__ == "builtins"
) | {"LinAlgError", "numpy.linalg.LinAlgError"}


def safe_python_diagnostics(
    stderr: bytes,
    *,
    script_content: str = "",
    verified_imports: frozenset[str] = frozenset(),
) -> tuple[ExecutionDiagnostic, ...]:
    """Extract identifiers and Agent-script line numbers, never raw stderr."""

    diagnostics = []
    for text, relation in traceback_chain(stderr):
        items = safe_python_exception(text, script_content, verified_imports)
        if not items:
            break  # Never attach a cause across an unrecognized exception.
        diagnostics.append(items[0].model_copy(update={"chain_relation": relation}))
    return tuple(diagnostics)


def safe_python_exception(
    text: str, script_content: str, verified_imports: frozenset[str],
) -> tuple[ExecutionDiagnostic, ...]:
    lines = text.rstrip().splitlines()
    if not lines:
        return ()
    terminal_line = lines[-1]
    terminal_exception = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_.]{0,127})(?::.*)?", terminal_line
    )
    if (
        terminal_exception is None
        or terminal_exception.group(1) not in SAFE_PYTHON_EXCEPTION_TYPES
    ):
        return ()
    exception_type = terminal_exception.group(1)

    # Keep complete-message matching distinct from traceback whitespace parsing.
    raw_terminal = text.rstrip("\r\n").split("\n")[-1]
    numerical_condition = reported_numerical_condition(
        exception_type, raw_terminal.removeprefix(exception_type + ": "),
    )
    line_numbers = tuple(
        dict.fromkeys(
            int(match)
            for match in re.findall(
                r'^\s*File "/labbio/script\.py", line ([0-9]{1,9}),',
                text,
                flags=re.MULTILINE,
            )
        )
    )[-16:]
    missing_module = re.fullmatch(
        r"ModuleNotFoundError: No module named "
        r"'([A-Za-z_][A-Za-z0-9_.]{0,127})'",
        terminal_line,
    )
    if missing_module is not None:
        imported_modules: set[str] = set(verified_imports)
        try:
            tree = ast.parse(script_content)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    imported_modules.add(node.module)
        module_name = missing_module.group(1)
        if any(
            module_name == imported
            or module_name.startswith(imported + ".")
            or imported.startswith(module_name + ".")
            for imported in imported_modules
        ):
            return (
                ExecutionDiagnostic(
                    code=ExecutionDiagnosticCode.PYTHON_MODULE_NOT_FOUND,
                    exception_type="ModuleNotFoundError",
                    script_line_numbers=line_numbers,
                    missing_module=module_name,
                ),
            )
    script_locations = script_error_locations(text, script_content)
    missing_key_type = None
    missing_key_locations = []
    if exception_type == "KeyError":
        match = re.fullmatch(r"KeyError: (.{1,512})", terminal_line)
        if match is not None:
            try:
                argument = ast.literal_eval(match.group(1))
            except (ValueError, SyntaxError, RecursionError):
                pass
            else:
                # Finite technical type vocabulary only; never serialize values.
                if type(argument) in (str, bytes, int, float, bool, type(None), tuple):
                    missing_key_type = type(argument).__name__
                    try:
                        tree = ast.parse(script_content)
                    except (SyntaxError, RecursionError):
                        tree = None
                    if tree is not None:
                        for node in ast.walk(tree):
                            if not isinstance(node, ast.Constant) or (
                                type(node.value) is not type(argument) or node.value != argument
                            ):
                                continue
                            # Existing highlights verify exact displayed source
                            # and ASCII coordinates. No dynamic variable value
                            # is inspected or copied from the process stream.
                            if any(
                                node.lineno == node.end_lineno == loc.line_number
                                and loc.start_column <= node.col_offset < node.end_col_offset <= loc.end_column
                                for loc in script_locations
                            ):
                                missing_key_locations.append(ExecutionScriptLocation(
                                    line_number=node.lineno,
                                    start_column=node.col_offset, end_column=node.end_col_offset,
                                ))
    reported_index_condition = None
    if exception_type == "IndexError":
        # Match a complete bounded message, not task/source keywords. Keep
        # trailing spaces significant and discard every captured number.
        index_line = text.rstrip("\r\n").split("\n")[-1]
        match = re.fullmatch(
            r"IndexError: index -?(?:0|[1-9][0-9]{0,18}) is out of bounds "
            r"for axis (?:0|[1-9][0-9]{0,18}) with size (0|[1-9][0-9]{0,18})",
            index_line,
        )
        if match is not None:
            reported_index_condition = (
                "OUT_OF_BOUNDS_EMPTY_AXIS" if match.group(1) == "0"
                else "OUT_OF_BOUNDS"
            )
        elif index_line in {
            "IndexError: list index out of range",
            "IndexError: tuple index out of range",
            "IndexError: string index out of range",
        }:
            reported_index_condition = "OUT_OF_BOUNDS"
    return (
        ExecutionDiagnostic(
            code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
            exception_type=exception_type,
            script_line_numbers=line_numbers,
            script_error_locations=script_locations,
            missing_key_type=missing_key_type,
            missing_key_source_locations=tuple(missing_key_locations[:16]),
            reported_index_condition=reported_index_condition,
            reported_numerical_condition=numerical_condition,
        ),
    )


def script_error_locations(
    text: str, script_content: str
) -> tuple[ExecutionScriptLocation, ...]:
    """Keep caret coordinates only when the displayed source matches the script."""

    source_lines = script_content.splitlines()
    locations: list[ExecutionScriptLocation] = []
    for match in re.finditer(
        r'^\s*File "/labbio/script\.py", line ([0-9]{1,9}),[^\n]*\n'
        r'    ([^\n]*)\n    ([ ~^]*\^[ ~^]*)$',
        text,
        flags=re.MULTILINE,
    ):
        line_number = int(match.group(1))
        if not 1 <= line_number <= len(source_lines):
            continue
        source = source_lines[line_number - 1]
        if match.group(2) != source.strip():
            continue
        # Display-width and tab expansion are not character coordinates.
        if not source.isascii() or "\t" in source:
            continue
        markers = match.group(3).rstrip()
        indent = len(source) - len(source.lstrip())
        start = indent + len(markers) - len(markers.lstrip())
        end = indent + len(markers)
        if not 0 <= start < end <= min(len(source), 262_144):
            continue
        location = ExecutionScriptLocation(
            line_number=line_number, start_column=start, end_column=end
        )
        if location not in locations:
            locations.append(location)
    return tuple(locations[-16:])
