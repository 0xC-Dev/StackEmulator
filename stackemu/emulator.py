"""x86-64 CPU emulator wrapper with snapshotting for step-back support."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from unicorn import (
    Uc,
    UcError,
    UC_ARCH_X86,
    UC_MODE_64,
    UC_HOOK_MEM_WRITE,
    UC_PROT_ALL,
)
from unicorn.x86_const import (
    UC_X86_REG_RAX, UC_X86_REG_RBX, UC_X86_REG_RCX, UC_X86_REG_RDX,
    UC_X86_REG_RSI, UC_X86_REG_RDI, UC_X86_REG_RBP, UC_X86_REG_RSP,
    UC_X86_REG_R8,  UC_X86_REG_R9,  UC_X86_REG_R10, UC_X86_REG_R11,
    UC_X86_REG_R12, UC_X86_REG_R13, UC_X86_REG_R14, UC_X86_REG_R15,
    UC_X86_REG_RIP, UC_X86_REG_EFLAGS,
)
from capstone import Cs, CS_ARCH_X86, CS_MODE_64


CODE_BASE  = 0x0040_0000
CODE_SIZE  = 0x0010_0000            # 1 MB code region
STACK_BASE = 0x7FFE_0000
STACK_SIZE = 0x0002_0000            # 128 KB stack region
STACK_TOP  = STACK_BASE + STACK_SIZE
# Headroom above INITIAL_RSP lets the UI show a few slots that pre-date
# the program's first push (caller frame stubs).
INITIAL_RSP = STACK_TOP - 0x100

REGS = [
    ("RAX", UC_X86_REG_RAX), ("RBX", UC_X86_REG_RBX),
    ("RCX", UC_X86_REG_RCX), ("RDX", UC_X86_REG_RDX),
    ("RSI", UC_X86_REG_RSI), ("RDI", UC_X86_REG_RDI),
    ("RBP", UC_X86_REG_RBP), ("RSP", UC_X86_REG_RSP),
    ("R8",  UC_X86_REG_R8),  ("R9",  UC_X86_REG_R9),
    ("R10", UC_X86_REG_R10), ("R11", UC_X86_REG_R11),
    ("R12", UC_X86_REG_R12), ("R13", UC_X86_REG_R13),
    ("R14", UC_X86_REG_R14), ("R15", UC_X86_REG_R15),
    ("RIP", UC_X86_REG_RIP), ("RFLAGS", UC_X86_REG_EFLAGS),
]
REG_NAMES = [n for n, _ in REGS]


@dataclass
class Snapshot:
    regs: dict = field(default_factory=dict)
    stack_bytes: bytes = b""
    written_mask: bytes = b""
    return_addrs: frozenset = frozenset()
    # Maps RA slot offset -> callee entry address. Used by the UI to
    # label each call frame with the function that owns it.
    callees: dict = field(default_factory=dict)
    instr_addr: int = 0
    instr_text: str = ""
    context: object = None  # opaque Unicorn context handle


class Emulator:
    """Unicorn wrapper.

    Tracks stack writes and return-address slots, and keeps a snapshot
    stack so execution can be stepped backward.
    """

    def __init__(self):
        self.uc: Optional[Uc] = None
        self.cs = Cs(CS_ARCH_X86, CS_MODE_64)
        self.code_bytes = b""
        self.code_end = CODE_BASE
        self.entry_point = CODE_BASE
        self.snapshots: list[Snapshot] = []
        self.written = bytearray(STACK_SIZE)
        self.return_addrs: set[int] = set()
        self.callees: dict[int, int] = {}
        self.halted = False
        self.error: Optional[str] = None

    # ------------------------------------------------------------------ setup

    def load(self, code_bytes: bytes, entry_point: Optional[int] = None) -> None:
        self.code_bytes = code_bytes
        self.entry_point = entry_point if entry_point is not None else CODE_BASE
        self.reset()

    def reset(self) -> None:
        self.uc = Uc(UC_ARCH_X86, UC_MODE_64)
        self.uc.mem_map(CODE_BASE,  CODE_SIZE,  UC_PROT_ALL)
        self.uc.mem_map(STACK_BASE, STACK_SIZE, UC_PROT_ALL)
        if self.code_bytes:
            self.uc.mem_write(CODE_BASE, self.code_bytes)
        self.uc.reg_write(UC_X86_REG_RSP, INITIAL_RSP)
        self.uc.reg_write(UC_X86_REG_RBP, INITIAL_RSP)
        self.uc.reg_write(UC_X86_REG_RIP, self.entry_point)
        self.code_end = CODE_BASE + len(self.code_bytes)
        self.written = bytearray(STACK_SIZE)
        self.return_addrs = set()
        self.callees = {}
        self.snapshots = []
        self.halted = False
        self.error = None
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self._on_write)
        self._save_snapshot()

    # ------------------------------------------------------------------ hooks

    def _on_write(self, uc, access, address, size, value, user_data):
        if STACK_BASE <= address < STACK_TOP:
            off = address - STACK_BASE
            for i in range(size):
                idx = off + i
                if 0 <= idx < STACK_SIZE:
                    self.written[idx] = 1

    # -------------------------------------------------------------- snapshots

    def _disasm_at(self, addr: int) -> str:
        if addr < CODE_BASE or addr >= self.code_end:
            return "(outside code)"
        try:
            data = bytes(self.uc.mem_read(addr, min(15, self.code_end - addr)))
        except UcError:
            return "??"
        for i in self.cs.disasm(data, addr, count=1):
            op = f" {i.op_str}" if i.op_str else ""
            return f"{i.mnemonic}{op}"
        return "??"

    def _capture(self) -> Snapshot:
        s = Snapshot()
        s.context = self.uc.context_save()
        s.stack_bytes = bytes(self.uc.mem_read(STACK_BASE, STACK_SIZE))
        s.written_mask = bytes(self.written)
        s.return_addrs = frozenset(self.return_addrs)
        s.callees = dict(self.callees)
        s.regs = {name: self.uc.reg_read(rid) for name, rid in REGS}
        s.instr_addr = s.regs["RIP"]
        s.instr_text = self._disasm_at(s.instr_addr)
        return s

    def _save_snapshot(self) -> None:
        self.snapshots.append(self._capture())

    # -------------------------------------------------------------- execution

    def step(self) -> bool:
        """Execute one instruction. Returns True if a step occurred."""
        if self.halted or self.uc is None:
            return False
        rip = self.uc.reg_read(UC_X86_REG_RIP)
        if rip < CODE_BASE or rip >= self.code_end:
            self.halted = True
            return False

        # Peek at the current instruction before executing it: CALL needs
        # special handling so the pushed slot can be tagged as a return
        # address once RSP moves.
        is_call = False
        next_rip_after_call = None
        try:
            instr_bytes = bytes(self.uc.mem_read(rip, min(15, self.code_end - rip)))
            for i in self.cs.disasm(instr_bytes, rip, count=1):
                if i.mnemonic == "call":
                    is_call = True
                    next_rip_after_call = rip + i.size
                break
        except UcError:
            pass

        try:
            self.uc.emu_start(rip, self.code_end, count=1)
        except UcError as e:
            self.error = f"execution error at 0x{rip:016X}: {e}"
            self.halted = True
            return False

        rsp = self.uc.reg_read(UC_X86_REG_RSP)

        if is_call and next_rip_after_call is not None:
            off = rsp - STACK_BASE
            if 0 <= off < STACK_SIZE - 8:
                self.return_addrs.add(off)
                # RIP after a CALL always resolves to the callee entry
                # (including indirect forms like `call rax`). Record it
                # so the UI can name each frame by its owner.
                self.callees[off] = self.uc.reg_read(UC_X86_REG_RIP)

        # Drop RA tags for slots that have been popped off the live stack.
        rsp_off = rsp - STACK_BASE
        self.return_addrs = {o for o in self.return_addrs if o >= rsp_off}
        self.callees = {o: c for o, c in self.callees.items() if o >= rsp_off}

        rip_after = self.uc.reg_read(UC_X86_REG_RIP)
        if rip_after < CODE_BASE or rip_after >= self.code_end:
            self.halted = True

        self._save_snapshot()
        return True

    def run_to_end(self, max_steps: int = 100_000) -> int:
        count = 0
        while not self.halted and count < max_steps:
            if not self.step():
                break
            count += 1
        if count >= max_steps and not self.halted:
            self.error = f"stopped after {max_steps} steps (possible infinite loop)"
            self.halted = True
        return count

    def step_back(self) -> bool:
        if len(self.snapshots) <= 1:
            return False
        self.snapshots.pop()
        s = self.snapshots[-1]
        self.uc.context_restore(s.context)
        self.uc.mem_write(STACK_BASE, s.stack_bytes)
        self.written = bytearray(s.written_mask)
        self.return_addrs = set(s.return_addrs)
        self.callees = dict(s.callees)
        self.halted = False
        self.error = None
        return True

    # ---------------------------------------------------------------- getters

    def current(self) -> Optional[Snapshot]:
        return self.snapshots[-1] if self.snapshots else None

    def previous(self) -> Optional[Snapshot]:
        return self.snapshots[-2] if len(self.snapshots) >= 2 else None

    def step_count(self) -> int:
        # snapshots[0] is the pre-execution state; each successful step
        # appends one snapshot, so count = len - 1.
        return max(0, len(self.snapshots) - 1)
