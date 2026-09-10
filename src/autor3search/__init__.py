"""Autonomous AI-driven performance optimization for Python repositories."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    # Must match the distribution name in pyproject.toml, not the import
    # package: a mismatch raises PackageNotFoundError and silently degrades
    # every install to "0.0.0+unknown", which results.tsv then records.
    __version__ = _version("autor3search")
except PackageNotFoundError:  # running from a source checkout, not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
