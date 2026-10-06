import json, sys, time
from conftest import PLUGINS, run_hook

sys.path.insert(0, str(PLUGINS / "remote-resume" / "scripts"))
import rresume  # noqa: E402

RRESUME = PLUGINS / "remote-resume" / "scripts" / "rresume.py"


def write_transcript(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def user(text, **extra):
    return {"type": "user", "uuid": f"u-{text[:8]}", "cwd": "/home/me/proj", "entrypoint": "cli",
            "message": {"role": "user", "content": text}, **extra}


def test_command_matches_bare_and_prefixed_forms():
    assert rresume.is_command("/rresume")
    assert rresume.is_command("/rresume 3")
    assert rresume.is_command("/remote-resume:rresume next")
    assert not rresume.is_command("/rresumes")
    assert not rresume.is_command("rresume")


def test_prompt_text_skips_tool_results_and_meta():
    assert rresume.prompt_text(user("hello")) == "hello"
    assert rresume.prompt_text(user("x", isMeta=True)) is None
    tool = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "out"}]}}
    assert rresume.prompt_text(tool) is None
    command = user("<command-name>/model</command-name><command-args>opus</command-args>")
    assert rresume.prompt_text(command) == "/model opus"


def test_summarize_titles_and_counts(tmp_path):
    path = tmp_path / "s.jsonl"
    write_transcript(path, [user("fix the build"), {"type": "ai-title", "aiTitle": "Build fix"},
                            {"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 1000}}},
                            user("/rresume"), user("now add tests")])
    s = rresume.summarize(path)
    assert s["title"] == "Build fix" and s["turns"] == 2 and s["last"] == "now add tests"
    assert s["tokens"] == 1005 and s["terminal"] and s["project"] == "proj"


def test_summary_cache_reuses_unchanged_transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(rresume, "STATE", tmp_path / "state")
    path = tmp_path / "s.jsonl"
    write_transcript(path, [user("one")])
    calls = []
    real = rresume.summarize
    monkeypatch.setattr(rresume, "summarize", lambda p: calls.append(p) or real(p))
    cache = rresume.Summaries()
    cache.get(path)
    cache.save()
    rresume.Summaries().get(path)
    assert len(calls) == 1
    time.sleep(0.01)
    write_transcript(path, [user("one"), user("two")])
    assert rresume.Summaries().get(path)["turns"] == 2 and len(calls) == 2


def test_throwaway_chat(tmp_path):
    path = tmp_path / "t.jsonl"
    write_transcript(path, [user("/rresume"), user("/rresume 2"), user("thanks")])
    assert rresume.throwaway(path)
    write_transcript(path, [user("/rresume"), user("real work"), user("more work")])
    assert not rresume.throwaway(path)


def test_auto_archive_option(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_AUTO_ARCHIVE", raising=False)
    assert rresume.auto_archive()
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_AUTO_ARCHIVE", "false")
    assert not rresume.auto_archive()


def test_render_fits_a_phone():
    items = [{"title": "A very long session title that will not fit", "last": "and a long last prompt as well",
              "turns": 3, "tokens": 52000, "cwd": "/x/other", "project": "other", "mtime": time.time() - 7200}]
    text = rresume.render(items, 1, "Sessions 1–1", "/x/here")
    assert all(len(line) <= rresume.WIDTH for line in text.splitlines())
    assert "2h · 3 turns · 52k" in text


def test_ignores_other_prompts():
    assert run_hook(RRESUME, {"prompt": "/rresumex"}) is None
    assert run_hook(RRESUME, {"prompt": "please /rresume"}) is None


def test_empty_chat_is_listed_once_it_has_a_conversation(tmp_path, monkeypatch):
    monkeypatch.setattr(rresume, "STATE", tmp_path / "state")
    monkeypatch.setattr(rresume, "PROJECTS", tmp_path / "projects")
    (tmp_path / "projects" / "p").mkdir(parents=True)
    write_transcript(tmp_path / "projects" / "p" / "local-1.jsonl", [user("fix the build", entrypoint="sdk-cli")])
    chat = {"id": "cse_A", "environment_kind": "bridge", "environment_id": "env_1", "title": "Build",
            "last_event_at": "2026-10-06T10:00:00Z", "updated_at": "2026-10-06T10:00:00Z"}
    events, lookups = [], []

    def api(method, path, body=None, **_):
        if path.startswith("/environments"):
            return {"data": [{"id": "env_1", "state": "active"}]}
        if path.startswith("/code/sessions?"):
            return {"data": [chat]}
        lookups.append(path)
        return {"data": events}
    monkeypatch.setattr(rresume, "api", api)
    listed = lambda: [s["cse"] for s in rresume.service_sessions(None, rresume.Summaries())]

    assert listed() == [] and listed() == [] and len(lookups) == 1  # unchanged empty chat: looked up once
    events.append({"source": "worker", "payload": {"session_id": "local-1"}})
    chat["last_event_at"] = "2026-10-06T10:05:00Z"
    assert listed() == ["cse_A"] and listed() == ["cse_A"] and len(lookups) == 2


def run_in_process(monkeypatch, capsys, prompt):
    """The hook's reply text, run in-process outside Remote Control, so it can't post into a real chat."""
    import io
    monkeypatch.setattr(rresume, "remote_session_id", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt, "session_id": "s", "cwd": "/home/me/app"})))
    rresume.main()
    return json.loads(capsys.readouterr().out)["reason"]


def test_demo_list_and_easter_eggs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rresume, "STATE", tmp_path)
    listing = run_in_process(monkeypatch, capsys, "/rresume --demo")
    assert listing.startswith("Sessions 1–10\n") and "10 Hello, world" in listing and "/rresume <n>     resume" in listing
    assert run_in_process(monkeypatch, capsys, "/rresume 7").startswith('Resumed "Teach the toaster')
    assert "is a demo session" in run_in_process(monkeypatch, capsys, "/rresume 1")
    assert run_in_process(monkeypatch, capsys, "/rresume next") == "No more sessions. You've seen them all."
