"""Assemble x86-64 source and map source lines to instruction addresses.

Supports both Intel and AT&T syntax; the syntax can be auto-detected from the
source (presence of `%reg`/`$imm` tokens implies AT&T) or forced by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from keystone import (
    Ks, KsError,
    KS_ARCH_X86, KS_MODE_64,
    KS_OPT_SYNTAX, KS_OPT_SYNTAX_INTEL, KS_OPT_SYNTAX_ATT,
)
from capstone import Cs, CS_ARCH_X86, CS_MODE_64

from .emulator import CODE_BASE


ATT_HINT_RE = re.compile(r'%[re]?(?:ax|bx|cx|dx|si|di|bp|sp|8|9|1[0-5])\b|\$-?\d')


def detect_syntax(code: str) -> str:
    """Heuristic syntax detection; defaults to Intel unless AT&T tokens
    (`%reg` / `$imm`) are present."""
    return "att" if ATT_HINT_RE.search(code) else "intel"


@dataclass
class AsmResult:
    code_bytes: bytes
    syntax: str                     # "intel" or "att"
    line_to_addr: dict              # 0-indexed source line -> instr address
    addr_to_line: dict              # instr address -> 0-indexed source line
    entry_point: int                # address to start execution at
    entry_label: Optional[str]      # name of the label chosen, or None
    used_subregs: set               # set of sub-register names referenced


# x86-64 sub-register hierarchy. Each parent 64-bit register maps to a list
# of (name, kind) tuples. `kind` is: 32, 16, 8 (low byte), or "hi8"
# (bits 8..15, only valid for AH/BH/CH/DH). Ordered widest to narrowest.
SUBREG_MAP: dict[str, list[tuple[str, object]]] = {
    "RAX": [("EAX", 32), ("AX", 16), ("AH", "hi8"), ("AL", 8)],
    "RBX": [("EBX", 32), ("BX", 16), ("BH", "hi8"), ("BL", 8)],
    "RCX": [("ECX", 32), ("CX", 16), ("CH", "hi8"), ("CL", 8)],
    "RDX": [("EDX", 32), ("DX", 16), ("DH", "hi8"), ("DL", 8)],
    "RSI": [("ESI", 32), ("SI", 16), ("SIL", 8)],
    "RDI": [("EDI", 32), ("DI", 16), ("DIL", 8)],
    "RBP": [("EBP", 32), ("BP", 16), ("BPL", 8)],
    "RSP": [("ESP", 32), ("SP", 16), ("SPL", 8)],
    "R8":  [("R8D",  32), ("R8W",  16), ("R8B",  8)],
    "R9":  [("R9D",  32), ("R9W",  16), ("R9B",  8)],
    "R10": [("R10D", 32), ("R10W", 16), ("R10B", 8)],
    "R11": [("R11D", 32), ("R11W", 16), ("R11B", 8)],
    "R12": [("R12D", 32), ("R12W", 16), ("R12B", 8)],
    "R13": [("R13D", 32), ("R13W", 16), ("R13B", 8)],
    "R14": [("R14D", 32), ("R14W", 16), ("R14B", 8)],
    "R15": [("R15D", 32), ("R15W", 16), ("R15B", 8)],
}

_ALL_SUBREG_NAMES = {n for subs in SUBREG_MAP.values() for n, _ in subs}
_TOKEN_RE = re.compile(r'\b([A-Za-z][A-Za-z0-9]*)\b')


def detect_used_subregs(code: str) -> set:
    """Return the set of sub-register names that appear in the source."""
    used = set()
    stripped = _strip_comments(code, "intel")
    for m in _TOKEN_RE.finditer(stripped):
        tok = m.group(1).upper()
        if tok in _ALL_SUBREG_NAMES:
            used.add(tok)
    return used


class AsmError(Exception):
    """Raised when assembly fails; carries an optional source line number."""

    def __init__(self, message: str, line: Optional[int] = None):
        super().__init__(message)
        self.line = line


def _strip_comment_line(raw_line: str) -> str:
    """Strip a trailing ';' / '#' / '//' comment, preserving line count."""
    line = raw_line
    for marker in (";", "#", "//"):
        idx = line.find(marker)
        if idx != -1:
            line = line[:idx]
    return line


def _strip_comments(code: str, syntax: str) -> str:
    return "\n".join(_strip_comment_line(l) for l in code.split("\n"))


# MASM / IDA-style hex suffix:  18h, 7A11h, 0FFh  ->  0x18, 0x7A11, 0xFF
# The MASM rule is "starts with a decimal digit, ends in h/H"; that's what
# distinguishes a hex literal from an identifier that happens to end in 'h'.
_MASM_HEX_RE = re.compile(r'\b(\d[0-9A-Fa-f]*)[hH]\b')


def _translate_masm_hex(code: str) -> str:
    """Convert MASM/IDA `<digits>h` hex literals to NASM `0x...` form.

    Keystone only accepts `0x...` hex. MASM requires hex literals to
    start with a decimal digit, so the pattern cannot collide with
    symbols ending in `h`.
    """
    return _MASM_HEX_RE.sub(lambda m: f"0x{m.group(1)}", code)


def _is_instruction_line(raw_line: str, syntax: str) -> tuple[bool, str]:
    """Decide whether a source line emits an instruction.

    Returns (is_instruction, stripped_body). Handles comments (';' Intel /
    '#' AT&T, also '//' either), directives (leading '.'), and bare labels.
    A `label: instr` on one line counts as one instruction.
    """
    line = raw_line
    # Strip inline comments. Assemblers accept both ';' and '#' commonly.
    for c in (";", "#"):
        if c in line:
            line = line.split(c, 1)[0]
    if "//" in line:
        line = line.split("//", 1)[0]
    line = line.strip()
    if not line:
        return False, ""
    if line.startswith("."):        # directive (.text, .globl, etc.)
        return False, ""
    # Bare label:  "name:"
    if re.match(r'^[A-Za-z_.\$][\w.\$]*:\s*$', line):
        return False, ""
    # Label + instruction:  "name: instr ..."
    m = re.match(r'^[A-Za-z_.\$][\w.\$]*:\s*(.+)$', line)
    if m:
        rest = m.group(1).strip()
        if not rest:
            return False, ""
    return True, line


def assemble(code: str, syntax: Optional[str] = None) -> AsmResult:
    if syntax is None:
        syntax = detect_syntax(code)
    if syntax not in ("intel", "att"):
        raise AsmError(f"unknown syntax: {syntax!r}")

    ks = Ks(KS_ARCH_X86, KS_MODE_64)
    ks.syntax = KS_OPT_SYNTAX_ATT if syntax == "att" else KS_OPT_SYNTAX_INTEL

    # Preprocess before handing source to keystone:
    #  1. Strip comments — `;` is a statement separator in Intel mode,
    #     not a comment marker, so leftover text becomes bad instructions.
    #  2. Translate MASM/IDA `<digits>h` hex to NASM `0x...` form.
    #  3. Force ASCII — mnemonics are always ASCII; any non-ASCII lives
    #     inside comments or strings and can safely be replaced.
    cleaned = _strip_comments(code, syntax)
    if syntax == "intel":
        cleaned = _translate_masm_hex(cleaned)
    ascii_code = cleaned.encode("ascii", errors="replace").decode("ascii")

    try:
        encoding, _count = ks.asm(ascii_code, addr=CODE_BASE)
    except KsError as e:
        # Keystone reports the error without a line number; re-assemble
        # line-by-line to locate the first failure.
        bad_line = _find_first_bad_line(code, ks)
        raise AsmError(str(e), line=bad_line) from None

    if not encoding:
        raise AsmError("no instructions were assembled")

    code_bytes = bytes(encoding)
    line_to_addr, addr_to_line = _build_line_map(code, code_bytes, syntax)
    entry_point, entry_label = _find_entry_point(code, line_to_addr)
    used_subregs = detect_used_subregs(code)
    return AsmResult(code_bytes, syntax, line_to_addr, addr_to_line,
                     entry_point, entry_label, used_subregs)


# Entry-point label candidates in priority order. Fallback is CODE_BASE
# (top of the assembled bytes) for sources without a main-style entry.
_ENTRY_CANDIDATES = ("main", "_main", "_start", "start", "WinMain", "wmain")


def _find_entry_point(code: str, line_to_addr: dict) -> tuple[int, Optional[str]]:
    """Pick a start address: prefer a main/_start-style label, else CODE_BASE.

    Resolves each label to the first instruction on-or-after its source
    line, so a label immediately followed by comments still points at the
    right address.
    """
    label_line: dict[str, int] = {}
    label_re = re.compile(r'^\s*([A-Za-z_.\$][\w.\$]*)\s*:')
    for i, raw in enumerate(code.split("\n")):
        line = _strip_comment_line(raw)
        m = label_re.match(line)
        if m:
            label_line.setdefault(m.group(1), i)

    if not line_to_addr:
        return CODE_BASE, None

    sorted_instr_lines = sorted(line_to_addr.keys())

    def first_addr_at_or_after(line_no: int) -> Optional[int]:
        for ln in sorted_instr_lines:
            if ln >= line_no:
                return line_to_addr[ln]
        return None

    for name in _ENTRY_CANDIDATES:
        if name in label_line:
            addr = first_addr_at_or_after(label_line[name])
            if addr is not None:
                return addr, name
    return CODE_BASE, None


def _find_first_bad_line(code: str, ks: Ks) -> Optional[int]:
    """Locate the first source line whose isolated assembly fails."""
    for i, raw in enumerate(code.split("\n")):
        is_ins, body = _is_instruction_line(raw, "intel")
        if not is_ins:
            continue
        body_x = _translate_masm_hex(body)
        body_ascii = body_x.encode("ascii", errors="replace").decode("ascii")
        try:
            ks.asm(body_ascii, addr=CODE_BASE)
        except KsError:
            return i
    return None


def _build_line_map(code: str, code_bytes: bytes, syntax: str) -> tuple[dict, dict]:
    """Bidirectional line <-> instruction-address map.

    Walks the source and the disassembled bytes in parallel. Each
    non-empty, non-comment, non-label source line corresponds to
    exactly one emitted instruction.
    """
    cs = Cs(CS_ARCH_X86, CS_MODE_64)
    disasm = list(cs.disasm(code_bytes, CODE_BASE))
    line_to_addr: dict[int, int] = {}
    addr_to_line: dict[int, int] = {}
    idx = 0
    for i, raw in enumerate(code.split("\n")):
        is_ins, _ = _is_instruction_line(raw, syntax)
        if not is_ins:
            continue
        if idx >= len(disasm):
            break
        addr = disasm[idx].address
        line_to_addr[i] = addr
        addr_to_line[addr] = i
        idx += 1
    return line_to_addr, addr_to_line
