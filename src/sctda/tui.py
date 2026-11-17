"""Terminal UI: project entry, mapping, plan run, evidence follow-up.

The TUI is a thin view over `harness.invoke_tool`; it has no privileged path, so
anything it can do is also auditable and reproducible from the CLI. There is no
approval key: a plan is frozen when it is written, and `run` executes it.
"""
from __future__ import annotations

import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (DataTable, Footer, Header, Input,
                             RichLog, Static, TabbedContent, TabPane)

from .harness import invoke_tool


class SctdaApp(App):
    CSS = """
    Screen { layout: vertical; }
    #top { height: 3; dock: top; background: $boost; padding: 0 1; }
    #body { height: 1fr; }
    DataTable { height: 1fr; }
    #askrow { height: 3; }
    #askinput { width: 1fr; }
    #log { height: 12; border: round $primary; }
    .hint { color: $text-muted; }
    """

    BINDINGS = [
        ("q", "quit", "quit"),
        ("s", "refresh", "refresh"),
        ("r", "run", "run selected frozen plan"),
        ("z", "pause", "pause protocol"),
        ("d", "derive", "derive evidence"),
        ("o", "dossiers", "candidate dossiers"),
        ("t", "targets", "target chains"),
        ("p", "report", "build report"),
        ("/", "focus_ask", "ask"),
    ]

    def __init__(self, project: str):
        super().__init__()
        self.project = str(Path(project).resolve())
        self.state: dict = {}
        self.selected_protocol: str | None = None

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"project: {self.project}", id="top")
        with Vertical(id="body"):
            with TabbedContent():
                with TabPane("Candidates", id="tab-cand"):
                    yield DataTable(id="cand")
                with TabPane("Protocols", id="tab-prot"):
                    yield DataTable(id="prot")
                with TabPane("Tasks", id="tab-task"):
                    yield DataTable(id="task")
                with TabPane("Evidence", id="tab-ev"):
                    with Vertical():
                        yield Static("Type a candidate label or gene symbol, then Enter.",
                                     classes="hint")
                        with Horizontal(id="askrow"):
                            yield Input(placeholder="e.g. Capillary or FLT4", id="askinput")
                        yield DataTable(id="ev")
            yield RichLog(id="log", wrap=True)
        yield Footer()

    async def on_mount(self) -> None:
        for tid, cols in (("cand", ("label", "cells", "unlabeled")),
                          ("prot", ("version", "status", "protocol")),
                          ("task", ("method", "candidate", "status", "reason")),
                          ("ev", ("subject", "claim", "sources", "statement"))):
            table = self.query_one(f"#{tid}", DataTable)
            table.add_columns(*cols)
        self.query_one("#prot", DataTable).cursor_type = "row"
        self.action_refresh()

    # ------------------------------------------------------------------- state
    def action_refresh(self) -> None:
        self.state = invoke_tool("sctda_status", {"project": self.project})
        self._fill_candidates()
        self._fill_protocols()
        self._fill_tasks()
        self._log(f"refreshed: {self.state.get('counts')}")

    def _fill_candidates(self) -> None:
        table = self.query_one("#cand", DataTable)
        table.clear()
        res = invoke_tool("sctda_candidates", {"project": self.project})
        for c in res.get("candidates", []):
            table.add_row(c["label"], str(c["n_cells"]),
                          "yes" if c["is_unlabeled"] else "")
        if res.get("status") == "needs_mapping":
            self._log("needs mapping: " + ", ".join(res.get("columns", [])[:12]))

    def _fill_protocols(self) -> None:
        table = self.query_one("#prot", DataTable)
        table.clear()
        self._protocol_rows = self.state.get("protocols", [])
        for p in self._protocol_rows:
            table.add_row(str(p["version"]), p["status"], p["protocol_id"])

    def _fill_tasks(self) -> None:
        table = self.query_one("#task", DataTable)
        table.clear()
        for status, n in (self.state.get("task_outcomes") or {}).items():
            table.add_row(f"({n} tasks)", "", status, "")

    # ------------------------------------------------------------------ actions
    def _current_protocol(self) -> str | None:
        table = self.query_one("#prot", DataTable)
        rows = getattr(self, "_protocol_rows", [])
        if not rows:
            return None
        idx = table.cursor_row if table.cursor_row is not None else 0
        idx = max(0, min(idx, len(rows) - 1))
        return rows[idx]["protocol_id"]

    def action_run(self) -> None:
        pid = self._current_protocol()
        if not pid:
            self._log("no protocol selected")
            return
        out = invoke_tool("sctda_run", {"project": self.project, "protocol": pid,
                                        "resume": True})
        self._log(f"run {pid}: {out.get('status')} {out.get('outcomes') or out.get('error')}")
        self.action_refresh()

    def _with_current(self, tool: str, extra: dict | None = None) -> None:
        pid = self._current_protocol()
        if not pid:
            self._log("no protocol selected")
            return
        out = invoke_tool(tool, {"project": self.project, "protocol": pid, **(extra or {})})
        self._log(f"{tool}: {out.get('status')} "
                  f"{out.get('error') or json.dumps({k: v for k, v in out.items() if k in ('n_claims','n_evidence_added','groups','n_targets','outdir')}, ensure_ascii=False)}")
        self.action_refresh()

    def action_pause(self) -> None:
        pid = self._current_protocol()
        if not pid:
            self._log("no protocol selected")
            return
        out = invoke_tool("sctda_pause", {"project": self.project, "protocol": pid})
        self._log(f"pause {pid}: {out.get('status')} {out.get('error') or ''}")
        self.action_refresh()

    def action_derive(self) -> None:
        self._with_current("sctda_derive")

    def action_dossiers(self) -> None:
        pid = self._current_protocol()
        if not pid:
            return
        out = invoke_tool("sctda_dossiers", {"project": self.project, "protocol": pid})
        for k, labels in (out.get("groups") or {}).items():
            self._log(f"{k}: {', '.join(labels) or '-'}")

    def action_targets(self) -> None:
        self._with_current("sctda_targets", {"top": 3})

    def action_report(self) -> None:
        pid = self._current_protocol()
        out = invoke_tool("sctda_report", {"project": self.project,
                                           "protocol": pid, "formats": "html,csv,pdf"})
        self._log(f"report: {out.get('outdir') or out.get('error')}")

    def action_focus_ask(self) -> None:
        self.query_one("#askinput", Input).focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        subject = event.value.strip()
        if not subject:
            return
        out = invoke_tool("sctda_ask", {"project": self.project, "subject": subject})
        table = self.query_one("#ev", DataTable)
        table.clear()
        for c in out.get("claims", []):
            ind = c["independence"]
            table.add_row(subject, c["claim_id"],
                          f"ext={ind['independent_external_sources']} "
                          f"own={'y' if ind['own_computation_only'] else 'n'}",
                          c["statement"][:120])
        if not out.get("claims"):
            self._log(f"no claim recorded for {subject!r}")
        event.input.value = ""

    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)


def run_tui(project: str) -> int:
    SctdaApp(project).run()
    return 0
