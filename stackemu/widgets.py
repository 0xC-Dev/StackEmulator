"""Rich renderables for the registers, stack and code panes.

Textual 8.x requires the top-level renderable of a Static widget to
expose `get_height` — `rich.Panel` does not. These widgets therefore
return plain Table / Group / Syntax objects and rely on CSS plus the
widget's `border_title` to paint the surrounding border and title.
"""

from __future__ import annotations

import re
from typing import Optional

from rich.console import Group
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from .emulator import (
    Snapshot, REG_NAMES,
    STACK_BASE, STACK_SIZE, STACK_TOP, INITIAL_RSP,
    CODE_BASE,
)
from .assembler import SUBREG_MAP


_LABEL_RE = re.compile(r'^\s*([A-Za-z_.\$][\w.\$]*)\s*:')


def _subreg_value(parent_val: int, kind) -> int:
    if kind == 32:    return parent_val & 0xFFFFFFFF
    if kind == 16:    return parent_val & 0xFFFF
    if kind == 8:     return parent_val & 0xFF
    if kind == "hi8": return (parent_val >> 8) & 0xFF
    return parent_val


def _fmt_subreg(val: int, kind) -> str:
    if kind == 32:    return f"0x{val:08X}"
    if kind == 16:    return f"0x{val:04X}"
    if kind in (8, "hi8"): return f"0x{val:02X}"
    return f"0x{val:016X}"


# ---- Registers ----------------------------------------------------------

class RegistersView(Static):
    """GP64 + RIP + RFLAGS, plus any sub-registers referenced by the source.

    Sub-registers (EAX, AX, AH, AL, R8D, ...) appear indented under
    their parent whenever the source references them, so the narrow
    aliases sit alongside the full 64-bit value they alias.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._current: Optional[Snapshot] = None
        self._previous: Optional[Snapshot] = None
        self._used_subregs: set = set()

    def on_mount(self) -> None:
        self.update(self._build())

    def update_state(self, current: Optional[Snapshot],
                     previous: Optional[Snapshot] = None,
                     used_subregs: Optional[set] = None):
        self._current = current
        self._previous = previous
        self._used_subregs = used_subregs or set()
        self.update(self._build())

    def _build(self):
        if self._current is None:
            return Text("(not assembled)", style="dim")

        table = Table.grid(padding=(0, 1))
        table.add_column("reg", style="bold cyan", no_wrap=True, justify="right")
        table.add_column("val", no_wrap=True)

        for name in REG_NAMES:
            val = self._current.regs[name]
            changed = (self._previous is not None
                       and self._previous.regs.get(name) != val)
            if name == "RFLAGS":
                cell = Text(_fmt_rflags(val), style=_val_style(changed))
            else:
                cell = Text(_fmt_hex(val), style=_val_style(changed))
            table.add_row(name, cell)

            # Emit only the sub-registers of `name` that the source actually
            # touches, to keep the pane compact.
            if name in SUBREG_MAP:
                prev_parent = (self._previous.regs.get(name)
                               if self._previous is not None else None)
                for subname, kind in SUBREG_MAP[name]:
                    if subname not in self._used_subregs:
                        continue
                    sub_val = _subreg_value(val, kind)
                    sub_changed = False
                    if prev_parent is not None:
                        sub_changed = (_subreg_value(prev_parent, kind) != sub_val)
                    sub_cell = Text(_fmt_subreg(sub_val, kind),
                                    style=_val_style(sub_changed))
                    # `|-` tree glyph marks the sub-register as a child of
                    # the parent row above it.
                    label = Text(f"|- {subname}", style="dim cyan")
                    table.add_row(label, sub_cell)
        return table


def _val_style(changed: bool) -> str:
    return "bold black on yellow" if changed else "white"


def _fmt_hex(val: int) -> str:
    """Full 16-hex value with `0x` prefix. Leading zeros are preserved
    to keep byte-size and padding visible."""
    return f"0x{val:016X}"


def _fmt_hex_size(val: int, size: int) -> str:
    """Zero-padded hex for a `size`-byte value (2 hex chars per byte)."""
    return f"0x{val:0{size * 2}X}"


def _fmt_rflags(rflags: int) -> str:
    # Full 16-hex plus the flag-letter block ("CpAzsDO"): upper case
    # means the flag is set, lower case means clear.
    flags = [
        ("C", 0), ("P", 2), ("A", 4), ("Z", 6),
        ("S", 7), ("D", 10), ("O", 11),
    ]
    parts = [(name if (rflags >> bit) & 1 else name.lower()) for name, bit in flags]
    return f"0x{rflags:016X} {''.join(parts)}"


# ---- Stack --------------------------------------------------------------

class StackView(Static):
    """8-byte slots rendered high-address-on-top, low-address-on-bottom.

    Renders the whole live stack (from a few slots above the initial
    RSP down past the current RSP), inserts a divider between call
    frames using tracked return addresses, and labels each frame with
    its owning function (return-address value -> source line ->
    nearest preceding label).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._current: Optional[Snapshot] = None
        self._previous: Optional[Snapshot] = None
        self._asm = None
        self._source_lines: list[str] = []
        self._pad_above = 3          # extra slots shown above initial RSP
        self._pad_below = 4          # extra slots shown below current RSP
        self._rsp_line: Optional[int] = None   # y-index used by auto-scroll
        # Byte-width of each rendered row. Defaults to 8 (native x86-64
        # push/pop granularity). May be shrunk to 4 / 2 / 1 to inspect
        # sub-qword writes; the stack itself is byte-addressable.
        self.slot_size: int = 8

    def set_slot_size(self, size: int) -> None:
        if size not in (1, 2, 4, 8):
            raise ValueError(f"slot_size must be 1, 2, 4, or 8; got {size}")
        self.slot_size = size
        self.update(self._build())
        self.call_after_refresh(self._scroll_to_rsp)

    def on_mount(self) -> None:
        self.update(self._build())

    def update_state(self, current: Optional[Snapshot],
                     previous: Optional[Snapshot] = None,
                     asm_result=None, source: str = ""):
        self._current = current
        self._previous = previous
        self._asm = asm_result
        self._source_lines = source.split("\n") if source else []
        self.update(self._build())
        # Scroll after refresh so the new content has been laid out
        # and the y-index we computed corresponds to a real row.
        self.call_after_refresh(self._scroll_to_rsp)

    def _scroll_to_rsp(self) -> None:
        """Keep the RSP row roughly centered in the containing scroller."""
        if self._rsp_line is None:
            return
        parent = self.parent
        if parent is None or not hasattr(parent, "scroll_to"):
            return
        try:
            viewport_h = parent.size.height
        except Exception:
            return
        target_y = max(0, self._rsp_line - viewport_h // 2)
        try:
            parent.scroll_to(y=target_y, animate=False)
        except Exception:
            pass

    # ---- frame-labeling helpers ----

    def _label_for_return_target(self, ret_target: int) -> str:
        """Return the name of the function that owns `ret_target`.

        Given a return-address value from a stack slot, snap it down to
        the nearest known instruction, then walk backward to the
        enclosing label. RET pushes the address of the instruction that
        follows the CALL, so exact-match lookups usually miss.
        """
        if self._asm is None or not self._source_lines:
            return f"0x{ret_target:016X}"
        addr_to_line = self._asm.addr_to_line
        line = addr_to_line.get(ret_target)
        if line is None:
            for addr in sorted(addr_to_line.keys(), reverse=True):
                if addr <= ret_target:
                    line = addr_to_line[addr]
                    break
        if line is None:
            return f"0x{ret_target:016X}"
        for i in range(line, -1, -1):
            m = _LABEL_RE.match(self._source_lines[i])
            if m:
                return m.group(1)
        return f"0x{ret_target:016X}"

    def _current_function_label(self, rip: int) -> str:
        if self._asm is None or not self._source_lines:
            return ""
        addr_to_line = self._asm.addr_to_line
        line = addr_to_line.get(rip)
        if line is None:
            for addr in sorted(addr_to_line.keys(), reverse=True):
                if addr <= rip:
                    line = addr_to_line[addr]
                    break
        if line is None:
            return ""
        for i in range(line, -1, -1):
            m = _LABEL_RE.match(self._source_lines[i])
            if m:
                return m.group(1)
        return ""

    def _build(self):
        self._rsp_line = None
        if self._current is None:
            return Text("(not assembled)", style="dim")

        size = self.slot_size

        rsp = self._current.regs["RSP"]
        rbp = self._current.regs["RBP"]
        rip = self._current.regs["RIP"]
        stack = self._current.stack_bytes
        written = self._current.written_mask
        ret_addrs = self._current.return_addrs
        callees = getattr(self._current, "callees", {}) or {}

        code_end = CODE_BASE + (len(self._asm.code_bytes) if self._asm else 0)

        top = INITIAL_RSP + self._pad_above * size
        bottom = rsp - self._pad_below * size

        # Extend `bottom` down to the lowest address the program has ever
        # written so contents of freed frames stay visible (e.g. locals
        # of a function whose epilogue has already run).
        for off, b in enumerate(written):
            if b:
                low_addr = STACK_BASE + (off // size) * size
                bottom = min(bottom, low_addr - size)
                break
        # Symmetric extension of `top` for the highest written slot.
        for off in range(STACK_SIZE - 1, -1, -1):
            if written[off]:
                high_addr = STACK_BASE + (off // size) * size
                top = max(top, high_addr + 2 * size)
                break
        top = min(top, STACK_TOP)
        bottom = max(bottom, STACK_BASE)
        top = (top // size) * size
        bottom = (bottom // size) * size

        # Per-RA slot: caller-side label and callee-side (frame owner)
        # label. `ret_addrs` always holds 8-byte-aligned offsets since RA
        # slots are pushed by CALL, regardless of the current view size.
        ra_caller: dict[int, str] = {}
        ra_callee: dict[int, str] = {}
        for off in ret_addrs:
            slot_addr = STACK_BASE + off
            if 0 <= off <= STACK_SIZE - 8:
                ret_val = int.from_bytes(stack[off:off + 8], "little")
                ra_caller[slot_addr] = self._label_for_return_target(ret_val)
                callee_addr = callees.get(off)
                if callee_addr is not None:
                    ra_callee[slot_addr] = self._label_for_return_target(callee_addr)

        # When view size < 8, an 8-byte RA spans multiple rows; mark every
        # sub-row that falls inside a RA's byte range.
        ret_row_addrs: set[int] = set()
        for ra_addr in ra_caller.keys():
            for j in range(0, 8, size):
                ret_row_addrs.add(ra_addr + j)

        # Shadow-space heuristic (Win64 ABI): the 32 bytes directly above
        # every RA are the caller's reserved shadow area. Enumerate them
        # at the current view size so each visible sub-row is marked.
        shadow_slots: set[int] = set()
        for ra_addr in ra_caller.keys():
            for byte_off in range(8, 40, size):
                shadow_slots.add(ra_addr + byte_off)

        entry_label = getattr(self._asm, "entry_label", None) if self._asm else None
        top_frame_label = entry_label or self._current_function_label(rip) or "start"

        # --- gather rows -----------------------------------------------

        # Each row is a dict with `type` in {'divider', 'slot', 'ellipsis'}.
        # The ellipsis type is unused today but the render loop still
        # supports it for future compact-mode work.
        rows: list[dict] = []

        rows.append({"type": "divider",
                     "text": "-- above initial RSP --",
                     "style": "dim italic yellow"})

        addr = top - size
        entry_frame_opened = False
        while addr >= bottom:
            off = addr - STACK_BASE
            if not (0 <= off <= STACK_SIZE - size):
                addr -= size
                continue

            if not entry_frame_opened and addr < INITIAL_RSP:
                rows.append({"type": "divider",
                             "text": f"-- frame: {top_frame_label} --",
                             "style": "bold blue"})
                entry_frame_opened = True

            val = int.from_bytes(stack[off:off + size], "little")
            is_written = any(written[off:off + size])
            is_ret = addr in ret_row_addrs
            is_rsp = (addr == rsp)
            is_rbp = (addr == rbp)
            is_shadow = (addr in shadow_slots) and not is_ret

            # A slot is allocated to some live frame when RSP sits at or
            # below it. Slots strictly below RSP form the red zone.
            is_allocated = (addr >= rsp)

            # Per-slot change detection, split into:
            #   val_changed     - byte value differs from prior snapshot
            #   just_allocated  - slot was below prior RSP, now at/above
            val_changed = False
            just_allocated = False
            if self._previous is not None:
                prev_stack = self._previous.stack_bytes
                if 0 <= off <= len(prev_stack) - size:
                    prev_val = int.from_bytes(prev_stack[off:off + size], "little")
                    val_changed = (prev_val != val)
                prev_rsp = self._previous.regs.get("RSP", rsp)
                just_allocated = (addr >= rsp and addr < prev_rsp)

            # An RA at 8-byte alignment may occupy multiple sub-rows;
            # attach labels / frame divider only to the row that starts
            # the RA (i.e. the one whose offset is in ret_addrs).
            ra_root = STACK_BASE + off if off in ret_addrs else None

            # Short-form notes column, kept terse for narrow terminals.
            notes = []
            if is_rsp: notes.append("RSP")
            if is_rbp: notes.append("RBP")
            if is_ret:
                caller = ra_caller.get(ra_root) if ra_root is not None else None
                notes.append(f"ret->{caller}" if caller else "ret")
            elif is_shadow:
                notes.append("shdw")
            elif not is_allocated:
                # Below RSP - slot is not owned by any live frame.
                notes.append("free")
            if just_allocated:
                notes.append("[new]")
            if val_changed and not just_allocated:
                notes.append("[chg]")
            if not is_ret and CODE_BASE <= val < code_end:
                lbl = self._label_for_return_target(val)
                if lbl and not lbl.startswith("0x"):
                    notes.append(f"->{lbl}")
                else:
                    notes.append(f"->code")

            interesting = (bool(notes) or is_written or val != 0
                           or just_allocated or val_changed)

            rows.append({
                "type": "slot",
                "addr": addr, "val": val, "is_rsp": is_rsp, "is_rbp": is_rbp,
                "is_ret": is_ret, "is_written": is_written,
                "is_shadow": is_shadow,
                "is_allocated": is_allocated,
                "just_allocated": just_allocated,
                "val_changed": val_changed,
                "notes": notes,
                "interesting": interesting,
            })

            if ra_root is not None:
                callee = ra_callee.get(ra_root, "?")
                rows.append({"type": "divider",
                             "text": f"-- frame: {callee} --",
                             "style": "bold blue"})

            addr -= size

        # --- render to a Rich Table -------------------------------------

        # Emit alternating dividers (Text) and mini-tables (Table.grid).
        # A fresh mini-table per frame keeps column widths tight; if all
        # rows shared one table the value column would be padded out to
        # match the widest divider string.
        parts = [Text("high address", style="dim italic")]

        # Value column width tracks the current view size:
        #   size 8 -> "0x" + 16 hex = 18 cols
        #   size 4 -> "0x" +  8 hex = 10 cols
        #   size 2 -> "0x" +  4 hex =  6 cols
        #   size 1 -> "0x" +  2 hex =  4 cols
        val_col_width = 2 + size * 2

        def make_table() -> Table:
            t = Table.grid(padding=(0, 1))
            t.add_column("addr", no_wrap=True, min_width=18, max_width=18)
            t.add_column("val", no_wrap=True, min_width=val_col_width)
            t.add_column("note", no_wrap=False, overflow="fold")
            return t

        cur = make_table()
        line_index = 1
        for r in rows:
            if r["type"] == "divider":
                if cur.row_count:
                    parts.append(cur)
                    cur = make_table()
                parts.append(Text(r["text"], style=r["style"]))
                line_index += 1
                continue

            if r["type"] == "ellipsis":
                cur.add_row(
                    Text(" ...", style="dim"),
                    Text("", style="dim"),
                    Text(r["text"], style="dim italic"),
                )
                line_index += 1
                continue

            # Stable 5-color palette. Each color carries one meaning and
            # does not flip between steps:
            #   yellow      - return address on the stack
            #   green       - live slot the program has written to
            #   light grey  - allocated to a live frame but never written
            #                 (Win64 shadow space, reserved locals, etc.)
            #   red         - freed slot that still holds old bytes from a
            #                 popped frame (below RSP, was written)
            #   dark grey   - unallocated and never touched (red-zone)
            # Ordering matters: allocation status is checked before the
            # written flag so freed bytes never render as if they were
            # still live. Transient events (`[chg]`, `[new]`, `free`)
            # live in the notes column so the colors stay predictable.
            addr, val = r["addr"], r["val"]
            if r["is_ret"]:
                val_style = "bold yellow"
            elif not r["is_allocated"]:
                # Below RSP: no live frame owns this slot.
                if r["is_written"]:
                    val_style = "red3"     # freed but still holds old bytes
                else:
                    val_style = "grey37"   # never touched / red-zone
            elif r["is_written"]:
                val_style = "bold green"
            else:
                val_style = "grey70"       # allocated but unwritten
            addr_style = "bold" if r["is_rsp"] else "dim"
            addr_text = Text(f"0x{addr:016X}", style=addr_style)
            val_text = Text(_fmt_hex_size(val, size), style=val_style)
            note_text = Text(" ".join(r["notes"]),
                             style="bold" if r["is_rsp"] else "dim")
            row_style = "on grey23" if r["is_rsp"] else None
            cur.add_row(addr_text, val_text, note_text, style=row_style)
            if r["is_rsp"]:
                self._rsp_line = line_index
            line_index += 1

        if cur.row_count:
            parts.append(cur)
        parts.append(Text("low", style="dim italic"))
        return Group(*parts)

# ---- Code view ----------------------------------------------------------

class CodeView(Static):
    """Highlighted source with an arrow at the current instruction line."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._source = ""
        self._syntax = "intel"
        self._current_line: Optional[int] = None
        self._error_line: Optional[int] = None

    def on_mount(self) -> None:
        self.border_title = "Code"
        self.update(self._build())

    def update_state(self, source: str, syntax: str,
                     current_line: Optional[int],
                     error_line: Optional[int] = None):
        self._source, self._syntax = source, syntax
        self._current_line, self._error_line = current_line, error_line
        self._refresh_title()
        self.update(self._build())
        self.call_after_refresh(self._scroll_to_pc)

    def _scroll_to_pc(self) -> None:
        """Center the current instruction line in the containing scroller
        so long programs stay navigable while stepping."""
        line = self._current_line if self._current_line is not None else self._error_line
        if line is None:
            return
        parent = self.parent
        if parent is None or not hasattr(parent, "scroll_to"):
            return
        try:
            viewport_h = parent.size.height
        except Exception:
            return
        # +1 accounts for the Syntax renderable's line-number header row.
        target_y = max(0, line + 1 - viewport_h // 2)
        try:
            parent.scroll_to(y=target_y, animate=False)
        except Exception:
            pass

    def _refresh_title(self) -> None:
        title = f"Code ({self._syntax})"
        if self._current_line is not None:
            title += f"  ->  line {self._current_line + 1}"
        if self._error_line is not None:
            title += f"  [!] line {self._error_line + 1}"
        self.border_title = title

    def _build(self):
        if not self._source.strip():
            return Text("(assemble source with F6 to see it here)", style="dim")
        lexer = "nasm" if self._syntax == "intel" else "gas"
        highlight_lines = set()
        if self._current_line is not None:
            highlight_lines.add(self._current_line + 1)
        return Syntax(
            self._source, lexer,
            theme="monokai",
            line_numbers=True,
            highlight_lines=highlight_lines,
            word_wrap=False,
            indent_guides=False,
        )
