; Classic function prologue/epilogue with a local variable.
; Watch RBP move, then a local slot get written (green).

    mov rdi, 10
    call square_plus_one
    jmp done

square_plus_one:
    push rbp
    mov  rbp, rsp
    sub  rsp, 16               ; reserve 16 bytes of locals

    mov  qword ptr [rbp-8], rdi  ; store arg into local
    mov  rax, [rbp-8]
    imul rax, rax                 ; rax = rdi * rdi
    add  rax, 1

    mov  rsp, rbp
    pop  rbp
    ret

done:
    nop
