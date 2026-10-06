"""/mode hook tests. The hook is run in-process with remote_session_id and the claude.ai API stubbed: run as a
subprocess, it would find the Remote Control session of whatever Claude Code ran the tests, switch that session's
real mode and post replies into it."""
import io, json, sys, urllib.error
import pytest
from conftest import PLUGINS
import claude_env

sys.path.insert(0, str(PLUGINS / "mode-picker" / "scripts"))
import mode  # noqa: E402


@pytest.fixture
def hook(monkeypatch, capsys):
    sent, posted = [], []

    def post(method, path, body=None, **_):
        if run.offline:
            raise urllib.error.URLError("offline")
        posted.append((path, body))
        return {}

    def run(payload, session=None, response=None):
        """Returns the text the user sees: the reply posted to the chat, or the blocked prompt's reason."""
        posted.clear()
        monkeypatch.setattr(claude_env, "api", post)
        monkeypatch.setattr(mode, "remote_session_id", lambda: session)
        monkeypatch.setattr(mode, "set_mode", lambda cse, m: sent.append((cse, m)) or response)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        try:
            mode.main()
        except SystemExit:
            pass
        out = capsys.readouterr().out
        if not out.strip():
            return None
        reply = json.loads(out)
        if "decision" not in reply:
            return reply
        assert reply["decision"] == "block"
        if session:
            [(path, body)] = posted
            assert path == f"/code/sessions/{session}/events"
            [text] = body["events"][0]["payload"]["message"]["content"]
            run.markdown = text["text"]
        return reply["reason"]
    run.sent, run.offline = sent, False
    return run


def test_ignores_other_prompts(hook):
    assert hook({"prompt": "please change the mode"}) is None
    assert hook({"prompt": "/modes"}) is None


def test_usage_shows_current_mode(hook):
    assert "Current mode: plan." in hook({"prompt": "/mode", "permission_mode": "plan"})


def test_unknown_mode(hook):
    assert hook({"prompt": "/mode-picker:mode banana"}).startswith('Unknown mode "banana".')


def test_terminal_session_points_to_shift_tab(hook):
    assert "Shift+Tab" in hook({"prompt": "/mode plan"}, session=None)
    assert hook.sent == []


def test_switch_confirmed(hook):
    reply = hook({"prompt": "/mode accept", "permission_mode": "default"}, session="cse_X",
                 response={"subtype": "success", "response": {"mode": "acceptEdits"}})
    assert reply == "Switched to acceptEdits (was default)." and hook.sent == [("cse_X", "acceptEdits")]
    assert hook.markdown == "Switched to **Accept edits** from Default.\n\n> Edits files without asking."


def test_menu_marks_the_current_mode(hook):
    hook({"prompt": "/mode", "permission_mode": "plan"}, session="cse_X")
    assert hook.markdown.startswith("Current mode: **Plan**\n\n| Command | Mode | What it does |")
    assert "| `/mode plan` | **Plan** (current) | Reads and plans, no edits |" in hook.markdown


def test_switch_refused(hook):
    reply = hook({"prompt": "/mode bypass"}, session="cse_X",
                 response={"subtype": "error", "error": "bypassPermissions is disabled by policy"})
    assert reply == "Mode not changed: bypassPermissions is disabled by policy."


def test_switch_unconfirmed(hook):
    assert "did not confirm" in hook({"prompt": "/mode plan"}, session="cse_X", response=None)


def test_reply_is_posted_as_a_plain_assistant_message(hook):
    hook({"prompt": "/mode"}, session="cse_X")


def test_falls_back_to_claude_when_the_post_fails(hook):
    hook.offline = True
    reply = hook({"prompt": "/mode"}, session="cse_X")
    assert reply["systemMessage"].startswith("Usage: /mode")
    assert "Show the user the text below" in reply["hookSpecificOutput"]["additionalContext"]
