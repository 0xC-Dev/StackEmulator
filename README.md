# StackEmulate

A lightweight, TUI-based x86-64 assembly learning tool. Type or paste
assembly, step through it one instruction at a time, and watch the CPU
registers and the stack change.

This is a tool I vibe coded to help me visualize the Stack and understand 
ASM operations while going through the OST2.fyi Architecture 1001 course.

Check it out - https://p.ost2.fyi/courses/course-v1:OpenSecurityTraining2+Arch1001_x86-64_Asm+2021_v1/about

![alt text](/examples/image.png)

## Install

Requires **Python 3.10+**.

```bash
git clone <this-repo> stackemulate
cd stackemulate
python -m venv .venv
# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

## Run

```bash
python run.py                       # start with the built-in demo
python run.py examples/04_recursion.asm   # start from a specific file
```

## Keys

| Key           | Action                                          |
| ------------- | ----------------------------------------------- |
| `F6`          | Assemble the editor buffer and enter run mode   |
| `F7`          | Return to the editor                            |
| `F10`         | Step one instruction                            |
| `F9`          | Step back (undo one instruction)                |
| `F5`          | Run to end (or until an error / step limit)     |
| `Ctrl+R`      | Reset — reload the assembled bytes, `RIP` = 0   |
| `Ctrl+L`      | Cycle through the bundled example programs     |
| `Ctrl+O`      | Open — put `#file: path/to/x.asm` on line 1     |
| `Ctrl+S`      | Save — same `#file:` convention                 |
| `Ctrl+Q`      | Quit                                            |


## Caveats / scope

StackEmulate is a *learning tool*, not a full debugger. It intentionally
skips:

- OS calls / syscalls — programs run in an isolated CPU sandbox, no OS.
- Heap or dynamic loader — only two memory regions exist: a code region
  and a stack region.
- Sections, symbols, relocations — you write flat assembly starting at
  `0x00400000`; labels are resolved by the assembler within that image.

For those, use a real debugger. For learning what the stack *feels* like
as instructions execute.
