"""Process-wide state shared by the pytest plugin and the observers."""

import logging
from pathlib import Path

logger = logging.getLogger("grpc_coverage")

# The `pyproject.toml` the pytest plugin found, if the plugin is active in this process.
plugin_config: Path | None = None

# (server, pyproject) of every observer created in this process. Observers created at import time of a conftest exist
# before the plugin's `pytest_configure`, so the plugin checks them again when it sets `plugin_config`.
observers: set[tuple[str, Path | None]] = set()


def mismatch(server: str, pyproject: Path | None) -> str | None:
    """The warning if an observer and the plugin read different `pyproject.toml` files: results would split."""
    if plugin_config is None or pyproject == plugin_config:
        return None
    return (
        f"grpc-coverage: WARNING the observer of {server!r} uses {pyproject}, the pytest plugin uses "
        f"{plugin_config}; results may end up in different places. Pass config= to CoverageObserver or move the "
        f"[tool.grpc-coverage] section"
    )


def register_observer(server: str, pyproject: Path | None) -> None:
    if (server, pyproject) in observers:
        return
    observers.add((server, pyproject))
    message = mismatch(server, pyproject)
    if message:
        logger.warning(message)


def set_plugin_config(pyproject: Path | None) -> list[str]:
    """Returns the warnings for observers created before the plugin was configured (at conftest import)."""
    global plugin_config
    plugin_config = pyproject
    found = (mismatch(server, observer_pyproject) for server, observer_pyproject in sorted(observers, key=str))
    return [message for message in found if message]
