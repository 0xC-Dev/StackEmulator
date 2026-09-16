; CALL and RET — see the return address land on the stack (yellow slot).
; When add_one returns, that slot leaves the visible/live region.

    mov rdi, 41
    call add_one
    mov rbx, rax          ; RBX now holds 42
    jmp done

add_one:
    mov rax, rdi
    add rax, 1
    ret

done:
    nop
