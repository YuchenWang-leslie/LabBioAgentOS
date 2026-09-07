"""Allow the same local entrypoint through python -m labbioagentos."""

from .cli import entrypoint

raise SystemExit(entrypoint())
