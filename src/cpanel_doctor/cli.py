"""Command-line interface for cPanel Doctor."""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from rich.console import Console
from rich.table import Table

from . import __version__, hook
from .core import Action, Patch, PatchState, Runner, load_patches, patches_by_id

console = Console()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_root() -> None:
    if os.geteuid() != 0:
        console.print("[red]This action needs root.[/] Re-run with sudo.")
        raise SystemExit(2)


def _runner(args: argparse.Namespace) -> Runner:
    def echo(a: Action) -> None:
        if a.kind == "note":
            console.print(f"  [dim]{a.detail}[/]")
        else:
            mark = "[green]✓[/]" if a.ok else "[red]✗[/]"
            line = f"  {mark} {a.kind} [cyan]{a.target}[/]"
            if a.detail:
                line += f" [dim]{a.detail}[/]"
            console.print(line)
            if a.output:
                console.print(f"     [dim]{a.output}[/]")

    return Runner(dry_run=getattr(args, "dry_run", False), on_action=echo)


def _resolve(ids: List[str]) -> List[Patch]:
    registry = patches_by_id()
    if not ids or ids == ["all"]:
        return load_patches()
    chosen = []
    for pid in ids:
        if pid not in registry:
            console.print(f"[red]Unknown patch:[/] {pid}")
            raise SystemExit(2)
        chosen.append(registry[pid])
    return chosen


def _state(patch: Patch, runner: Runner) -> PatchState:
    return patch.state(runner)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_list(args: argparse.Namespace) -> int:
    runner = Runner()
    table = Table(title="cPanel Doctor — patches", title_style="bold")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Name")
    for patch in load_patches():
        st = _state(patch, runner)
        table.add_row(patch.id, f"[{st.color}]{st.label}[/]", patch.name)
    console.print(table)
    h_ok, h_msg = hook.status(runner)
    color = "green" if h_ok else "yellow"
    console.print(f"\npost-upcp self-heal hook: [{color}]{h_msg}[/]")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    runner = Runner()
    for patch in _resolve(args.id):
        st = _state(patch, runner)
        console.print(f"\n[bold cyan]{patch.id}[/] — {patch.name}")
        console.print(f"  state: [{st.color}]{st.label}[/]")
        applicable, reason = patch.applicable(runner)
        if not applicable:
            console.print(f"  [grey50]not applicable: {reason}[/]")
            continue
        for cs in patch.component_statuses(runner):
            mark = "[green]●[/]" if cs.present else "[red]○[/]"
            console.print(f"    {mark} {cs.key:<12} {cs.description}")
    return 0


def _act(args: argparse.Namespace, action: str) -> int:
    _require_root()
    runner = _runner(args)
    rc = 0
    for patch in _resolve(args.id):
        applicable, reason = patch.applicable(runner)
        st = patch.state(runner)
        if action != "remove" and not applicable:
            console.print(f"[grey50]skip {patch.id}: {reason}[/]")
            continue
        console.print(f"\n[bold]{action.capitalize()} {patch.id}[/] (currently {st.label})")
        if action in ("apply", "reapply") and patch.caveats and not args.dry_run:
            console.print(f"  [yellow]⚠ {patch.caveats}[/]")
        try:
            if action == "remove":
                patch.remove(runner)
            else:
                patch.apply(runner)
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]error: {exc}[/]")
            rc = 1
            continue
        new = patch.state(runner)
        console.print(f"  -> now [{new.color}]{new.label}[/]")
    return rc


def cmd_apply(args: argparse.Namespace) -> int:
    return _act(args, "apply")


def cmd_remove(args: argparse.Namespace) -> int:
    return _act(args, "remove")


def cmd_reapply(args: argparse.Namespace) -> int:
    """Heal DRIFTED patches only — used by the post-upcp hook."""
    _require_root()
    runner = _runner(args)
    healed = 0
    for patch in load_patches():
        if patch.state(runner) == PatchState.DRIFTED:
            console.print(f"[yellow]healing drifted patch {patch.id}[/]")
            try:
                patch.apply(runner)
                healed += 1
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [red]error: {exc}[/]")
    console.print(f"reapply complete: {healed} patch(es) healed")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    runner = Runner()
    rc = 0
    for patch in _resolve(args.id):
        ok, msg = patch.self_test(runner)
        color = "green" if ok else "red"
        console.print(f"[bold cyan]{patch.id}[/] self-test: [{color}]{'PASS' if ok else 'FAIL'}[/] — {msg}")
        rc = rc or (0 if ok else 1)
    return rc


def cmd_hook(args: argparse.Namespace) -> int:
    runner = _runner(args) if args.action in ("install", "remove") else Runner()
    if args.action == "status":
        ok, msg = hook.status(runner)
        console.print(f"post-upcp hook: [{'green' if ok else 'yellow'}]{msg}[/]")
        return 0
    _require_root()
    if args.action == "install":
        hook.install(runner)
        console.print("[green]hook installed[/] (runs cpanel-doctor reapply after upcp)")
    else:
        hook.remove(runner)
        console.print("[green]hook removed[/]")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from .tui import DoctorApp

    DoctorApp().run()
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cpanel-doctor", description=__doc__)
    p.add_argument("--version", action="version", version=f"cpanel-doctor {__version__}")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("tui", help="launch the interactive Textual UI (default)")
    sub.add_parser("list", help="list patches and their status")

    sp = sub.add_parser("status", help="detailed, per-component status")
    sp.add_argument("id", nargs="*", help="patch id(s), or none for all")

    for name, helptext in (("apply", "apply patch(es)"), ("remove", "remove patch(es)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("id", nargs="+", help="patch id(s) or 'all'")
        sp.add_argument("--dry-run", action="store_true", help="preview only")
        sp.add_argument("--yes", action="store_true", help="assume yes (non-interactive)")

    sp = sub.add_parser("reapply", help="re-apply only DRIFTED patches (post-upcp self-heal)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--yes", action="store_true")

    sp = sub.add_parser("test", help="run a patch's functional self-test")
    sp.add_argument("id", nargs="*", help="patch id(s), or none for all")

    sp = sub.add_parser("hook", help="manage the post-upcp self-heal hook")
    sp.add_argument("action", choices=["install", "remove", "status"])
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--yes", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        None: cmd_tui,
        "tui": cmd_tui,
        "list": cmd_list,
        "status": cmd_status,
        "apply": cmd_apply,
        "remove": cmd_remove,
        "reapply": cmd_reapply,
        "test": cmd_test,
        "hook": cmd_hook,
    }
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
