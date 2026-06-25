"""Patch abstraction.

A *patch* fixes one known cPanel problem. It is composed of independent
*components* (a file, a service, a config edit, a hook ...). Each component knows
how to check whether it is in place, how to apply itself and how to remove
itself. Building patches out of components is what makes the doctor "intelligent":

* full status is derived from per-component checks;
* a patch reset by a cPanel update shows up as :data:`PatchState.DRIFTED`
  (some components present, some missing) and can be re-applied surgically;
* removal is just every component undoing itself, in reverse.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Tuple

from .runner import Runner


class PatchState(str, Enum):
    NOT_APPLICABLE = "not-applicable"   # the problem cannot occur on this host
    NOT_APPLIED = "not-applied"         # applicable, nothing applied
    DRIFTED = "drifted"                 # partially applied (e.g. reset by an update)
    APPLIED = "applied"                 # fully applied

    @property
    def label(self) -> str:
        return {
            PatchState.NOT_APPLICABLE: "n/a",
            PatchState.NOT_APPLIED: "NOT APPLIED",
            PatchState.DRIFTED: "DRIFTED",
            PatchState.APPLIED: "APPLIED",
        }[self]

    @property
    def color(self) -> str:
        return {
            PatchState.NOT_APPLICABLE: "grey50",
            PatchState.NOT_APPLIED: "red",
            PatchState.DRIFTED: "yellow",
            PatchState.APPLIED: "green",
        }[self]


@dataclass
class Component:
    """One installable piece of a patch."""

    key: str
    description: str
    present: Callable[[Runner], bool]
    apply: Callable[[Runner], None]
    remove: Callable[[Runner], None]


@dataclass
class ComponentStatus:
    key: str
    description: str
    present: bool


class Patch(abc.ABC):
    #: stable machine id, e.g. ``"pg-cpses"``
    id: str = ""
    #: human title
    name: str = ""
    #: one-line summary
    summary: str = ""
    #: longer explanation (markdown-ish, shown in the TUI detail pane)
    description: str = ""
    #: extra warning shown before applying (e.g. security implications)
    caveats: str = ""

    # -- to implement -------------------------------------------------------
    @abc.abstractmethod
    def components(self) -> List[Component]:
        ...

    def applicable(self, runner: Runner) -> Tuple[bool, str]:
        """Return ``(is_applicable, reason)``. Default: always applicable."""
        return True, ""

    def self_test(self, runner: Runner) -> Tuple[bool, str]:
        """Optional functional check after applying. Default: not implemented."""
        return True, "no self-test"

    # -- derived ------------------------------------------------------------
    def component_statuses(self, runner: Runner) -> List[ComponentStatus]:
        return [
            ComponentStatus(c.key, c.description, bool(c.present(runner)))
            for c in self.components()
        ]

    def state(self, runner: Runner) -> PatchState:
        ok, _ = self.applicable(runner)
        if not ok:
            return PatchState.NOT_APPLICABLE
        statuses = self.component_statuses(runner)
        present = [s.present for s in statuses]
        if all(present):
            return PatchState.APPLIED
        if any(present):
            return PatchState.DRIFTED
        return PatchState.NOT_APPLIED

    # -- actions ------------------------------------------------------------
    def apply(self, runner: Runner) -> None:
        """Apply only the components that are missing (idempotent / fixes drift)."""
        for comp in self.components():
            if comp.present(runner):
                runner.note(f"[skip] {comp.key}: already present")
                continue
            runner.note(f"[apply] {comp.key}: {comp.description}")
            comp.apply(runner)

    def remove(self, runner: Runner) -> None:
        """Remove every component, in reverse order."""
        for comp in reversed(self.components()):
            if not comp.present(runner):
                runner.note(f"[skip] {comp.key}: not present")
                continue
            runner.note(f"[remove] {comp.key}: {comp.description}")
            comp.remove(runner)
