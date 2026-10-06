#!/usr/bin/env python3
"""UserPromptSubmit hook behind ! and /! (plugin remote-terminal): a persistent shell per Claude Code chat, driven
from any client, including the claude.ai web and mobile apps.

  !<text>  or  /!<text>    type <text> and Enter (commands, answers to prompts, passwords)
  !        or  /!          show new output only
  !^C      or  /!^C        send a control key (^C, ^D, ^Z, ^[ ...)

In a terminal, Claude Code keeps a plain "!" for its own bash mode, which never reaches hooks; "/!" works there.

Each chat gets its own window in a private tmux server (tmux -L remote-terminal, attach with
`tmux -L remote-terminal attach`), started in the chat's working directory, so the directory, variables and running
programs persist between messages. A window belongs to the Claude Code process that made it (from Claude Code's
own session registry); a window whose owner has exited is closed the next time any chat uses the shell.

The hook replies as soon as one of these is true:
  finished   the shell's prompt hook set the pane title to the next sequence number; tmux handles a program's output
             in order, so everything it printed is on screen by then
  waiting    the remote-terminal helper (a root daemon; see helperd.py) reports a process in the foreground job
             blocked reading the terminal, and nothing typed is still queued for it
  undecided  the helper cannot tell (macOS: select/poll on the terminal and a socket at once) and the screen has
             not changed for a few seconds
  timeout    MAX_WAIT seconds passed; the program keeps running and "!" shows what it printed later
Without the helper, the hook replies once the screen has been still for a few seconds, and says so.
"""
import fcntl, hashlib, json, os, re, shutil, socket, struct, subprocess, sys, termios, time
from pathlib import Path
from claude_env import claude_process, config_dir, install_hint, post_message, remote_session_id, reply

MAX_WAIT = 270             # stays under the hook's 300 s timeout in hooks.json
QUIET = 3.0                # seconds of unchanged screen before an undecided or helper-less wait gives up
CONTEXT_CHARS, MESSAGE_CHARS = 20000, 4000
HISTORY_LIMIT = 50000
SERVER = ["tmux", "-L", "remote-terminal", "-f", "/dev/null"]
SESSION = "remote-terminal"
HELPER_VERSION = 1
HELPER_SOCKET = os.environ.get("REMOTE_TERMINAL_HELPER_SOCKET") or (
    "/var/run/remote-terminal-helper.sock" if sys.platform == "darwin" else "/run/remote-terminal-helper.sock")
ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or Path.home() / ".cache/remote-terminal")
INSTALL_HELPER = f"sudo python3 '{ROOT / 'install_helper.py'}'"
COMMAND = re.compile(r"/?!(.*)", re.S)
CONTROL = re.compile(r"\^([A-Za-z\[\\\]^_])")


def tmux(*args):
    return subprocess.run([*SERVER, *args], capture_output=True, text=True)


def literal(arg):
    """<arg> as tmux must receive it to pass it on unchanged: tmux reads a trailing ";" as a command separator, and
    takes one backslash away from a trailing "\\;"."""
    return arg[:-1] + "\\;" if arg.endswith(";") else arg


def pane(target, fmt):
    return tmux("display", "-p", "-t", target, fmt).stdout.strip()


def history(target):
    return tmux("capture-pane", "-p", "-J", "-S", "-", "-t", target).stdout.rstrip("\n").split("\n")


def screen(target):
    return tmux("capture-pane", "-p", "-t", target).stdout


def tail(text, limit):
    return text if len(text) <= limit else f"[... {len(text) - limit} earlier characters cut ...]\n" + text[-limit:]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def owner():
    """The Claude Code process running this hook, as '<pid>\t<startedAt>\t<config dir>', from Claude Code's session
    registry. The config dir is part of it because each config dir has its own registry, and one tmux server serves
    them all."""
    process = claude_process()
    if process:
        return f"{process[0]}\t{process[2].get('startedAt')}\t{config_dir()}"
    return ""


def owner_gone(tag):
    parts = tag.split("\t")
    if len(parts) != 3:
        return False  # no known owner: leave the window alone
    pid, started, registry = parts
    if not alive(int(pid)):
        return True
    try:
        entry = Path(registry) / "sessions" / f"{pid}.json"
        return str(json.loads(entry.read_text()).get("startedAt")) != started
    except (OSError, ValueError):
        return True


def close_orphaned_windows():
    out = tmux("list-windows", "-t", SESSION, "-F", "#{window_name}\t#{@owner}").stdout
    for line in out.splitlines():
        name, _, tag = line.partition("\t")
        if owner_gone(tag):
            tmux("kill-window", "-t", f"{SESSION}:={name}")
            (STATE / f"lines-{name}").unlink(missing_ok=True)


def shell_command():
    """The user's shell if it is bash or zsh (the two whose prompt hook we use), else whichever is installed. Started
    without startup files: a prompt theme that redraws earlier lines would break the "new output" line count."""
    preferred = os.path.basename(os.environ.get("SHELL", ""))
    for name in ([preferred] if preferred in ("bash", "zsh") else []) + ["bash", "zsh"]:
        path = shutil.which(name)
        if path:
            # The prompt hook sets the pane title to rt-<sequence>-<exit code of the last command>.
            if name == "zsh":
                return [path, "-f", "-i"], ("PS1='$ '; RT_SEQ=0; "
                                            "precmd() { local e=$?; print -n \"\\e]2;rt-$((++RT_SEQ))-$e\\a\" }; clear")
            return [path, "--norc", "--noprofile", "-i"], (
                "PS1='$ '; RT_SEQ=0; PROMPT_COMMAND='RT_E=$?; printf \"\\033]2;rt-%d-%d\\007\" $((++RT_SEQ)) $RT_E'; "
                "clear")
    return None, None


def ensure_window(name, cwd, tag):
    target = f"{SESSION}:={name}"
    if tmux("has-session", "-t", SESSION).returncode == 0:
        close_orphaned_windows()
        # Exact name check: `display -t` on a missing window silently falls back to the current one.
        if name in tmux("list-windows", "-t", SESSION, "-F", "#{window_name}").stdout.split("\n"):
            return target
    # Closing the last orphaned window ends the tmux server, so look again.
    exists = tmux("has-session", "-t", SESSION).returncode == 0
    shell, setup = shell_command()
    STATE.mkdir(parents=True, exist_ok=True)
    start = ["-c", literal(cwd), "-e", f"PATH={os.environ.get('PATH', '')}", *shell]
    # Some tmux versions apply the limit only to panes created after it is set. Another chat's hook may start the
    # server in between, which makes new-session fail.
    if exists or tmux("start-server", ";", "set-option", "-g", "history-limit", str(HISTORY_LIMIT), ";",
                      "new-session", "-d", "-s", SESSION, "-n", name, "-x", "120", "-y", "40", *start).returncode:
        tmux("new-window", "-d", "-t", f"{SESSION}:", "-n", name, *start)
    for option, value in (("allow-rename", "off"), ("automatic-rename", "off"), ("@owner", tag)):
        tmux("set-option", "-w", "-t", target, option, value)
    tmux("send-keys", "-t", target, "-l", setup)
    tmux("send-keys", "-t", target, "Enter")
    deadline = time.time() + 10
    while not pane(target, "#{pane_title}").startswith("rt-"):
        if time.time() > deadline:
            raise RuntimeError("the shell did not start its prompt hook")
        time.sleep(0.05)
    tmux("clear-history", "-t", target)
    (STATE / f"lines-{name}").write_text(str(len(history(target)) - 1))
    return target


def tty_queue(tty, request):
    fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        return struct.unpack("i", fcntl.ioctl(fd, request, b"\0\0\0\0"))[0]
    finally:
        os.close(fd)


class Helper:
    """Connection to the remote-terminal helper; `problem` says why there is none."""

    def __init__(self):
        self.stream, self.problem, self.stale = None, None, False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(HELPER_SOCKET)
            self.stream = sock.makefile("rw")
        except OSError:
            self.problem = "missing"
            return
        hello = self.ask({"op": "hello"})
        if hello.get("version") != HELPER_VERSION:
            self.problem, self.stream = "outdated", None
            return
        # Same protocol, older code: still usable, but the user should install the plugin's copy.
        self.stale = hello.get("source") != hashlib.sha256((ROOT / "helperd.py").read_bytes()).hexdigest()
        answer = self.ask({"op": "watch"})
        if not answer.get("ok"):
            self.problem, self.stream = f"broken: {answer.get('error')}", None

    def ask(self, request):
        try:
            self.stream.write(json.dumps(request) + "\n")
            self.stream.flush()
            return json.loads(self.stream.readline() or "{}")
        except (OSError, ValueError) as e:
            return {"error": str(e)}


def wait(target, tty, title, helper):
    """Waits for the typed input to finish, ask for more, or time out; returns a status line or None."""
    start, last_screen, changed_at, waiting = time.time(), screen(target), time.time(), 0
    while True:
        time.sleep(0.05)
        if pane(target, "#{pane_title}") != title:
            return None
        now, current = time.time(), screen(target)
        if current != last_screen:
            last_screen, changed_at = current, now
        quiet = now - changed_at >= QUIET
        answer = helper.ask({"op": "state", "tty": tty}) if helper.stream else {}
        if "error" in answer:
            helper.problem, helper.stream = f"broken: {answer['error']}", None
        state = answer.get("state")
        # Two answers in a row, so a read that returned between our input and the helper's look doesn't count.
        waiting = waiting + 1 if state == "waiting" and tty_queue(tty, termios.FIONREAD) == 0 else 0
        if waiting >= 2:
            return "waiting for input"
        if state == "undecided" and quiet:
            return f"still running; can't tell whether it waits for input ({answer.get('detail')})"
        if not helper.stream and quiet:
            return f"still running; no new output for {QUIET:.0f} s"
        if now - start > MAX_WAIT:
            return f"still running after {MAX_WAIT} s"


def status_markdown(status):
    """How the reply shows a command that didn't finish, as a blockquote; nothing for one that did."""
    if not status:
        return ""
    if status == "waiting for input":
        return "> **Waiting for input.** Your next `!` message is the answer."
    if status.startswith("still running after"):
        return f"> **Still running** after {MAX_WAIT} seconds. Send `!` to see what it prints next."
    if "can't tell" in status:
        return "> **Still running.** It may be waiting for input; send `!` to check again."
    return f"> **Still running.** No new output for {QUIET:.0f} seconds."


def helper_note(helper):
    if helper.stale and not helper.problem:
        return (" The installed remote-terminal helper is older than this plugin's copy. Tell the user in one short "
                f"line, and offer to update it with: {INSTALL_HELPER} (it needs sudo).")
    if not helper.problem:
        return ""
    why = {"missing": "is not installed", "outdated": "is out of date"}.get(helper.problem, f"is {helper.problem}")
    return (f" The remote-terminal helper {why}, so the hook could not tell exactly whether the program waits for "
            f"input. Tell the user in one short line, and offer to install it with: {INSTALL_HELPER} (it needs sudo).")


def main():
    data = json.load(sys.stdin)
    command = COMMAND.match(data.get("prompt", ""))
    if not command:
        return
    if not shutil.which("tmux"):
        hint = install_hint("tmux")
        reply("The remote-terminal hook needs tmux, which is not installed. Tell the user and offer to install it"
              + (f" with: {hint}" if hint else "") + ". Do not run the user's command yourself.",
              "remote-terminal needs tmux" + (f": {hint}" if hint else ""))
        return
    if not shell_command()[0]:
        reply("The remote-terminal hook needs bash or zsh, and neither is installed. Tell the user.",
              "remote-terminal needs bash or zsh")
        return
    text = command.group(1).strip()
    name = (data.get("session_id") or "default")[:8]
    target = ensure_window(name, data.get("cwd") or str(Path.home()), owner())
    tty, title = pane(target, "#{pane_tty}"), pane(target, "#{pane_title}")
    helper = Helper() if text else None
    key, started = CONTROL.fullmatch(text), time.time()
    if key:
        tmux("send-keys", "-t", target, "C-" + key.group(1).lower())
    elif text:
        tmux("send-keys", "-t", target, "-l", literal(text))
        tmux("send-keys", "-t", target, "Enter")
    # A bare "!" sends nothing, so an idle shell would give no signal; it just shows what is new.
    status = wait(target, tty, title, helper) if text else None
    seconds = time.time() - started
    exit_code = pane(target, "#{pane_title}").rsplit("-", 1)[-1] if text and not status else None
    deadline = time.time() + 2
    while tty_queue(tty, termios.TIOCOUTQ) > 0 and time.time() < deadline:  # let tmux read what the program wrote
        time.sleep(0.01)
    lines = history(target)
    marker = STATE / f"lines-{name}"
    seen = int(marker.read_text() or 0) if marker.exists() else 0
    output = "\n".join(lines[seen:] if 0 <= seen < len(lines) else lines[-60:]).rstrip()
    if text and not status and len(lines) > 0.8 * int(pane(target, "#{history_limit}") or HISTORY_LIMIT):
        # tmux drops the oldest tenth of a full history, which would shift the line count. The command finished, so
        # nothing is printing: start the count over first.
        tmux("clear-history", "-t", target)
        lines = history(target)
    # The last line is the prompt or a program's input prompt; the next keys land on it, so the next diff starts there.
    marker.write_text(str(len(lines) - 1))
    note = f"\n[{status}]" if status else ""
    where = pane(target, "#{pane_current_path}").replace(str(Path.home()), "~", 1)
    shown = post_message(cse, chat_message(output, status, where, exit_code, seconds)) \
        if (cse := remote_session_id()) else False
    if shown or not cse:  # in a terminal, the hook's message below shows the output
        context = ("The user ran this in their persistent shell with the \"!\" prefix. The remote-terminal hook already "
                   "ran it and posted the output as a message the user sees right above your reply. Don't run it again, "
                   "and don't quote the output or put any of it in a code block. Reply the way you would to a command "
                   "result: a sentence or two on anything that matters, like an error and what to do about it. If "
                   "there's nothing worth saying, keep it to a few words.")
    else:
        context = ("The user typed this into their persistent shell with the \"!\" prefix; the remote-terminal hook "
                   "already sent it. Do not run it again. Show the user the shell output below in one code block, "
                   "exactly as given (if it is longer than about 80 lines, the last 80, saying how many were cut)"
                   + (f", then this status line under it, exactly as given: {status_markdown(status)}" if status else "")
                   + ".")
    context += (helper_note(helper) if helper else "") + "\n\nShell output:\n" + tail(output + note, CONTEXT_CHARS)
    reply(context, tail(output + note, MESSAGE_CHARS))


def chat_message(output, status, where, exit_code, seconds):
    """The output as the chat shows it: a line with the directory and, once the command finished, its exit code and
    run time; the terminal text in a console block; then how the command stands if it didn't finish."""
    header = [f"`{where}`"]
    if exit_code is not None and exit_code.isdigit():
        header += ["exit 0" if exit_code == "0" else f"**exit {exit_code}**", f"{seconds:.1f}\u00a0s"]
    if not output:
        return " · ".join(header) + "\n\nNo new output."
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", output)), default=0) + 1)
    message = f"{' · '.join(header)}\n\n{fence}console\n{tail(output, CONTEXT_CHARS)}\n{fence}"
    return message + (f"\n\n{status_markdown(status)}" if status else "")

if __name__ == "__main__":
    main()
