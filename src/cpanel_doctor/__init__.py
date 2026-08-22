"""cPanel Doctor — diagnose and fix known cPanel/WHM problems, intelligently."""
from __future__ import annotations

# Read the version from the installed distribution rather than repeating it here:
# the hand-maintained copy silently drifted (it still said 0.3.1 two releases
# later), so `--version` reported a build that was no longer installed. The
# literal is only a fallback for running straight from a source tree that was
# never installed, and pyproject.toml remains the single source of truth.
try:
    from importlib.metadata import PackageNotFoundError, version as _version

    try:
        __version__ = _version("cpanel-doctor")
    except PackageNotFoundError:  # source checkout, not installed
        __version__ = "0.5.0"
except ImportError:  # pragma: no cover - importlib.metadata is 3.8+
    __version__ = "0.5.0"

__all__ = ["__version__"]
