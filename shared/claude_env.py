"""What the claude-code-minis hooks need from Claude Code itself: its config directory, the claude.ai login, the
Remote Control session a hook runs in, and the claude.ai API. Each plugin ships an identical copy of this file
(tools/sync_shared.py copies it; CI fails if a copy drifts). Python 3.9 standard library only."""
import hashlib, json, os, platform, re, shutil, subprocess, time, unicodedata, urllib.error, urllib.request, uuid
from pathlib import Path

API = "https://api.anthropic.com/v1"
MACOS = platform.system() == "Darwin"


class Problem(Exception):
    """Something the user has to fix; the message says what and how."""


def config_dir():
    """Claude Code's config directory: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(value).expanduser() if value else Path.home() / ".claude"


def global_config():
    """~/.claude.json, or .claude.json inside $CLAUDE_CONFIG_DIR when that is set."""
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    for path in ([Path(value).expanduser() / ".claude.json"] if value else []) + [Path.home() / ".claude.json"]:
        if path.exists():
            return json.loads(path.read_text())
    return {}


def keychain_service():
    """The macOS Keychain item Claude Code keeps its login in. With $CLAUDE_CONFIG_DIR set, the name carries the first
    8 hex digits of the directory's SHA-256, so each config directory has its own login."""
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    suffix = "-" + hashlib.sha256(unicodedata.normalize("NFC", value).encode()).hexdigest()[:8] if value else ""
    return "Claude Code-credentials" + suffix


_token = (None, 0.0)


def oauth_token():
    """The claude.ai access token of the running Claude Code login. Linux keeps it in <config>/.credentials.json;
    macOS keeps it in the Keychain and falls back to that file when the Keychain refuses the write. Read again after
    a minute, since Claude Code refreshes it and some of these scripts run for many minutes."""
    global _token
    if _token[0] and time.time() - _token[1] < 60:
        return _token[0]
    path = config_dir() / ".credentials.json"
    raw = path.read_text() if path.exists() else None
    if raw is None and MACOS:
        found = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", keychain_service(), "-w"],
                               capture_output=True, text=True)
        raw = found.stdout if found.returncode == 0 else None
    try:
        token = json.loads(raw)["claudeAiOauth"]["accessToken"] if raw else None
    except (ValueError, KeyError, TypeError):
        token = None
    if not token:
        raise Problem("no claude.ai login found. Run /login in Claude Code with your claude.ai account")
    _token = (token, time.time())
    return token


def parent_and_args(pid):
    """(parent pid, command line) of a process, or (None, "") if it is gone."""
    if not MACOS:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            args = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            return int(stat.rsplit(")", 1)[1].split()[1]), args.strip()
        except (OSError, IndexError, ValueError):
            return None, ""
    out = subprocess.run(["/bin/ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True).stdout
    parts = out.strip().split(None, 1)
    return (int(parts[0]), parts[1] if len(parts) > 1 else "") if parts else (None, "")


def session_id_in(args):
    """The cse_... id in a Remote Control worker's command line: --session-id cse_... or a --sdk-url ending in
    /sessions/cse_..."""
    found = re.search(r"--session-id[ =](cse_\w+)|/sessions/(cse_\w+)", args)
    return (found.group(1) or found.group(2)) if found else None


def claude_process():
    """The Claude Code process running this hook, as (pid, its argv, its session registry entry), or None. It is the
    nearest ancestor with an entry in <config dir>/sessions/, so a Claude Code started inside another one's session
    never acts as the outer one."""
    pid = os.getppid()
    while pid and pid > 1:
        parent, args = parent_and_args(pid)
        entry = config_dir() / "sessions" / f"{pid}.json"
        if entry.exists():
            try:
                return pid, args, json.loads(entry.read_text())
            except ValueError:
                pass
        pid = parent
    return None


def remote_session_id():
    """The claude.ai session (cse_...) of the Remote Control worker this hook runs under, or None in a terminal
    session."""
    process = claude_process()
    return session_id_in(process[1]) if process else None


def session_link(cse):
    return "https://claude.ai/code/session_" + cse.removeprefix("cse_")


def api(method, path, body=None, token=None, headers=None, timeout=30):
    """Calls the claude.ai API the way Claude Code does and returns the decoded JSON reply."""
    request = urllib.request.Request(
        path if path.startswith("https:") else API + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token or oauth_token()}", "Content-Type": "application/json",
                 "anthropic-version": "2023-06-01", "anthropic-beta": "environments-2025-11-01",
                 "anthropic-client-platform": "cli", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        raw = reply.read()
    return json.loads(raw) if raw else {}


def http_problem(error):
    """A one-line description of a failed API call."""
    if isinstance(error, urllib.error.HTTPError):
        detail = error.read().decode(errors="replace")[:300]
        return f"HTTP {error.code} from claude.ai{': ' + detail if detail else ''}"
    return f"could not reach claude.ai ({getattr(error, 'reason', error)})"


def install_hint(*tools):
    """The command that installs <tools> with this machine's package manager, or None if none is known."""
    managers = [("brew", "brew install"), ("apt-get", "sudo apt-get install -y"), ("dnf", "sudo dnf install -y"),
                ("pacman", "sudo pacman -S --noconfirm"), ("zypper", "sudo zypper install -y"),
                ("apk", "sudo apk add")]
    for binary, command in managers:
        if shutil.which(binary) or (binary == "brew" and MACOS and Path("/opt/homebrew/bin/brew").exists()):
            return f"{command} {' '.join(tools)}"
    return None


def reply(context, message=None):
    """UserPromptSubmit output: <context> goes to Claude; <message> is shown to the user."""
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}
    if message:
        out["systemMessage"] = message
    print(json.dumps(out))


def post_message(cse, markdown):
    """Posts <markdown> to Remote Control session <cse> as an assistant message, the way Claude Code uploads session
    history; returns whether it worked. The worker ignores inbound events that aren't user messages, so Claude never
    sees it."""
    message = {"type": "assistant", "uuid": str(uuid.uuid4()), "session_id": cse, "parent_tool_use_id": None,
               # A "<synthetic>" model makes the app draw the message as an error.
               "message": {"id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
                           "model": "", "content": [{"type": "text", "text": markdown}],
                           "stop_reason": "end_turn", "stop_sequence": None,
                           "usage": {"input_tokens": 0, "output_tokens": 0}}}
    try:
        api("POST", f"/code/sessions/{cse}/events",
            {"events": [{"payload": {**message, "historical": True}, "historical": True}]})
        return True
    except (Problem, urllib.error.URLError, OSError):
        return False


def answer(cse, text, markdown=None):
    """Answers the prompt without a model turn; returns False if it had to fall back to Claude repeating the text.

    In Remote Control session <cse> the reply is posted to the chat (post_message). The app hides a blocked prompt's
    reason, which is only what a terminal shows. <markdown> replaces <text> where markdown renders."""
    if cse and not post_message(cse, markdown or text):
        reply("A hook handled this command. Show the user the text below exactly as given and nothing else."
              f"\n\n{markdown or text}", text)
        return False
    print(json.dumps({"decision": "block", "reason": text}))
    return True
