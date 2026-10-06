"""Live Remote Control test: the three plugins in a real Remote Control session, driven the way the claude.ai app
drives one. It uses this machine's claude.ai login and runs only with MINIS_LIVE=1 (tools/live-nightly.sh runs it
every night on the maintainer's Mac).

It starts its own `claude remote-control` server in a dedicated folder, with the plugins loaded from this checkout
(CLAUDE_CODE_PLUGIN_DIRS), opens one chat on Haiku through the claude.ai API, sends it messages, and reads the replies
from the chat's event log. /mode and /rresume answer without a model turn; the ! command costs one short Haiku turn.
The chat is archived and the server stopped at the end.

The messages use forms a maintainer's older loose hooks don't catch (/mode-picker:mode, plain !), so only the
plugins answer."""
import json, os, signal, subprocess, sys, time, uuid
from pathlib import Path
import pytest
from conftest import PLUGINS

pytestmark = pytest.mark.skipif(os.environ.get("MINIS_LIVE") != "1", reason="live test: set MINIS_LIVE=1")
import claude_env  # noqa: E402  (shared copy, on sys.path via conftest)

MODEL = "claude-haiku-4-5-20251001"
WORK = Path(os.environ.get("MINIS_LIVE_DIR") or Path.home() / ".cache" / "claude-code-minis" / "live")


def api(method, path, body=None, **kw):
    return claude_env.api(method, path, body, **kw)


def wait_for(what, condition, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        found = condition()
        if found:
            return found
        time.sleep(1)
    raise AssertionError(f"timed out after {seconds} s waiting for {what}")


def trusted(directory):
    projects = claude_env.global_config().get("projects", {})
    return projects.get(str(directory), {}).get("hasTrustDialogAccepted") is True


@pytest.fixture
def chat(tmp_path):
    """(cse, send) for a new Remote Control chat on a server of our own; send(text) returns the reply text."""
    assert trusted(WORK), (f"{WORK} is not trusted: run `claude` there once and accept the trust prompt, or set "
                           f'.projects["{WORK}"].hasTrustDialogAccepted = true in ~/.claude.json')
    name = f"minis-live-{uuid.uuid4().hex[:6]}"
    env = {**os.environ, "CLAUDE_CODE_PLUGIN_DIRS": ":".join(str(PLUGINS / p) for p in
                                                               ("remote-resume", "remote-terminal", "mode-picker"))}
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    log = open(tmp_path / "remote-control.log", "w")
    server = subprocess.Popen(["claude", "remote-control", "--name", name, "--no-create-session-in-dir",
                               # A bypassing session holds messages that don't come signed from the app, as these don't.
                               "--permission-mode", "default"], cwd=WORK,
                              env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                              start_new_session=True)
    cse = None
    try:
        def environment():
            for e in api("GET", "/environments?limit=100").get("data", []):
                config = e.get("config") or {}
                if e.get("state") == "active" and config.get("type") == "bridge" and \
                        os.path.realpath(config.get("directory", "")) == os.path.realpath(WORK) and \
                        server.poll() is None:
                    return e["id"]
        env_id = wait_for("the Remote Control server to register", environment, 60)
        org = claude_env.global_config()["oauthAccount"]["organizationUuid"]
        cse = api("POST", "/code/sessions", {"title": name, "environment_id": env_id, "events": [],
                                             "config": {"sources": [], "outcomes": [], "cwd": str(WORK),
                                                        "model": MODEL}},
                  headers={"x-organization-uuid": org})["session"]["id"]

        def connected():
            s = api("GET", f"/code/sessions/{cse}")
            return (s.get("response_shape") or s.get("session") or {}).get("connection_status") == "connected"
        wait_for("the server to start a worker for the chat", connected, 60)

        def send(text, seconds=120, claude_replies=False):
            """Sends <text> as the web app does; returns the hook's posted reply, or with claude_replies, waits for
            Claude's reply too and returns (hook's reply, Claude's reply)."""
            sent = str(uuid.uuid4())
            # As the web app sends it: a message without client_platform reaches Claude as one from another session.
            api("POST", f"/code/sessions/{cse}/events", {"events": [{"payload": {
                "type": "user", "uuid": sent, "client_platform": "web",
                "message": {"role": "user", "content": text}}}]})

            def replies():
                events = [e["payload"] for e in api("GET", f"/code/sessions/{cse}/events?limit=50").get("data", [])]
                after = events[:next((i for i, p in enumerate(events) if p.get("uuid") == sent), len(events))]
                found = {"hook": [], "claude": []}
                for p in reversed(after):
                    if p.get("type") == "assistant":
                        texts = [b.get("text", "") for b in p["message"].get("content") or [] if b.get("type") == "text"]
                        found["claude" if p["message"].get("model") else "hook"] += [t for t in texts if t]
                hook, claude = "\n".join(found["hook"]), "\n".join(found["claude"])
                if claude_replies:
                    # Wait for the turn to end too: a message sent during a turn joins it without UserPromptSubmit
                    # hooks, so the next command would go to Claude as plain text.
                    ended = any(p.get("type") == "result" for p in after)
                    return (hook, claude) if hook and claude and ended else None
                return hook or claude  # a limit notice comes from Claude Code, not the hook
            return wait_for(f"a reply to {text!r}", replies, seconds)
        yield cse, send
    finally:
        if cse:
            try:
                api("POST", f"/code/sessions/{cse}/archive", {})
            except Exception as e:  # the test's own result matters more than cleanup
                print("archive failed:", e, file=sys.stderr)
        os.killpg(server.pid, signal.SIGINT)
        try:
            server.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, signal.SIGKILL)
        log.close()
        print((tmp_path / "remote-control.log").read_text()[-3000:], file=sys.stderr)


def test_plugins_in_a_live_remote_control_chat(chat):
    cse, send = chat
    # remote-resume: the hook posts the list itself; no model turn.
    assert "| # | Session | Activity |" in send("/rresume")
    # remote-terminal: the hook posts the output as its own message first, then Claude (Haiku) replies to it.
    marker = f"live-{uuid.uuid4().hex[:8]}"
    output, claude = send(f"!echo {marker}", claude_replies=True)
    assert " · exit 0 · " in output.split("\n")[0] and f"```console\n$ echo {marker}\n{marker}\n$\n```" in output
    if "hit your" in claude and "limit" in claude:
        pytest.skip(f"Claude's reply to ! not checked: the account's usage limit is reached ({claude})")
    assert "```" not in claude  # it replies to the output instead of showing it again
    # mode-picker: the session confirms the switch, then the hook posts the result.
    assert send("/mode-picker:mode plan") == "Switched to **Plan** from Default.\n\n> Reads and plans, no edits."
