"""EPO/USPTO patent data clients and the OKF v0.2 Markdown knowledge base."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("okf-kb-agent")
except PackageNotFoundError:  # running directly from a source checkout
    __version__ = "0.2.0"

__all__ = ["__version__"]
