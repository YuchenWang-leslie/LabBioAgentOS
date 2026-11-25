"""Opt-in bounded process-error evidence, not an error-to-repair classifier."""

import hashlib
import re

from .models import ExecutionErrorContext

_SCAN_BYTES = 65_536
_LINE_CHARS = 1200
_TRACEBACK = "Traceback (most recent call last):"
_OMISSION = "[oversized line omitted]"
_EXCEPTION = re.compile(r"((?:[A-Za-z_]\w*\.)*[A-Z]\w*):", re.ASCII)
_FRAME = re.compile(r'(?m)^[ \t]+File "[^"\r\n]{1,1024}", line [0-9]{1,12}\b')
_KEY_BOUNDARY = re.compile(rb"-----(BEGIN|END) [^-\r\n]*PRIVATE KEY-----")


def _omitted_line(prefix: str, has_traceback: bool) -> str:
    match = _EXCEPTION.match(prefix[:128]) if has_traceback else None
    return f"{match[1]}: {_OMISSION}" if match else _OMISSION


def _inside_private_key(stderr: bytes, offset: int) -> bool:
    # Preserve multiline redaction when a scan or omitted line splits a PEM.
    is_open = False
    for match in _KEY_BOUNDARY.finditer(stderr, 0, offset):
        is_open = match[1] == b"BEGIN"
    return is_open


def _scan_tail(stderr: bytes) -> tuple[str, bool]:
    """Decode bounded windows. Never copy a partial data line to the model."""
    cut = max(0, len(stderr) - _SCAN_BYTES)
    if not cut:
        return stderr.decode("utf-8", errors="replace"), False

    resume = cut
    prefix = ""
    omitted = False
    if stderr[cut - 1] != ord("\n"):
        start = stderr.rfind(b"\n", 0, cut) + 1
        end = stderr.find(b"\n", cut)
        resume = end + 1 if end >= 0 else len(stderr)
        if resume - start > _LINE_CHARS:
            # The byte window intersects a long record. Recover only traceback
            # frame locations from its bounded prelude, never source expressions.
            window = max(0, start - _SCAN_BYTES)
            prelude = stderr[window:start].decode("utf-8", errors="replace")
            if _inside_private_key(stderr, window):
                prelude = "-----BEGIN PRIVATE KEY-----\n" + prelude
            prelude = redact_process_text(prelude)
            header = prelude.rfind(_TRACEBACK)
            if header >= 0:
                prefix = "\n".join([_TRACEBACK, *_FRAME.findall(prelude, header)]) + "\n"
            name = stderr[start:start + 128].decode("ascii", errors="replace")
            show_type = header >= 0 and not _inside_private_key(stderr, start)
            prefix += _omitted_line(name, show_type) + "\n"
            omitted = True
    if _inside_private_key(stderr, resume):
        prefix += "-----BEGIN PRIVATE KEY-----\n"
    return prefix + stderr[resume:].decode("utf-8", errors="replace"), omitted


def bounded_error_context(stderr: bytes, *, raw_stderr: bytes | None = None) -> ExecutionErrorContext | None:
    """Release the traceback or error tail without stdout or variable inspection.

    Redaction is best effort, not a guarantee that arbitrary exception text is
    free of data values. Deployment authorization is required before using this
    projection. Process text is untrusted evidence, never a tool instruction.
    """
    if not stderr.strip():
        return None
    # Backend filtering may precede this projection; bind the retained RAW log.
    raw_stderr = stderr if raw_stderr is None else raw_stderr
    scanned, omitted = _scan_tail(stderr)
    text = redact_process_text(scanned)
    redacted = omitted or text != scanned or stderr != raw_stderr
    truncated = len(stderr) > _SCAN_BYTES
    start = text.find(_TRACEBACK)
    source = "PYTHON_TRACEBACK" if start >= 0 else "STDERR_TAIL"
    if start >= 0:
        text = text[start:]
    lines = text.splitlines()
    truncated |= len(lines) > 80
    kept = []
    for line in lines[-80:]:
        if len(line) > _LINE_CHARS:
            line = _omitted_line(line, start >= 0)
            truncated = redacted = True
        kept.append(line)
    text = "\n".join(kept)
    if len(text) > 6000:
        # Bound the receipt at a complete line; each individual line fits.
        cut = len(text) - 6000
        text = text[cut:] if text[cut - 1] == "\n" else text[text.index("\n", cut) + 1:]
        truncated = True
    return ExecutionErrorContext(
        source=source, text=text, truncated=truncated, redacted=redacted,
        stderr_sha256=hashlib.sha256(raw_stderr).hexdigest(),
    )


def redact_process_text(text: str) -> str:
    """Best-effort secret/path redaction without truncating diagnostic content."""

    def frame(match):
        path = match.group(1).replace("\\", "/")
        if path == "/labbio/script.py":
            name = "<submitted_script>"
        elif "/site-packages/" in path:
            name = path.split("/site-packages/", 1)[1].replace("/", ".")
        else:
            name = path.rsplit("/", 1)[-1]
        return f'File "{name}", line {match.group(2)}'

    text = re.sub(r'File "([^"\n]+)", line ([0-9]+)', frame, text)
    text = re.sub(r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?(?:-----END [^-\n]*PRIVATE KEY-----|\Z)",
                  "[REDACTED_KEY]", text, flags=re.DOTALL)
    text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r'''(?ix)(["']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization|credential)["']?\s*[:=]\s*)
        (?:"[^"\n]*"|'[^'\n]*'|[^\s,;]+)''',
        r"\1[REDACTED]", text,
    )
    text = re.sub(r"(?i)\b(?:https?|ftp)://[^\s\"'<>]+", "[REDACTED_URL]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b",
                  "[REDACTED_TOKEN]", text)
    # A slash inside a signed identifier is not an absolute POSIX path start.
    text = re.sub(r'''(?<![\w])(?:[A-Za-z]:[\\/]|(?<![+-])/|~[/\\])[^\s'"<>),;]+''',
                  "[REDACTED_PATH]", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text
