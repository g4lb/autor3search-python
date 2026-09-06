"""Autonomous AI-driven performance optimization for Python repositories."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("autor3search-python")
except PackageNotFoundError:  # running from a source checkout, not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
