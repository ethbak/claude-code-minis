# How remote-terminal knows a program is waiting for input

When you send a command with `!` from the Claude app, remote-terminal replies as soon as the command finishes or stops to wait for input. That covers a password prompt, a `y/n` question or a REPL. Finishing is easy to see, because the shell's prompt hook runs. Waiting is harder. Guessing from a quiet screen fails both ways: a slow build looks idle, and a prompt that prints nothing looks busy. remote-terminal asks the kernel instead.

## What counts as waiting

A process in the terminal's foreground job is blocked in a system call that waits for terminal input, and nothing typed is still queued for it:

- `read` or `readv` on a file descriptor that is the terminal (including `/dev/tty`)
- `select`, `poll` or `epoll` whose read set holds the terminal
- `kevent` on a kqueue with an enabled read filter on the terminal (macOS)

The hook asks twice in a row before it accepts "waiting", so a read that returned between your input and the check does not count.

## Why it needs a root helper

Without root, neither macOS nor Linux tells you what another process is blocked in.

On macOS:

- `ps -o wchan` is empty for every blocked process.
- libproc has no wait-channel or syscall information. A thread's flags look the same for a terminal read and a pipe read.
- `ktrace` and `stackshot` need root, even for your own processes.
- `^T` (`TIOCSTAT`) reports only running, waiting or stopped.
- DTrace's syscall provider is not available with System Integrity Protection on.

On Linux:

- `/proc/<pid>/syscall` needs ptrace rights. Under the default `ptrace_scope=1`, an ancestor of the shell can read it.
- Setuid programs such as `sudo`, `su` and `passwd` stay unreadable without root, and those are the password prompts that matter most.

remote-terminal therefore ships a small root service, the remote-terminal helper. The helper answers one question over a Unix socket: is the foreground job of this terminal waiting for input? It answers only about a terminal the caller owns, and only about processes running as the caller.

## Linux

The helper reads `/proc` for each thread in the terminal's foreground process group:

- `/proc/<pid>/task/<tid>/syscall` gives the syscall number and its arguments.
- For `select` and `poll`, it reads the fd sets from `/proc/<pid>/mem` at the addresses in those arguments.
- For `epoll`, it reads the interest list from `/proc/<pid>/fdinfo/<epfd>`.
- `/proc/<pid>/fd` says which descriptors are the terminal.

This is exact for every case.

## macOS

DTrace cannot trace syscalls with SIP on, so the helper uses kdebug, the kernel trace facility behind `fs_usage`, through the `sysctl` interface. kdebug works with SIP on.

While a command runs, the helper traces syscall entries and exits (class `DBG_BSD`, subclass `DBG_BSD_EXCP_SC`). From those events it keeps a map of the threads inside `read`, `readv`, `select`, `poll` or `kevent`. The map also records each call's first two arguments. libproc then lists each process's threads and descriptors, and for `kevent`, the kqueue's registered filters (`PROC_PIDFDKQUEUE_EXTINFO`).

kdebug yields to foreground tracing tools. While Instruments, `fs_usage` or `ktrace` is running, the helper reports that instead of an answer. The background tracer `tailspind` gives way and takes tracing back afterwards.

kdebug cannot read a process's memory, so for `select` and `poll` the helper cannot see the fd set. The helper narrows the set to the descriptors that can block: terminals, pipes, sockets and kqueues.

- If the only candidate is the terminal, the answer is exact. This covers `vim`, `less`, the Python REPL and most prompts.
- If the terminal and a socket or pipe are both candidates, as in an interactive `ssh` session, the helper reports that it cannot tell. The hook then replies once the screen has been still for 3 seconds, and says why.

## What it reports

| Program | Blocked in | Answer |
|---|---|---|
| `cat`, `bash -c 'read x'`, the shell's own `read x`, Python `input()` | `read` on the terminal | waiting |
| Python `select.select([stdin])`, `select.poll()` on stdin | `select` / `poll` on the terminal | waiting |
| `su root -c true` (setuid) | `read` on the terminal | waiting |
| `sleep`, a program reading a pipe | not on the terminal | running |
| `select` on the terminal and a socket (macOS) | `select` | cannot tell |

## Source

- Helper: [`plugins/remote-terminal/scripts/helperd.py`](../plugins/remote-terminal/scripts/helperd.py)
- Hook: [`plugins/remote-terminal/scripts/terminal.py`](../plugins/remote-terminal/scripts/terminal.py)
