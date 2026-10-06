#!/usr/bin/env python3
"""remote-terminal helper: a small root daemon that tells a user's remote-terminal hook whether the foreground job in
that user's terminal is blocked waiting for terminal input.

Root is needed because nothing else can see it exactly: on Linux, /proc/<pid>/syscall and /proc/<pid>/mem of another
process (and of any setuid program such as sudo) need ptrace rights; on macOS, no unprivileged interface reports
what a blocked thread waits for, and kdebug (the kernel trace behind fs_usage and ktrace) is root-only. Both work
with SIP on.

A thread is waiting for terminal input when it is blocked in:
  read/readv           on a file descriptor that is the terminal
  select/poll/epoll    whose read set holds the terminal (Linux reads the set from the process's memory; macOS cannot,
                       so there the set is narrowed to the fds that can block, and is undecidable when it mixes the
                       terminal with sockets or pipes)
  kevent               on a kqueue with an enabled EVFILT_READ knote on the terminal (macOS)

Protocol: newline-delimited JSON over a Unix socket.
  {"op": "hello"}                         -> {"version": N, "source": sha256 of this file, so the hook can tell
                                             when the plugin ships a newer helper}
  {"op": "watch"}                         -> {"ok": true} once tracing runs (macOS needs it running before the
                                             command starts; Linux needs nothing)
  {"op": "state", "tty": "/dev/ttys003"}  -> {"state": "waiting" | "running" | "undecided" | "idle", "detail": "..."}
Answers cover only a terminal the caller owns, and only processes whose real uid is the caller's.
"""
import ctypes, ctypes.util, errno, hashlib, json, os, platform, socket, stat, struct, sys, threading, time
from pathlib import Path

VERSION = 1
SOURCE = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
MACOS = platform.system() == "Darwin"
SOCKET = os.environ.get("REMOTE_TERMINAL_HELPER_SOCKET") or (
    "/var/run/remote-terminal-helper.sock" if MACOS else "/run/remote-terminal-helper.sock")


def log(*parts):
    print(time.strftime("%F %T"), *parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------------------------
# Linux


class Linux:
    # Syscall numbers: x86_64 and aarch64 (aarch64 has only the p-variants of select/poll/epoll_wait).
    TABLES = {
        "x86_64": {0: "read", 19: "readv", 23: "select", 7: "poll", 270: "pselect6", 271: "ppoll",
                   232: "epoll_wait", 281: "epoll_pwait", 441: "epoll_pwait2"},
        "aarch64": {63: "read", 65: "readv", 72: "pselect6", 73: "ppoll", 22: "epoll_pwait", 441: "epoll_pwait2"},
    }

    def __init__(self):
        machine = platform.machine()
        self.calls = self.TABLES.get("aarch64" if machine in ("arm64", "aarch64") else machine, self.TABLES["x86_64"])

    def watch(self):
        return True

    def stop(self):
        pass

    @staticmethod
    def real_uid(pid):
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
        return None

    @staticmethod
    def stat_fields(pid):
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw.rsplit(")", 1)[1].split()  # fields from 3 (state) on

    def foreground(self, tty_rdev):
        """Pids of the terminal's foreground process group."""
        pids = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                f = self.stat_fields(d.name)
            except OSError:
                continue
            # f[0]=state f[1]=ppid f[2]=pgrp f[3]=session f[4]=tty_nr f[5]=tpgid
            if int(f[4]) == tty_rdev and f[2] == f[5]:
                pids.append(int(d.name))
        return pids

    @staticmethod
    def fd_is_tty(pid, fd, tty_path, tty_rdev):
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            return False
        if target == tty_path:
            return True
        if target == "/dev/tty":
            return int(Linux.stat_fields(pid)[4]) == tty_rdev
        return False

    @staticmethod
    def read_mem(pid, address, size):
        with open(f"/proc/{pid}/mem", "rb", 0) as mem:
            mem.seek(address)
            return mem.read(size)

    def classify_thread(self, pid, tid, tty_path, tty_rdev):
        """('waiting' | 'running', detail) for one thread."""
        try:
            raw = Path(f"/proc/{pid}/task/{tid}/syscall").read_text().split()
        except OSError:
            return "running", ""
        if not raw or not raw[0].isdigit():
            return "running", ""  # "running", or -1: blocked outside a syscall
        name = self.calls.get(int(raw[0]))
        if not name:
            return "running", ""
        args = [int(x, 16) for x in raw[1:7]]
        is_tty = lambda fd: self.fd_is_tty(pid, fd, tty_path, tty_rdev)
        if name in ("read", "readv"):
            return ("waiting", f"{name}(fd {args[0]})") if is_tty(args[0]) else ("running", "")
        if name in ("select", "pselect6"):
            nfds, readfds = args[0], args[1]
            if not readfds or nfds <= 0:
                return "running", ""
            bits = int.from_bytes(self.read_mem(pid, readfds, (min(nfds, 1024) + 7) // 8), "little")
            fds = [fd for fd in range(min(nfds, 1024)) if bits >> fd & 1]
            return ("waiting", f"{name} on fd {fds}") if any(map(is_tty, fds)) else ("running", "")
        if name in ("poll", "ppoll"):
            raw_fds = self.read_mem(pid, args[0], 8 * min(args[1], 256))
            fds = [fd for fd, events, _ in struct.iter_unpack("ihh", raw_fds) if events & 0x1]  # POLLIN
            return ("waiting", f"{name} on fd {fds}") if any(map(is_tty, fds)) else ("running", "")
        # epoll: the interest list is in /proc/<pid>/fdinfo/<epfd> as "tfd: N events: X ..." lines.
        try:
            info = Path(f"/proc/{pid}/fdinfo/{args[0]}").read_text().splitlines()
        except OSError:
            return "running", ""
        for line in info:
            parts = line.split()
            if parts[:1] == ["tfd:"] and int(parts[3], 16) & 0x1 and is_tty(int(parts[1])):
                return "waiting", f"{name} on fd {parts[1]}"
        return "running", ""

    def state(self, tty_path, uid):
        tty_rdev = os.stat(tty_path).st_rdev
        pids = []
        for pid in self.foreground(tty_rdev):
            try:
                if self.real_uid(pid) != uid:
                    continue
                pids.append(pid)
                for task in Path(f"/proc/{pid}/task").iterdir():
                    verdict, detail = self.classify_thread(pid, task.name, tty_path, tty_rdev)
                    if verdict == "waiting":
                        return {"state": "waiting", "pid": pid, "detail": f"{self.comm(pid)} {detail}"}
            except OSError:  # it exited meanwhile, as short-lived processes in a build do all the time
                continue
        if not pids:
            return {"state": "idle", "detail": "no foreground process"}
        return {"state": "running", "pids": pids}

    @staticmethod
    def comm(pid):
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return str(pid)


# ---------------------------------------------------------------------------------------------------------------
# macOS


class Mac:
    CTL_KERN, KERN_KDEBUG = 1, 24
    KDEFLAGS, KDENABLE, KDSETBUF, KDGETBUF, KDSETUP, KDREMOVE, KDREADTR, KDSET_TYPEFILTER = 1, 3, 4, 5, 6, 7, 10, 22
    KDBG_WRAPPED = 0x008
    EVENTS = 1 << 18          # trace buffer, in 64-byte events
    BSC = 0x040C              # class DBG_BSD, subclass DBG_BSD_EXCP_SC: one event per syscall entry and exit
    CALLS = {3: "read", 120: "readv", 396: "read", 411: "readv",
             93: "select", 407: "select", 394: "pselect", 395: "pselect",
             230: "poll", 417: "poll",
             363: "kevent", 369: "kevent64", 374: "kevent_qos"}
    # libproc
    PIDTBSDINFO, PIDLISTFDS, PIDLISTTHREADIDS, PIDTHREADID64INFO = 3, 1, 28, 15
    FDVNODEPATHINFO, FDKQUEUE_EXTINFO = 2, 9
    FDTYPE_VNODE, FDTYPE_SOCKET, FDTYPE_KQUEUE, FDTYPE_PIPE = 1, 2, 5, 6
    VCHR, VFIFO, VSOCK = 4, 7, 6
    EXTINFO_SIZE, EVFILT_READ, EV_DISABLE = 104, -1, 0x0008

    def __init__(self):
        self.libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self.libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.libproc.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.libproc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        self.lock = threading.Lock()      # guards `blocked`
        self.control = threading.Lock()   # one start or stop of the trace at a time
        self.reader = threading.Lock()    # one reader of the kernel's trace buffer at a time
        self.buffer = ctypes.create_string_buffer(64 * self.EVENTS)
        self.blocked = {}                 # thread id -> (syscall name, arg1, arg2) for threads inside a tracked syscall
        self.tracing = False
        self.watchers = 0
        self.generation = 0               # bumped on every start and stop, so an old drain thread knows to quit
        self.leaders = {}                 # terminal device -> pid of the process that has it as controlling terminal

    # --- kdebug ---

    def sysctl(self, *mib, buffer=None, size=None):
        names = (ctypes.c_int * len(mib))(*mib)
        length = ctypes.c_size_t(size or 0)
        if self.libc.sysctl(names, len(mib), buffer, ctypes.byref(length) if buffer is not None else None, None, 0):
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        return length.value

    def kd(self, *args, **kw):
        return self.sysctl(self.CTL_KERN, self.KERN_KDEBUG, *args, **kw)

    def owner(self):
        info = ctypes.create_string_buffer(20)
        self.kd(self.KDGETBUF, buffer=info, size=20)
        nkdbufs, nolog, flags, nthreads, bufid = struct.unpack("iiIii", info.raw)
        return bufid, flags

    def start(self):
        """Caller holds self.control."""
        # A background tracer such as tailspind gives way to us and takes tracing back when we remove ours; the
        # kernel refuses (EBUSY) only while a foreground tool such as Instruments, fs_usage or ktrace holds it.
        try:
            self.kd(self.KDREMOVE)
        except OSError as e:
            if e.errno == errno.EBUSY:
                raise RuntimeError(f"kernel tracing is in use by process {self.owner()[0]} "
                                   "(Instruments, fs_usage or ktrace?)") from e
            raise
        self.kd(self.KDSETBUF, self.EVENTS)
        self.kd(self.KDSETUP)
        bitmap = bytearray(8192)
        bitmap[self.BSC >> 3] |= 1 << (self.BSC & 7)
        self.kd(self.KDSET_TYPEFILTER, buffer=ctypes.create_string_buffer(bytes(bitmap), 8192), size=8192)
        # Waits that began before tracing are invisible, and anything recorded before may be stale. Input the hook
        # sends after this point wakes any blocked reader, so its next wait shows up as a fresh syscall entry.
        with self.lock:
            self.blocked.clear()
        self.kd(self.KDENABLE, 1)
        self.generation += 1
        self.tracing = True
        threading.Thread(target=self.drain, args=(self.generation,), daemon=True).start()
        log("tracing started")

    def stop(self):
        with self.control:
            if self.watchers or not self.tracing:
                return
            self.tracing = False
            self.generation += 1
            with self.reader:
                try:
                    self.kd(self.KDENABLE, 0)
                    self.kd(self.KDREMOVE)
                except OSError as e:
                    log("stop:", e)
            with self.lock:
                self.blocked.clear()
        log("tracing stopped")

    def ingest(self):
        """Reads every event the kernel has buffered and updates which threads are blocked in a tracked syscall."""
        with self.reader:
            if not self.tracing:
                return
            count = self.kd(self.KDREADTR, buffer=self.buffer, size=len(self.buffer))
            _, flags = self.owner()
            with self.lock:
                if flags & self.KDBG_WRAPPED:
                    self.blocked.clear()  # lost events: a thread we think is blocked may have returned
                for _, a1, a2, _, _, tid, debugid, _, _ in struct.iter_unpack("<Q5QIIQ", self.buffer.raw[:64 * count]):
                    if debugid >> 16 != self.BSC:
                        continue
                    name = self.CALLS.get((debugid >> 2) & 0x3FFF)
                    if debugid & 1 and name:     # DBG_FUNC_START of a tracked syscall
                        self.blocked[tid] = (name, a1, a2)
                    else:                        # any other syscall event: the thread is not in a tracked wait
                        self.blocked.pop(tid, None)

    def drain(self, generation):
        """Keeps the kernel's buffer from filling between queries."""
        while self.generation == generation:
            try:
                self.ingest()
            except OSError as e:
                log("read:", e)
                time.sleep(0.5)
            time.sleep(0.02)

    def watch(self):
        with self.control:
            self.watchers += 1
            if not self.tracing:
                try:
                    self.start()
                except Exception:
                    self.watchers -= 1
                    raise
        return True

    def unwatch(self):
        with self.control:
            self.watchers -= 1
        threading.Timer(5, self.stop).start()  # keep tracing briefly for the next command

    # --- libproc ---

    def bsdinfo(self, pid):
        buf = ctypes.create_string_buffer(136)
        if self.libproc.proc_pidinfo(pid, self.PIDTBSDINFO, 0, buf, 136) != 136:
            return None
        f = struct.unpack_from("<12I", buf.raw)
        pgid, _, tdev, tpgid = struct.unpack_from("<4I", buf.raw, 100)
        return {"ruid": f[7], "pgid": pgid, "tdev": tdev, "tpgid": tpgid}

    def pgrp(self, pgid):
        buf = (ctypes.c_int * 4096)()
        n = self.libproc.proc_listpids(2, pgid, buf, ctypes.sizeof(buf))  # PROC_PGRP_ONLY
        return [p for p in buf[: n // 4] if p]

    def thread_ids(self, pid):
        buf = (ctypes.c_uint64 * 4096)()
        n = self.libproc.proc_pidinfo(pid, self.PIDLISTTHREADIDS, 0, buf, ctypes.sizeof(buf))
        return list(buf[: max(n, 0) // 8])

    def fds(self, pid):
        """{fd: (type, vnode type, rdev, path)} of a process."""
        buf = ctypes.create_string_buffer(8 * 4096)
        n = self.libproc.proc_pidinfo(pid, self.PIDLISTFDS, 0, buf, len(buf))
        result = {}
        for fd, kind in struct.iter_unpack("<iI", buf.raw[: max(n, 0)]):
            vtype, rdev, path = None, None, ""
            if kind == self.FDTYPE_VNODE:
                info = ctypes.create_string_buffer(1200)
                if self.libproc.proc_pidfdinfo(pid, fd, self.FDVNODEPATHINFO, info, 1200) > 0:
                    rdev, = struct.unpack_from("<I", info.raw, 24 + 116)
                    vtype, = struct.unpack_from("<i", info.raw, 24 + 136)
                    path = info.raw[176:176 + 1024].split(b"\0", 1)[0].decode(errors="replace")
            result[fd] = (kind, vtype, rdev, path)
        return result

    def kqueue_reads(self, pid, kq):
        """fds with an enabled EVFILT_READ knote on kqueue <kq>."""
        buf = ctypes.create_string_buffer(self.EXTINFO_SIZE * 512)
        count = self.libproc.proc_pidfdinfo(pid, kq, self.FDKQUEUE_EXTINFO, buf, len(buf))
        idents = []
        for i in range(max(count, 0)):
            ident, filt, flags = struct.unpack_from("<QhH", buf.raw, i * self.EXTINFO_SIZE)
            if filt == self.EVFILT_READ and not flags & self.EV_DISABLE:
                idents.append(ident)
        return idents

    def state(self, tty_path, uid):
        tty_rdev = os.stat(tty_path).st_rdev
        procs = {}
        for pid in self.pgrp_of_tty(tty_path, tty_rdev):
            info = self.bsdinfo(pid)
            if info and info["ruid"] == uid:
                procs[pid] = info
        if not procs:
            return {"state": "idle", "detail": "no foreground process"}
        self.ingest()  # include everything up to now, so a read that just returned isn't still counted as waiting
        with self.lock:
            blocked = dict(self.blocked)
        undecided = []
        for pid, info in procs.items():
            fds = None
            for tid in self.thread_ids(pid):
                wait = blocked.get(tid)
                if not wait:
                    continue
                fds = fds if fds is not None else self.fds(pid)
                is_tty = lambda fd: fd in fds and fds[fd][0] == self.FDTYPE_VNODE and (
                    fds[fd][2] == tty_rdev or (fds[fd][3] == "/dev/tty" and info["tdev"] == tty_rdev))
                can_block = lambda fd: fd in fds and (
                    fds[fd][0] in (self.FDTYPE_SOCKET, self.FDTYPE_PIPE, self.FDTYPE_KQUEUE)
                    or fds[fd][1] in (self.VCHR, self.VFIFO, self.VSOCK))
                name, a1, a2 = wait
                if name in ("read", "readv"):
                    if is_tty(a1):
                        return {"state": "waiting", "pid": pid, "detail": f"{name}(fd {a1})"}
                    continue
                if name.startswith("kevent"):
                    if any(is_tty(fd) for fd in self.kqueue_reads(pid, a1)):
                        return {"state": "waiting", "pid": pid, "detail": f"{name} on the terminal"}
                    continue
                # select/pselect (nfds = a1) or poll (set unknown): the set can only hold fds that can block.
                limit = a1 if name in ("select", "pselect") else max(fds, default=-1) + 1
                candidates = [fd for fd in range(limit) if can_block(fd)]
                ttys = [fd for fd in candidates if is_tty(fd)]
                if ttys and len(ttys) == len(candidates):
                    return {"state": "waiting", "pid": pid, "detail": f"{name} on fd {ttys}"}
                if ttys:
                    others = [fd for fd in candidates if fd not in ttys]
                    undecided.append(f"{name} on the terminal and fd {others}")
        if undecided:
            return {"state": "undecided", "detail": "; ".join(undecided)}
        return {"state": "running", "pids": list(procs)}

    def pgrp_of_tty(self, tty_path, tty_rdev):
        """The terminal's foreground process group, read from any process that has the terminal as its controlling
        terminal. The process found is remembered, so the scan of the user's processes runs once per terminal."""
        cached = self.leaders.get(tty_rdev)
        info = cached and self.bsdinfo(cached)
        if not (info and info["tdev"] == tty_rdev and info["tpgid"]):
            info = None
            buf = (ctypes.c_int * 8192)()
            n = self.libproc.proc_listpids(4, os.stat(tty_path).st_uid, buf, ctypes.sizeof(buf))  # PROC_RUID_ONLY
            for pid in buf[: n // 4]:
                candidate = pid and self.bsdinfo(pid)
                if candidate and candidate["tdev"] == tty_rdev and candidate["tpgid"]:
                    self.leaders[tty_rdev], info = pid, candidate
                    break
        return self.pgrp(info["tpgid"]) if info else []


# ---------------------------------------------------------------------------------------------------------------
# Server


def peer_uid(conn):
    if MACOS:
        raw = conn.getsockopt(0, 0x001, 76)  # SOL_LOCAL, LOCAL_PEERCRED -> struct xucred
        return struct.unpack_from("<I", raw, 4)[0]
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack("3i", raw)[1]


def serve(conn, backend):
    uid, watching = peer_uid(conn), False
    stream = conn.makefile("rw")
    try:
        for line in stream:
            try:
                request = json.loads(line)
                op = request.get("op")
                if op == "hello":
                    answer = {"version": VERSION, "source": SOURCE}
                elif op == "watch":
                    if not watching:
                        backend.watch()
                        watching = True
                    answer = {"ok": True}
                elif op == "state":
                    tty = request["tty"]
                    st = os.stat(tty)
                    if not stat.S_ISCHR(st.st_mode) or st.st_uid != uid:
                        answer = {"error": f"{tty} is not a terminal you own"}
                    else:
                        answer = backend.state(tty, uid)
                else:
                    answer = {"error": f"unknown op {op!r}"}
            except Exception as e:  # report every failure to the client instead of dropping the connection
                answer = {"error": f"{type(e).__name__}: {e}"}
            stream.write(json.dumps(answer) + "\n")
            stream.flush()
    finally:
        if watching and MACOS:
            backend.unwatch()
        conn.close()


def main():
    if os.geteuid() != 0:
        sys.exit("remote-terminal helper must run as root")
    backend = Mac() if MACOS else Linux()
    try:
        os.unlink(SOCKET)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET)
    os.chmod(SOCKET, 0o666)  # any local user may ask; answers are limited to the caller's own terminal
    server.listen(16)
    log(f"remote-terminal helper {VERSION} listening on {SOCKET}")
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve, args=(conn, backend), daemon=True).start()


if __name__ == "__main__":
    main()
