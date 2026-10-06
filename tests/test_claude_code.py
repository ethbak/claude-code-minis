"""End to end through the real, newest Claude Code: it loads each plugin from its folder, hands the prompt to the
plugin's hook the way it does for users, and records the hook's output in the session transcript.

No login is used. Claude Code runs UserPromptSubmit hooks before it calls the model; the model call then fails on a
deliberately invalid API key, so nothing is billed, and the test stops Claude Code once the hook's output is in.
These tests catch Claude Code changes that unit tests can't: a renamed hook field, a prompt that no longer reaches
the hook (for example "!" or a skill name), or a plugin that no longer loads."""
import json, os, shutil, subprocess, time
import pytest
from conftest import PLUGINS

pytestmark = pytest.mark.skipif(not shutil.which("claude"), reason="needs Claude Code")
BLOCKED = "UserPromptSubmit operation blocked by hook:\n"


def close_windows_in(directory):
    """Closes only the remote-terminal windows a test opened (they start in its work folder)."""
    out = subprocess.run(["tmux", "-L", "remote-terminal", "list-windows", "-a", "-F",
                          "#{session_name}:#{window_index}\t#{pane_start_path}"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        window, _, path = line.partition("\t")
        if os.path.realpath(path) == os.path.realpath(directory):
            subprocess.run(["tmux", "-L", "remote-terminal", "kill-window", "-t", window], capture_output=True)


def run_claude(tmp_path, plugin, prompt, wait=60):
    """Runs `claude -p <prompt>` with only <plugin> loaded; returns the hook's output from the transcript: the context
    it gave Claude, or the reason it blocked the prompt, prefixed with "blocked: "."""
    run = tmp_path / f"run-{time.monotonic_ns()}"  # a fresh config per run, so one run never reads another's transcript
    config, work = run / "config", tmp_path / "work"
    config.mkdir(parents=True)
    work.mkdir(exist_ok=True)
    # Nothing from an enclosing Claude Code session may leak in: its login, its Remote Control session, its config.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env.update(CLAUDE_CONFIG_DIR=str(config), ANTHROPIC_API_KEY="sk-ant-invalid-for-hook-tests")
    proc = subprocess.Popen(["claude", "-p", prompt, "--plugin-dir", str(PLUGINS / plugin)], cwd=work, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    records, deadline = [], time.time() + wait
    try:
        while time.time() < deadline and not records:
            time.sleep(0.5)
            for transcript in config.glob("projects/*/*.jsonl"):
                for line in transcript.read_text(errors="replace").splitlines():
                    record = json.loads(line)
                    content = str(record.get("content"))
                    if record.get("type") == "system" and content.startswith(BLOCKED):
                        records.append({"type": "blocked", "content": "blocked: " + content[len(BLOCKED):]})
                    attachment = record.get("attachment") or {}
                    if attachment.get("hookEvent") == "UserPromptSubmit" and attachment.get("type") in (
                            "hook_additional_context", "hook_non_blocking_error", "hook_blocking_error"):
                        records.append(attachment)
    finally:
        proc.kill()
        proc.wait()
    assert records, f"no hook output from {plugin} within {wait} s"
    errors = [r.get("stderr") for r in records if r["type"] not in ("hook_additional_context", "blocked")]
    assert not errors, errors
    return "\n".join(str(r.get("content")) for r in records)


def test_bang_reaches_the_terminal_hook(tmp_path):
    marker = tmp_path / "marker"
    try:
        context = run_claude(tmp_path, "remote-terminal", f"!echo e2e-ok > {marker}")
        assert marker.read_text().strip() == "e2e-ok"
        assert "remote-terminal hook already ran it" in context
    finally:
        close_windows_in(tmp_path / "work")


def test_slash_bang_reaches_the_terminal_hook(tmp_path):
    try:
        assert "slash-bang-ok" in run_claude(tmp_path, "remote-terminal", "/!echo slash-bang-ok")
    finally:
        close_windows_in(tmp_path / "work")


def test_mode_answers_without_a_model_turn(tmp_path):
    # Usage and unknown-mode paths only: they never look up or change a session's mode.
    assert run_claude(tmp_path, "mode-picker", "/mode").startswith("blocked: Usage: /mode default | accept | plan")
    assert run_claude(tmp_path, "mode-picker", "/mode banana").startswith('blocked: Unknown mode "banana"')


def test_rresume_answers_without_a_model_turn(tmp_path):
    # No login in the throwaway config, so the hook answers that it needs one: proof the prompt reached it.
    reply = run_claude(tmp_path, "remote-resume", "/rresume")
    assert reply.startswith("blocked: /rresume:") and "/login" in reply
