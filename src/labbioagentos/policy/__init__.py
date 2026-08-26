"""Architecture-neutral policy interfaces."""

from .delegation import DelegationPolicy, InMemoryDelegationPolicy

__all__ = ["DelegationPolicy", "InMemoryDelegationPolicy"]
