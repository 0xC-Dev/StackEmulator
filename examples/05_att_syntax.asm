// AT&T syntax is auto-detected from `%reg` and `$imm`.
// Same swap as the default demo, but written the AT&T way.

    movq $0x11111111, %rax
    movq $0x22222222, %rbx

    pushq %rax
    pushq %rbx

    popq %rcx        // rcx = old rbx
    popq %rdx        // rdx = old rax

    movq %rcx, %rax
    movq %rdx, %rbx
