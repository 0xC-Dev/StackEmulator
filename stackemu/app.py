"""Textual TUI app that glues the editor, emulator, and visualizers together."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, RichLog, TextArea

from .asm_editor import AsmTextArea
from .assembler import AsmError, AsmResult, assemble, detect_syntax
from .emulator import Emulator
from .widgets import CodeView, RegistersView, StackView


DEFAULT_SOURCE = ""


EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


class StackEmuApp(App):
    CSS = """
    Screen { layout: vertical; }

    #main { height: 1fr; }

    /* Editor is capped so the right pane always has room on wide screens. */
    #left  { width: 1fr; min-width: 28; max-width: 82; }
    #right { width: 1fr; min-width: 52; }

    #editor           { height: 1fr; border: solid $accent; }
    #editor.hidden    { display: none; }
    #code_scroll      { height: 1fr; border: solid $success;
                        scrollbar-size-vertical: 1; padding: 0; }
    #code_scroll.hidden { display: none; }
    #code_view        { height: auto; }

    /* Scroll containers own the border and scrollbar; inner Static
       widgets auto-size to content so the scrollbar tracks correctly. */
    #regs_scroll  { height: 20;  border: solid cyan;    padding: 0;
                    scrollbar-size-vertical: 1; }
    #stack_scroll { height: 1fr; border: solid magenta; padding: 0;
                    scrollbar-size-vertical: 1; }

    #code_scroll:focus,
    #regs_scroll:focus,
    #stack_scroll:focus { border: heavy $accent; }

    #status { height: 6; border: solid $warning; }
    """

    # priority=True keeps bindings live while the TextArea has focus,
    # which would otherwise consume function keys.
    BINDINGS = [
        Binding("f6", "assemble", "Assemble", priority=True),
        Binding("f7", "edit_mode", "Edit", priority=True),
        Binding("f10", "step", "Step", priority=True),
        Binding("f9", "step_back", "Back", priority=True),
        Binding("f5", "run", "Run", priority=True),
        Binding("ctrl+r", "reset", "Reset", priority=True),
        Binding("ctrl+k", "clear_editor", "Clear", priority=True),
        Binding("ctrl+g", "cycle_slot_size", "Slot", priority=True),
        Binding("ctrl+l", "cycle_example", "Example", priority=True),
        Binding("ctrl+s", "save_file", "Save", priority=True),
        Binding("ctrl+o", "open_file", "Open", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    TITLE = "StackEmulate — x86-64 stack visualizer"

    def __init__(self, initial_source: Optional[str] = None):
        super().__init__()
        self.emu = Emulator()
        self.asm_result: Optional[AsmResult] = None
        self._examples: list[Path] = []
        self._example_index = -1
        self._initial_source = initial_source or DEFAULT_SOURCE

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="main"):
            with Vertical(id="left"):
                editor = AsmTextArea(
                    self._initial_source,
                    id="editor",
                    show_line_numbers=True,
                    theme="monokai",
                    tab_behavior="indent",
                )
                editor.border_title = "Editor (F6 assemble  F7 back to edit)"
                yield editor
                code_scroll = VerticalScroll(
                    CodeView(id="code_view"),
                    id="code_scroll",
                    classes="hidden",
                )
                code_scroll.border_title = (
                    "Code (mouse-wheel / arrows to scroll  F7 back to editor)"
                )
                code_scroll.can_focus = True
                yield code_scroll
            with Vertical(id="right"):
                regs_scroll = VerticalScroll(
                    RegistersView(id="regs"), id="regs_scroll",
                )
                regs_scroll.border_title = "Registers"
                regs_scroll.can_focus = True
                yield regs_scroll
                stack_scroll = VerticalScroll(
                    StackView(id="stack"), id="stack_scroll",
                )
                stack_scroll.border_title = (
                    "Stack — 8B slots  (Ctrl+G to change  |  wheel/arrows to scroll)"
                )
                stack_scroll.can_focus = True
                yield stack_scroll
        log = RichLog(id="status", markup=True, highlight=False, wrap=True)
        log.border_title = "Status"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        try:
            if EXAMPLES_DIR.is_dir():
                self._examples = sorted(EXAMPLES_DIR.glob("*.asm"))
        except OSError:
            self._examples = []
        self._log("[b]StackEmulate ready.[/b] F6 assemble  F10 step  F9 back  "
                  "F5 run  Ctrl+R reset  Ctrl+K clear  Ctrl+G stack-slot  "
                  "Ctrl+L example  (mouse-wheel or Tab+arrows to scroll panes)")
        self._refresh_visualizers()

    # ------------------------------------------------------------ view state

    def _log(self, message: str) -> None:
        self.query_one("#status", RichLog).write(message)

    def _show_editor(self) -> None:
        self.query_one("#editor").remove_class("hidden")
        self.query_one("#code_scroll").add_class("hidden")
        self.query_one("#editor", TextArea).focus()

    def _show_code(self) -> None:
        self.query_one("#editor").add_class("hidden")
        self.query_one("#code_scroll").remove_class("hidden")
        # Focus the scroll container so arrow keys / PgUp / PgDn scroll it.
        self.query_one("#code_scroll").focus()

    def _refresh_visualizers(self, error_line: Optional[int] = None) -> None:
        current = self.emu.current()
        previous = self.emu.previous()
        source = self.query_one("#editor", TextArea).text
        used_subregs = (self.asm_result.used_subregs
                        if self.asm_result is not None else set())
        self.query_one("#regs", RegistersView).update_state(
            current, previous, used_subregs,
        )
        self.query_one("#stack", StackView).update_state(
            current, previous, self.asm_result, source,
        )

        if self.asm_result is not None:
            current_line = None
            if current is not None and not self.emu.halted:
                current_line = self.asm_result.addr_to_line.get(current.instr_addr)
            self.query_one("#code_view", CodeView).update_state(
                source, self.asm_result.syntax, current_line, error_line,
            )

    # -------------------------------------------------------------- actions

    def action_assemble(self) -> None:
        source = self.query_one("#editor", TextArea).text
        syntax = detect_syntax(source)
        try:
            self.asm_result = assemble(source, syntax)
        except AsmError as e:
            self.asm_result = None
            line_hint = f" (line {e.line + 1})" if e.line is not None else ""
            self._log(f"[b red]assemble failed[/b red]{line_hint}: {e}")
            self.query_one("#code_view", CodeView).update_state(
                source, syntax, None, error_line=e.line,
            )
            return

        self.emu.load(self.asm_result.code_bytes,
                      entry_point=self.asm_result.entry_point)
        n_bytes = len(self.asm_result.code_bytes)
        if self.asm_result.entry_label:
            entry_desc = (f"[b]{self.asm_result.entry_label}[/b] "
                          f"(0x{self.asm_result.entry_point:016X})")
        else:
            entry_desc = (f"top of file "
                          f"(0x{self.asm_result.entry_point:016X}) - "
                          f"no main/_start label found")
        self._log(f"[b green]assembled[/b green] {n_bytes} bytes  "
                  f"syntax=[b]{self.asm_result.syntax}[/b]  "
                  f"instructions={len(self.asm_result.addr_to_line)}  "
                  f"entry={entry_desc}")
        self._show_code()
        self._refresh_visualizers()

    def action_edit_mode(self) -> None:
        self._show_editor()

    def action_step(self) -> None:
        if self.asm_result is None:
            self._log("[yellow]nothing to step — press F6 to assemble first[/yellow]")
            return
        if self.emu.halted:
            self._log("[yellow]program halted; press Ctrl+R to reset[/yellow]")
            return
        if self.emu.step():
            # previous() is the snapshot from before this step executed.
            snap = self.emu.previous()
            executed = snap.instr_text if snap else "?"
            self._log(f"step {self.emu.step_count():>4}  executed: [b cyan]{executed}[/b cyan]")
        if self.emu.error:
            self._log(f"[b red]{self.emu.error}[/b red]")
        if self.emu.halted and not self.emu.error:
            self._log("[b]program halted (RIP left code region)[/b]")
        self._refresh_visualizers()

    def action_step_back(self) -> None:
        if self.asm_result is None:
            return
        if self.emu.step_back():
            self._log(f"stepped back  (now at step {self.emu.step_count()})")
        else:
            self._log("[yellow]already at the beginning[/yellow]")
        self._refresh_visualizers()

    def action_run(self) -> None:
        if self.asm_result is None:
            self._log("[yellow]nothing to run — press F6 to assemble first[/yellow]")
            return
        n = self.emu.run_to_end()
        self._log(f"ran {n} steps to end.")
        if self.emu.error:
            self._log(f"[b red]{self.emu.error}[/b red]")
        self._refresh_visualizers()

    def action_reset(self) -> None:
        if self.asm_result is None:
            return
        self.emu.reset()
        self._log("[b]reset[/b] — RIP back to start")
        self._refresh_visualizers()

    def action_clear_editor(self) -> None:
        editor = self.query_one("#editor", TextArea)
        editor.text = ""
        self.asm_result = None
        # Fresh Emulator drops any snapshots / stack state from the last run.
        self.emu = Emulator()
        self._show_editor()
        self._log("[b]cleared[/b] — editor empty, emulator reset")
        self._refresh_visualizers()

    def action_cycle_slot_size(self) -> None:
        # Cycle stack-view granularity 8 -> 4 -> 2 -> 1 -> 8.
        stack = self.query_one("#stack", StackView)
        order = (8, 4, 2, 1)
        cur = stack.slot_size if stack.slot_size in order else 8
        new_size = order[(order.index(cur) + 1) % len(order)]
        stack.set_slot_size(new_size)
        scroll = self.query_one("#stack_scroll")
        scroll.border_title = (
            f"Stack — {new_size}B slots  "
            f"(Ctrl+G to change  |  wheel/arrows to scroll)"
        )
        self._log(f"stack view: [b]{new_size}-byte[/b] slots")

    def action_cycle_example(self) -> None:
        if not self._examples:
            self._log("[yellow]no examples found in examples/ directory[/yellow]")
            return
        self._example_index = (self._example_index + 1) % len(self._examples)
        path = self._examples[self._example_index]
        try:
            self.query_one("#editor", TextArea).text = path.read_text(encoding="utf-8")
        except OSError as e:
            self._log(f"[red]could not read {path.name}: {e}[/red]")
            return
        self.asm_result = None
        self._show_editor()
        self._log(f"loaded example [b]{path.name}[/b] "
                  f"({self._example_index + 1}/{len(self._examples)})")

    def action_open_file(self) -> None:
        # Dependency-free file open: reads the path from a `#file: <path>`
        # header on the first source line to avoid a picker widget.
        editor = self.query_one("#editor", TextArea)
        first = editor.text.split("\n", 1)[0].strip()
        if not first.lower().startswith("#file:"):
            self._log("[yellow]to open a file: put '#file: <path>' on the "
                      "first line, then press Ctrl+O[/yellow]")
            return
        path = Path(first.split(":", 1)[1].strip())
        try:
            editor.text = path.read_text(encoding="utf-8")
            self._log(f"loaded [b]{path}[/b]")
            self.asm_result = None
            self._show_editor()
        except OSError as e:
            self._log(f"[red]open failed: {e}[/red]")

    def action_save_file(self) -> None:
        editor = self.query_one("#editor", TextArea)
        first = editor.text.split("\n", 1)[0].strip()
        if not first.lower().startswith("#file:"):
            self._log("[yellow]to save: put '#file: <path>' on the "
                      "first line, then press Ctrl+S[/yellow]")
            return
        path = Path(first.split(":", 1)[1].strip())
        try:
            path.write_text(editor.text, encoding="utf-8")
            self._log(f"saved [b]{path}[/b]")
        except OSError as e:
            self._log(f"[red]save failed: {e}[/red]")


def main() -> None:
    import sys
    initial = None
    if len(sys.argv) > 1:
        try:
            initial = Path(sys.argv[1]).read_text(encoding="utf-8")
        except OSError as e:
            print(f"could not read {sys.argv[1]}: {e}")
            return
    StackEmuApp(initial_source=initial).run()


if __name__ == "__main__":
    main()
