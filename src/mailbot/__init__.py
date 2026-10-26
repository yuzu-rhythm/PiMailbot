"""Offline first Discord mailbot prototype.

The package deliberately keeps external integrations behind optional adapters.
The local task, game-credit, attachment, and SQLite features work with the
Python standard library alone.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
