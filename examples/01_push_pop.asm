; Basic push / pop — watch the stack grow downward and shrink back.
; Intel syntax, x86-64.

mov rax, 0xAAAAAAAA
mov rbx, 0xBBBBBBBB
mov rcx, 0xCCCCCCCC

push rax          ; RSP -= 8, [RSP] = RAX
push rbx          ; RSP -= 8, [RSP] = RBX
push rcx          ; RSP -= 8, [RSP] = RCX

pop rdx           ; RDX = RCX  (RSP += 8)
pop rsi           ; RSI = RBX
pop rdi           ; RDI = RAX
