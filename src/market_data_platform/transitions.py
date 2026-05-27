"""Transitional dispatch for data workflows not yet physically migrated here."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TransitionBackend:
    name: str
    repo: str
    executable: str
    module: str
    prefix_args: tuple[str, ...]
    command_env: str
    capability: str


TRANSITION_BACKENDS: dict[str, TransitionBackend] = {}


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _checkout_command(backend: TransitionBackend) -> list[str] | None:
    repo_root = _workspace_root() / backend.repo
    source_root = repo_root / "src"
    if not source_root.is_dir():
        return None
    python_candidates = (
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "Scripts" / "python.exe",
    )
    python = next((candidate for candidate in python_candidates if candidate.is_file()), None)
    return [str(python or Path(sys.executable)), "-m", backend.module]


def _checkout_environment(backend: TransitionBackend) -> dict[str, str] | None:
    source_root = _workspace_root() / backend.repo / "src"
    if not source_root.is_dir():
        return None
    env = dict(os.environ)
    existing = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{existing}" if existing else str(source_root)
    )
    return env


def resolve_transition_command(backend_name: str) -> list[str] | None:
    backend = TRANSITION_BACKENDS[backend_name]
    override = str(os.environ.get(backend.command_env) or "").strip()
    if override:
        return shlex.split(override)
    checkout_command = _checkout_command(backend)
    if checkout_command is not None:
        return checkout_command
    installed_command = shutil.which(backend.executable)
    return [installed_command] if installed_command else None


def transition_status() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for backend in TRANSITION_BACKENDS.values():
        command = resolve_transition_command(backend.name)
        rows.append(
            {
                "name": backend.name,
                "status": "transition_backend",
                "backend_repo": backend.repo,
                "capability": backend.capability,
                "platform_command": f"marketdata rqdata {backend.name} -- <backend-args>",
                "command_env": backend.command_env,
                "resolved_command": command,
                "available": command is not None,
            }
        )
    return rows


def run_transition_backend(
    backend_name: str,
    argv: Sequence[str],
    *,
    runner: Any = subprocess.run,
) -> int:
    backend = TRANSITION_BACKENDS[backend_name]
    command = resolve_transition_command(backend_name)
    if command is None:
        raise RuntimeError(
            f"Transition backend '{backend_name}' is unavailable. Install or sync "
            f"{backend.repo}, or set {backend.command_env} to its executable command."
        )
    has_override = bool(str(os.environ.get(backend.command_env) or "").strip())
    env = None if has_override else _checkout_environment(backend)
    completed = runner([*command, *backend.prefix_args, *argv], check=False, env=env)
    return int(completed.returncode)
