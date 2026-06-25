"""Interactive Textual UI for cPanel Doctor."""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from . import __version__, hook
from .core import Action, Patch, Runner, load_patches


class DoctorApp(App):
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #table { width: 46%; border: round $primary; }
    #side { width: 1fr; }
    #detail { height: 1fr; border: round $secondary; padding: 0 1; }
    #log { height: 14; border: round $accent; }
    #banner { height: 1; content-align: center middle; color: $text-muted; }
    """

    BINDINGS = [
        ("a", "apply", "Apply"),
        ("r", "remove", "Remove"),
        ("h", "heal", "Re-apply drift"),
        ("t", "test", "Self-test"),
        ("k", "hook", "Toggle hook"),
        ("d", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.patches: List[Patch] = []
        self._row_to_patch: Dict[str, Patch] = {}
        self._busy = False

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="banner")
        with Horizontal(id="body"):
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side"):
                yield Static(id="detail")
                yield RichLog(id="log", highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "cPanel Doctor"
        self.sub_title = f"v{__version__}"
        table = self.query_one("#table", DataTable)
        table.add_columns("ID", "Status", "Patch")
        self._banner()
        self.refresh_table()
        self.log_write("[dim]Select a patch; a=apply r=remove h=heal t=test k=hook.[/]")
        if os.geteuid() != 0:
            self.log_write("[yellow]Not running as root — apply/remove will fail. Re-run with sudo.[/]")

    # -- helpers ------------------------------------------------------------
    def _banner(self) -> None:
        ok, msg = hook.status(Runner())
        color = "green" if ok else "yellow"
        self.query_one("#banner", Static).update(
            Text.from_markup(f"post-upcp self-heal hook: [{color}]{msg}[/]   (press k to toggle)")
        )

    def log_write(self, markup: str) -> None:
        self.query_one("#log", RichLog).write(Text.from_markup(markup))

    def refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        saved = table.cursor_row
        table.clear()
        self.patches = load_patches()
        self._row_to_patch.clear()
        runner = Runner()
        for patch in self.patches:
            st = patch.state(runner)
            key = table.add_row(
                patch.id,
                Text(st.label, style=st.color),
                patch.name,
                key=patch.id,
            )
            self._row_to_patch[str(key.value)] = patch
        if self.patches:
            try:
                table.move_cursor(row=min(saved, len(self.patches) - 1))
            except Exception:
                pass
        self._banner()
        self.update_detail()

    def current_patch(self) -> Optional[Patch]:
        table = self.query_one("#table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
        except Exception:
            return None
        return self._row_to_patch.get(str(row_key.value))

    def update_detail(self) -> None:
        patch = self.current_patch()
        detail = self.query_one("#detail", Static)
        if patch is None:
            detail.update("No patches.")
            return
        runner = Runner()
        st = patch.state(runner)
        lines = [
            f"[bold cyan]{patch.id}[/]  [{st.color}]{st.label}[/]",
            f"[bold]{patch.name}[/]",
            "",
            patch.description,
            "",
            "[bold]Components[/]",
        ]
        applicable, reason = patch.applicable(runner)
        if not applicable:
            lines.append(f"  [grey50]not applicable: {reason}[/]")
        else:
            for cs in patch.component_statuses(runner):
                dot = "[green]●[/]" if cs.present else "[red]○[/]"
                lines.append(f"  {dot} [b]{cs.key}[/] — {cs.description}")
        if patch.caveats:
            lines += ["", f"[yellow]⚠ {patch.caveats}[/]"]
        detail.update(Text.from_markup("\n".join(lines)))

    def on_data_table_row_highlighted(self, _event) -> None:
        self.update_detail()

    # -- actions ------------------------------------------------------------
    def action_refresh(self) -> None:
        self.refresh_table()
        self.log_write("[dim]refreshed[/]")

    def action_apply(self) -> None:
        self._run_on_current("apply")

    def action_remove(self) -> None:
        self._run_on_current("remove")

    def action_test(self) -> None:
        patch = self.current_patch()
        if patch:
            ok, msg = patch.self_test(Runner())
            self.log_write(f"[bold]{patch.id}[/] self-test: "
                           f"[{'green' if ok else 'red'}]{'PASS' if ok else 'FAIL'}[/] — {msg}")

    def action_heal(self) -> None:
        self._run_worker("reapply", None)

    def action_hook(self) -> None:
        if os.geteuid() != 0:
            self.log_write("[red]need root to manage the hook[/]")
            return
        self._run_worker("hook", None)

    def _run_on_current(self, op: str) -> None:
        patch = self.current_patch()
        if patch is None:
            return
        if os.geteuid() != 0:
            self.log_write("[red]need root for this action[/]")
            return
        self._run_worker(op, patch.id)

    def _run_worker(self, op: str, patch_id: Optional[str]) -> None:
        if self._busy:
            self.log_write("[yellow]busy…[/]")
            return
        self._busy = True
        self._work(op, patch_id)

    @work(thread=True, exclusive=True)
    def _work(self, op: str, patch_id: Optional[str]) -> None:
        def echo(a: Action) -> None:
            if a.kind == "note":
                self.call_from_thread(self.log_write, f"  [dim]{a.detail}[/]")
            else:
                mark = "[green]✓[/]" if a.ok else "[red]✗[/]"
                self.call_from_thread(self.log_write,
                                      f"  {mark} {a.kind} [cyan]{a.target}[/] [dim]{a.detail}[/]")

        runner = Runner(on_action=echo)
        try:
            if op == "hook":
                ok, _ = hook.status(runner)
                if ok:
                    self.call_from_thread(self.log_write, "[bold]removing post-upcp hook[/]")
                    hook.remove(runner)
                else:
                    self.call_from_thread(self.log_write, "[bold]installing post-upcp hook[/]")
                    hook.install(runner)
            elif op == "reapply":
                self.call_from_thread(self.log_write, "[bold]healing drifted patches[/]")
                from .core import PatchState

                for patch in load_patches():
                    if patch.state(runner) == PatchState.DRIFTED:
                        self.call_from_thread(self.log_write, f"[yellow]heal {patch.id}[/]")
                        patch.apply(runner)
            else:
                registry = {p.id: p for p in load_patches()}
                patch = registry[patch_id]
                self.call_from_thread(self.log_write, f"[bold]{op} {patch.id}[/]")
                (patch.remove if op == "remove" else patch.apply)(runner)
                ok, msg = patch.self_test(runner) if op == "apply" else (True, "")
                if op == "apply":
                    self.call_from_thread(self.log_write,
                                          f"  self-test: [{'green' if ok else 'red'}]"
                                          f"{'PASS' if ok else 'FAIL'}[/] — {msg}")
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.log_write, f"[red]error: {exc}[/]")
        finally:
            self._busy = False
            self.call_from_thread(self.refresh_table)


if __name__ == "__main__":
    DoctorApp().run()
