import hashlib, json, os
import pytest
import claude_env


@pytest.fixture(autouse=True)
def fresh_token_cache(monkeypatch):
    monkeypatch.setattr(claude_env, "_token", (None, 0.0))


def test_plugin_copies_match_shared_source():
    source = (claude_env.Path(claude_env.__file__)).read_bytes()
    from conftest import PLUGINS
    for plugin in PLUGINS.iterdir():
        assert (plugin / "scripts" / "claude_env.py").read_bytes() == source, plugin.name


def test_keychain_service_default(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert claude_env.keychain_service() == "Claude Code-credentials"


def test_keychain_service_with_config_dir(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/work-claude")
    suffix = hashlib.sha256(b"/tmp/work-claude").hexdigest()[:8]
    assert claude_env.keychain_service() == f"Claude Code-credentials-{suffix}"


def test_token_from_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}}))
    assert claude_env.oauth_token() == "tok-123"


def test_missing_login_says_how_to_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(claude_env, "MACOS", False)
    try:
        claude_env.oauth_token()
    except claude_env.Problem as problem:
        assert "/login" in str(problem)
    else:
        raise AssertionError("expected Problem")


def test_global_config_follows_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {"organizationUuid": "org-1"}}))
    assert claude_env.global_config()["oauthAccount"]["organizationUuid"] == "org-1"


def test_session_id_in_worker_command_lines():
    assert claude_env.session_id_in("claude --print --session-id cse_01AbC --x") == "cse_01AbC"
    assert claude_env.session_id_in("claude --sdk-url https://api.anthropic.com/v1/code/sessions/cse_9Z") == "cse_9Z"
    assert claude_env.session_id_in("claude --resume 1234") is None


def test_session_link():
    assert claude_env.session_link("cse_01ABC") == "https://claude.ai/code/session_01ABC"


def test_answer_posts_a_message_the_app_shows_as_a_reply(monkeypatch, capsys):
    posted = []
    monkeypatch.setattr(claude_env, "api", lambda method, path, body=None, **_: posted.append(body) or {})
    assert claude_env.answer("cse_X", "plain", "**markdown**")
    event = posted[0]["events"][0]
    assert event["historical"] and event["payload"]["historical"]
    message = event["payload"]["message"]
    # The worker ignores inbound assistant events, so Claude never sees it; "<synthetic>" would draw it as an error.
    assert event["payload"]["type"] == "assistant" and message["model"] != "<synthetic>"
    assert message["content"] == [{"type": "text", "text": "**markdown**"}]
    assert json.loads(capsys.readouterr().out) == {"decision": "block", "reason": "plain"}


def test_hook_belongs_to_the_nearest_claude_code(tmp_path, monkeypatch):
    # A Claude Code started inside a Remote Control session (like these tests' own e2e runs) is not that session.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    for pid in (200, 300):
        (tmp_path / "sessions" / f"{pid}.json").write_text(json.dumps({"pid": pid}))
    tree = {100: (200, "/bin/sh -c hook"), 200: (300, "claude -p hi"),
            300: (1, "claude --print --session-id cse_OUTER")}
    monkeypatch.setattr(claude_env.os, "getppid", lambda: 100)
    monkeypatch.setattr(claude_env, "parent_and_args", tree.get)
    assert claude_env.claude_process()[0] == 200
    assert claude_env.remote_session_id() is None
    (tmp_path / "sessions" / "200.json").unlink()
    assert claude_env.remote_session_id() == "cse_OUTER"
