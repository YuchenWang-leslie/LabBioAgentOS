"""Opt-in bounded process-error evidence, not an error-to-repair classifier."""

import hashlib
import re

from .models import ExecutionErrorContext


def bounded_error_context(stderr: bytes) -> ExecutionErrorContext | None:
    """Release the traceback or error tail without stdout or variable inspection.

    Redaction is best effort, not a guarantee that arbitrary exception text is
    free of data values. Deployment authorization is required before using this
    projection. Process text is untrusted evidence, never a tool instruction.
    """
    if not stderr.strip():
        return None
    tail = stderr[-65_536:]
    truncated = len(tail) != len(stderr)
    text = tail.decode("utf-8", errors="replace")
    if truncated:
        # Do not expose an arbitrary partial line at the scan boundary.
        text = text.partition("\n")[2]
    start = text.find("Traceback (most recent call last):")
    source = "PYTHON_TRACEBACK" if start >= 0 else "STDERR_TAIL"
    if start >= 0:
        text = text[start:]
    original = text
    text = redact_process_text(text)
    redacted = text != original
    # Retain the terminal exception; make all length loss explicit.
    lines = text.splitlines()
    truncated |= len(lines) > 80 or any(len(line) > 1200 for line in lines)
    text = "\n".join(line[:1200] for line in lines[-80:])
    truncated |= len(text) > 6000
    text = text[-6000:]
    return ExecutionErrorContext(
        source=source, text=text, truncated=truncated, redacted=redacted,
        stderr_sha256=hashlib.sha256(stderr).hexdigest(),
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
    text = re.sub(r'''(?<![\w])(?:[A-Za-z]:[\\/]|/|~[/\\])[^\s'"<>),;]+''',
                  "[REDACTED_PATH]", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]", "", text)
    return text
