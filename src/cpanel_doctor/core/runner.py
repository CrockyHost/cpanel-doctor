"""Side-effecting operations (command execution + filesystem) with dry-run support.

Every mutating action goes through :class:`Runner` so that patches stay declarative
and so that ``--dry-run`` can preview *exactly* what would change without touching
the system. Each action is recorded and can be streamed to a callback (used by the
TUI to show a live activity log).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence


# cPanel runs hooks (and thus `reapply`) with a minimal PATH that omits the sbin
# dirs, so bare tools like `ip` and `systemctl` fail with FileNotFoundError. Every
# subprocess we spawn gets a PATH that always includes the standard admin dirs so
# patches can call system tools by name without each one hard-coding absolute paths.
_STD_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/sbin", "/usr/bin", "/bin")


def _augmented_env() -> dict:
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(":") if p]
    for d in _STD_BIN_DIRS:
        if d not in parts:
            parts.append(d)
    env["PATH"] = ":".join(parts)
    return env


@dataclass
class Action:
    kind: str          # "run" | "write" | "remove" | "chmod" | "backup" | "restore" | "note"
    target: str
    detail: str = ""
    ok: bool = True
    output: str = ""


class RunnerError(RuntimeError):
    pass


class Runner:
    """Executes (or, in dry-run, only records) system changes."""

    def __init__(
        self,
        dry_run: bool = False,
        on_action: Optional[Callable[[Action], None]] = None,
    ) -> None:
        self.dry_run = dry_run
        self._on_action = on_action
        self.actions: List[Action] = []

    # -- bookkeeping --------------------------------------------------------
    def _record(self, action: Action) -> Action:
        self.actions.append(action)
        if self._on_action:
            self._on_action(action)
        return action

    def note(self, message: str) -> None:
        self._record(Action(kind="note", target="", detail=message))

    # -- read-only helpers (always real, even in dry-run) -------------------
    @staticmethod
    def exists(path: str) -> bool:
        return os.path.exists(path)

    @staticmethod
    def read(path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    @staticmethod
    def capture(cmd: Sequence[str], timeout: int = 30) -> "subprocess.CompletedProcess[str]":
        """Run a *read-only* command and capture output (runs even in dry-run)."""
        return subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_augmented_env(),
        )

    # -- mutating operations ------------------------------------------------
    def run(self, cmd: Sequence[str], timeout: int = 120, check: bool = True) -> Action:
        pretty = " ".join(cmd)
        if self.dry_run:
            return self._record(Action("run", pretty, detail="(dry-run)"))
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False,
            env=_augmented_env(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        action = Action("run", pretty, ok=proc.returncode == 0, output=out.strip())
        self._record(action)
        if check and proc.returncode != 0:
            raise RunnerError(f"command failed ({proc.returncode}): {pretty}\n{out.strip()}")
        return action

    def write(self, path: str, content: str, mode: int = 0o644) -> Action:
        action = Action("write", path, detail=f"{len(content)} bytes, mode {oct(mode)}")
        if not self.dry_run:
            os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.chmod(path, mode)
        return self._record(action)

    def chmod(self, path: str, mode: int) -> Action:
        if not self.dry_run and os.path.exists(path):
            os.chmod(path, mode)
        return self._record(Action("chmod", path, detail=oct(mode)))

    def remove(self, path: str) -> Action:
        if not self.dry_run and os.path.exists(path):
            os.remove(path)
        return self._record(Action("remove", path))

    def backup(self, path: str, suffix: str = ".orig") -> Action:
        """Copy ``path`` to ``path+suffix`` once (never overwrites an existing backup)."""
        dst = path + suffix
        if not self.dry_run and os.path.exists(path) and not os.path.exists(dst):
            shutil.copy2(path, dst)
        return self._record(Action("backup", path, detail=f"-> {dst}"))

    def restore(self, path: str, suffix: str = ".orig") -> Action:
        src = path + suffix
        if not self.dry_run and os.path.exists(src):
            shutil.move(src, path)
        return self._record(Action("restore", path, detail=f"<- {src}"))
