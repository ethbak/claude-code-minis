"""Drives the remote-terminal hook through a real tmux shell. With the helper installed (CI installs it), also checks
that every case comes back as waiting or not, exactly."""
import json, os, platform, shutil, socket, subprocess, sys, time, uuid
import pytest
from conftest import PLUGINS, run_hook

TERMINAL = PLUGINS / "remote-terminal" / "scripts" / "terminal.py"
sys.path.insert(0, str(TERMINAL.parent))
import terminal  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")


def no_session(tmp_path):
    """An empty Claude Code config dir, so the hook finds no Claude Code session above it. Run from a Remote Control
    chat, it would otherwise post its replies into that chat."""
    (tmp_path / "no-session").mkdir(exist_ok=True)
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "no-session")}
HELPER = os.path.exists(terminal.HELPER_SOCKET)
PY = "/usr/bin/python3" if platform.system() == "Darwin" else "python3"


@pytest.fixture
def shell(tmp_path):
    session = f"test-{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path), **no_session(tmp_path)}

    def send(prompt):
        reply = run_hook(TERMINAL, {"prompt": prompt, "session_id": session, "cwd": str(tmp_path)}, env=env)
        return reply["systemMessage"] if reply else None
    send.title = lambda: subprocess.run([*terminal.SERVER, "display", "-p", "-t", f"{terminal.SESSION}:={session[:8]}",
                                         "#{pane_title}"], capture_output=True, text=True).stdout.strip()
    yield send
    subprocess.run([*terminal.SERVER, "kill-window", "-t", f"{terminal.SESSION}:={session[:8]}"], capture_output=True)


@pytest.mark.parametrize("login_shell", ["bash", "zsh"])
def test_prompt_hook_reports_the_exit_code(shell, monkeypatch, login_shell):
    if not shutil.which(login_shell):
        pytest.skip(f"no {login_shell}")
    monkeypatch.setenv("SHELL", login_shell)
    shell("!false")
    assert shell.title().endswith("-1")
    shell("!true")
    assert shell.title().endswith("-0")


def test_chat_message_reads_like_a_terminal():
    finished = terminal.chat_message("$ make\nmake: *** No rule.\n$", None, "~/app", "2", 0.43)
    assert finished == "`~/app` · **exit 2** · 0.4\u00a0s\n\n```console\n$ make\nmake: *** No rule.\n$\n```"
    waiting = terminal.chat_message("$ sudo ls\nPassword:", "waiting for input", "~", None, 1.0)
    assert waiting.startswith("`~`\n\n```console\n") and waiting.endswith("> **Waiting for input.** Your next `!` "
                                                                         "message is the answer.")
    assert terminal.chat_message("say ```hi```", None, "~", "0", 0).count("````") == 2  # fence outlasts the output's


def test_ignores_other_prompts(shell):
    assert shell("hello") is None
    assert shell("why ! here") is None


def test_runs_and_keeps_state(shell, tmp_path):
    out = shell("!cd / && export RT_X=42")
    assert "[" not in out.splitlines()[-1]
    out = shell("/!pwd; echo $RT_X")
    assert "\n/\n42" in out


def test_trailing_semicolons_reach_the_shell(shell):
    assert "\nsemi\n" in shell("!echo semi;")
    assert "\nescaped ;\n" in shell("!echo escaped \\;")
    assert "\nbackslash\\\n" in shell("!echo backslash\\\\;")  # the shell gets: echo backslash\\;


def test_returns_when_finished_not_before(shell):
    start = time.time()
    out = shell(f"!{PY} -c 'import time; time.sleep(2); print(\"done\")'")
    assert "done" in out and 2 <= time.time() - start < 10


def test_long_output_after_history_fills(shell):
    # Once a pane's history is full (50000 lines), tmux drops the oldest tenth of it, so a line count alone would
    # lose its place. All 6000 lines must come back (the message keeps the last 4000 characters of them).
    shell("!seq 60000")
    limits = subprocess.run([*terminal.SERVER, "list-panes", "-a", "-F", "#{history_limit}"], capture_output=True,
                            text=True).stdout.split()
    assert set(limits) == {str(terminal.HISTORY_LIMIT)}
    out = shell("!seq 6000")
    assert "earlier characters cut" in out and out.endswith("5999\n6000\n" + out.splitlines()[-1])


def test_bare_bang_shows_new_output(shell):
    shell("!echo first")
    assert "first" not in shell("!")


@pytest.mark.skipif(not HELPER, reason="needs the remote-terminal helper")
@pytest.mark.parametrize("command, end", [
    ("cat", "^C"),
    ("bash -c 'read x'", "^C"),
    (f"{PY} -c 'input(\"name? \")'", "^C"),
    (f"{PY} -c 'import select,sys; select.select([sys.stdin],[],[])'", "^C"),
    (f"{PY} -c 'import select; p=select.poll(); p.register(0, select.POLLIN); p.poll()'", "^C"),
    ("read x", "^C"),
    # setuid: only root can see what su waits for, which is why the helper runs as root. su ignores Ctrl-C at its
    # password prompt, so a wrong password ends it.
    ("su root -c true", "not-the-password"),
])
def test_waiting_for_input(shell, command, end):
    start = time.time()
    out = shell("!" + command)
    assert out.endswith("[waiting for input]") and time.time() - start < 5
    assert "[" not in shell("!" + end).splitlines()[-1]


@pytest.mark.skipif(not HELPER, reason="needs the remote-terminal helper")
def test_answering_a_prompt(shell):
    assert shell(f"!{PY} -c 'print(\"hi \" + input(\"name? \"))'").endswith("[waiting for input]")
    assert "hi ethan" in shell("!ethan")


@pytest.mark.skipif(not HELPER, reason="needs the remote-terminal helper")
def test_reading_a_pipe_is_not_waiting(shell):
    # Runs 4 s and prints at the end; reporting "waiting" would return early without "end".
    out = shell(f"!{PY} -c 'import time; time.sleep(4); print(\"end\")' | cat")
    assert "end" in out and "[" not in out.splitlines()[-1]


@pytest.mark.skipif(not HELPER, reason="needs the remote-terminal helper")
def test_helper_answers_only_about_own_terminals():
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(terminal.HELPER_SOCKET)
    stream = sock.makefile("rw")
    stream.write(json.dumps({"op": "state", "tty": "/dev/null"}) + "\n")
    stream.flush()
    assert "error" in json.loads(stream.readline())


@pytest.mark.skipif(not HELPER, reason="needs the remote-terminal helper")
def test_two_chats_at_once(tmp_path):
    """Two chats starting the helper's trace at the same moment must both get exact answers."""
    from concurrent.futures import ThreadPoolExecutor
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path), **no_session(tmp_path)}
    sessions = [f"test-{uuid.uuid4().hex[:8]}" for _ in range(2)]

    def send(session, prompt):
        reply = run_hook(TERMINAL, {"prompt": prompt, "session_id": session, "cwd": str(tmp_path)}, env=env)
        return reply["systemMessage"]
    try:
        with ThreadPoolExecutor(2) as pool:
            outs = list(pool.map(send, sessions, ["!cat", f"!{PY} -c 'input(\"q? \")'"]))
        assert all(out.endswith("[waiting for input]") for out in outs), outs
    finally:
        for s in sessions:
            subprocess.run([*terminal.SERVER, "kill-window", "-t", f"{terminal.SESSION}:={s[:8]}"], capture_output=True)


def test_other_config_dirs_keep_their_shells(tmp_path):
    """One tmux server serves every Claude Code config dir (e.g. CLAUDE_CONFIG_DIR=~/.claude-work); a chat in one
    must not close another's windows as orphans, and closing the last real orphan must not break the next chat."""
    registry = {}
    for name, started in (("a", 1), ("b", 99)):
        (tmp_path / name / "sessions").mkdir(parents=True)
        # The test process stands in for the Claude Code process that owns the hook; each config dir's registry
        # records it differently, as two real Claude Code processes would be.
        registry[name] = tmp_path / name / "sessions" / f"{os.getpid()}.json"
        registry[name].write_text(json.dumps({"pid": os.getpid(), "startedAt": started}))
    sessions = {name: f"test-{uuid.uuid4().hex[:8]}" for name in ("a", "b", "c")}

    def send(config, session, prompt):
        env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"), "CLAUDE_CONFIG_DIR": str(tmp_path / config)}
        return run_hook(TERMINAL, {"prompt": prompt, "session_id": session, "cwd": str(tmp_path)}, env=env)

    def windows():
        out = subprocess.run([*terminal.SERVER, "list-windows", "-t", terminal.SESSION, "-F", "#{window_name}"],
                             capture_output=True, text=True).stdout
        return set(out.split())
    try:
        send("a", sessions["a"], "!echo from-a")
        send("b", sessions["b"], "!echo from-b")
        assert sessions["a"][:8] in windows()       # b's chat left a's window alone
        registry["a"].write_text(json.dumps({"pid": os.getpid(), "startedAt": 2}))  # a's owner has gone
        assert "from-c" in send("b", sessions["c"], "!echo from-c")["systemMessage"]
        assert sessions["a"][:8] not in windows()   # a's orphan is closed
    finally:
        for s in sessions.values():
            subprocess.run([*terminal.SERVER, "kill-window", "-t", f"{terminal.SESSION}:={s[:8]}"], capture_output=True)


def test_two_chats_start_the_server_at_once(tmp_path):
    if subprocess.run([*terminal.SERVER, "has-session"], capture_output=True).returncode == 0:
        pytest.skip("the remote-terminal tmux server is running; this test needs to start it")
    env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path), **no_session(tmp_path)}
    sessions = [f"race{i}-{uuid.uuid4().hex[:8]}" for i in range(2)]
    procs = [subprocess.Popen([sys.executable, str(TERMINAL)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env) for _ in sessions]
    try:
        for proc, session in zip(procs, sessions):
            proc.stdin.write(json.dumps({"prompt": f"!echo hi-{session}", "session_id": session, "cwd": str(tmp_path)}))
            proc.stdin.close()
        for proc, session in zip(procs, sessions):
            proc.wait(30)
            assert proc.returncode == 0, proc.stderr.read()
            assert f"\nhi-{session}\n" in json.loads(proc.stdout.read())["systemMessage"]
    finally:
        for session in sessions:
            subprocess.run([*terminal.SERVER, "kill-window", "-t", f"{terminal.SESSION}:={session[:8]}"],
                           capture_output=True)


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux helper backend")
def test_helper_skips_processes_that_exit_while_it_looks(monkeypatch):
    import helperd
    linux = helperd.Linux()
    gone = 2 ** 22 + 12345  # above pid_max's default, so no such process
    monkeypatch.setattr(linux, "foreground", lambda rdev: [gone, os.getpid()])
    master, slave = os.openpty()
    try:
        assert linux.state(os.ttyname(slave), os.getuid()) == {"state": "running", "pids": [os.getpid()]}
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(not HELPER, reason="needs the remote-terminal helper")
def test_helper_from_an_older_plugin_is_flagged(tmp_path, monkeypatch):
    assert not terminal.Helper().stale  # the helper this checkout installed
    (tmp_path / "helperd.py").write_text("# a newer helper\n")
    monkeypatch.setattr(terminal, "ROOT", tmp_path)
    helper = terminal.Helper()
    assert helper.stream and helper.stale and "older than this plugin's copy" in terminal.helper_note(helper)
