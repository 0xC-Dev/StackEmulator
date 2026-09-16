; Recursion — factorial(5). Watch the stack build up several return-address
; frames (yellow) and locals (green), then unwind.

    mov rdi, 5
    call fact
    jmp done

fact:                       ; unsigned long fact(unsigned long n)
    push rbp
    mov  rbp, rsp
    sub  rsp, 16

    cmp  rdi, 1
    jbe  base

    mov  [rbp-8], rdi       ; save n
    dec  rdi
    call fact               ; rax = fact(n-1)
    mov  rcx, [rbp-8]       ; restore n
    imul rax, rcx           ; rax = n * fact(n-1)
    jmp  epilogue

base:
    mov  rax, 1

epilogue:
    mov  rsp, rbp
    pop  rbp
    ret

done:
    nop
