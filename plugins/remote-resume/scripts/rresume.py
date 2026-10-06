#!/usr/bin/env python3
"""UserPromptSubmit hook behind /rresume (plugin remote-resume).

Remote Control sessions run headless, where /resume is blocked, and the app cannot bring back an archived session.

  /rresume          list the 10 most recent resumable sessions, numbered
  /rresume <text>   the 10 most recent whose title or prompts contain <text>
  /rresume next     next 10 of the last list (prev goes back); numbering continues
                    each chat keeps its own list position, keyed by session id
  /rresume <n>      resume session <n> from the last list this chat showed

Resuming brings the old session itself back, under its own id, the way /resume would: unarchive it if needed, then
ask the server to hand it back to its Remote Control environment (POST /v1/environments/<env>/bridge/reconnect, the
call the Remote Control service makes after a restart). The service starts a worker that resumes the session from
its server log, and manages it from then on like any other session. The reply links to it.

The server hands a session back only to the environment it came from, which for a terminal session ended with the
terminal, so a terminal session is moved into the Remote Control service whose directory holds it:
  1. create a session in the service's environment (the service starts a worker for it, as for any new chat)
  2. take over as that session's worker (POST /v1/code/sessions/<cse>/bridge, which retires the service's worker)
  3. upload the terminal transcript as the worker's stored transcript (/worker/internal-events, which a resuming
     worker loads; titles and file checkpoints included) and as chat messages (/worker/events, which the app shows),
     and copy its file checkpoint backups and subagent transcripts to the worker's session id
  4. hand the session back to the service (bridge/reconnect); its new worker loads the conversation.
The session is created with the terminal's last model, which the service passes to every worker it starts for it.
The service always starts workers in its own directory, and effort and permission mode reset with each worker, so
these are kept per session and re-applied by this script's SessionStart mode whenever a worker for the session
starts: a set_cwd control request moves the worker into the terminal's directory (the headless /cd), then
apply_flag_settings and set_permission_mode restore effort and mode. A worker the service restarts because a message
arrived runs that first turn before it can move; it is told so.
From then on it is an ordinary service session. The import is remembered, so the terminal session drops out of the
list until the terminal adds to it.

Afterwards, unless the plugin's auto_archive option is off, the chat the command was typed in is archived when it
holds nothing but /rresume commands and at most one other message, so throwaway chats don't pile up; a chat with
real content is left as it was.
"""
import calendar, json, os, re, shutil, subprocess, sys, time, urllib.error, uuid
from pathlib import Path
from claude_env import MACOS, Problem, answer, api as claude_api, config_dir, global_config, http_problem, remote_session_id, \
    session_link

PROJECTS = config_dir() / "projects"
STATE = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or Path.home() / ".cache/remote-resume")
COMMAND = re.compile(r"/(?:remote-resume:)?rresume(?:\s+(.*))?", re.S)
PAGE = 10
WIDTH = 35  # characters per line that fit a phone screen in a code block
API = "https://api.anthropic.com/v1"
# A service worker started with --resume=<session URL> keeps its local transcript under uuid5(this, URL).
WORKER_ID_NAMESPACE = uuid.UUID("3ab19d7e-9f35-45c2-926e-75e271cc60b3")
BATCH = 100
# Transcript records that belong to the terminal process, not the conversation: its own Remote Control link, its
# input queue and its running cost.
TERMINAL_ONLY = {"bridge-session", "queue-operation", "atis-latch", "cost-state"}
SUMMARY_VERSION = 1


def is_command(text):
    return bool(COMMAND.fullmatch(text.strip()))


def auto_archive():
    return os.environ.get("CLAUDE_PLUGIN_OPTION_AUTO_ARCHIVE", "true").strip().lower() not in ("false", "0", "no", "off")


def records(path):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def prompt_text(r):
    """The text of a prompt the user typed, or None for tool results, meta and injected messages."""
    if r.get("type") != "user" or r.get("isMeta") or r.get("isSidechain") or r.get("isCompactSummary"):
        return None
    if (r.get("origin") or {}).get("kind", "human") != "human":
        return None
    content = (r.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content.strip() or content.startswith("[Request interrupted"):
        return None
    name = re.search(r"<command-name>(.*?)</command-name>", content)
    if name:
        args = re.search(r"<command-args>(.*?)</command-args>", content, re.S)
        return f"{name.group(1)} {args.group(1).strip() if args else ''}".strip()
    return content.strip()


def summarize(path):
    title, prompts, tokens, cwd, entrypoint, last = None, [], 0, None, None, None
    for r in records(path):
        t, cwd, entrypoint = r.get("type"), cwd or r.get("cwd"), entrypoint or r.get("entrypoint")
        last = r.get("uuid") or last
        if t == "custom-title":
            title = ("custom", r.get("customTitle"))
        elif t == "ai-title" and not (title and title[0] == "custom"):
            title = ("ai", r.get("aiTitle"))
        elif t == "assistant" and not r.get("isSidechain"):
            u = (r.get("message") or {}).get("usage") or {}
            used = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            tokens = used or tokens
        else:
            text = prompt_text(r)
            if text and not is_command(text):
                prompts.append(text)
    if not prompts or not cwd:
        return None
    return {"title": title[1] if title else prompts[0], "last": prompts[-1], "turns": len(prompts),
            "text": "\n".join(prompts).lower(), "tokens": tokens, "cwd": cwd, "terminal": entrypoint == "cli",
            "last_uuid": last, "project": "~" if cwd == str(Path.home()) else Path(cwd).name}


class Summaries:
    """summarize() results cached by transcript path, size and mtime, so a listing reads only changed transcripts."""

    def __init__(self):
        self.file = STATE / "summaries.json"
        try:
            saved = json.loads(self.file.read_text())
            self.cache = saved["entries"] if saved.get("version") == SUMMARY_VERSION else {}
        except (OSError, ValueError, KeyError):
            self.cache = {}
        self.changed = False

    def get(self, path):
        st = path.stat()
        key, stamp = str(path), [st.st_size, st.st_mtime_ns]
        hit = self.cache.get(key)
        if hit and hit["stamp"] == stamp:
            return hit["summary"]
        summary = summarize(path)
        self.cache[key], self.changed = {"stamp": stamp, "summary": summary}, True
        return summary

    def save(self):
        if self.changed:
            STATE.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": SUMMARY_VERSION, "entries": self.cache}))
            os.replace(tmp, self.file)


def sessions(here, current, summaries):
    """Resumable sessions, newest first, other than this chat."""
    return sorted([*service_sessions(here, summaries), *terminal_sessions(current, summaries)], key=lambda s: -s["mtime"])


def held():
    """Local ids of sessions a live claude process is running."""
    found = set()
    for f in (config_dir() / "sessions").glob("*.json"):
        try:
            s = json.loads(f.read_text())
            os.kill(s["pid"], 0)
        except (ValueError, KeyError, OSError):
            continue
        found.add(s["sessionId"])
    return found


def imports():
    f = STATE / "imports.json"
    return json.loads(f.read_text()) if f.exists() else {}


def terminal_sessions(current, summaries):
    """Terminal sessions with real prompts that are not open in a terminal and not already imported as they are."""
    running, imported = held(), imports()
    for path in PROJECTS.glob("*/*.jsonl"):
        if path.stem == current or path.stem in running:
            continue
        summary = summaries.get(path)
        if summary and summary["terminal"] and imported.get(path.stem, {}).get("last") != summary["last_uuid"]:
            yield {**summary, "local": path.stem, "mtime": path.stat().st_mtime}


def service_sessions(here, summaries):
    """Remote Control sessions of active environments whose local transcript has real prompts. A worker's events
    carry its local session id, which links a server session to its transcript; the pairing never changes, so it is
    cached. A resumed terminal session is listed by terminal_sessions instead."""
    active = {e["id"] for e in api("GET", "/environments?limit=100").get("data", []) if e.get("state") == "active"}
    cache_file = STATE / "transcripts.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    cursor = None
    while True:
        page = api("GET", "/code/sessions?limit=100" + (f"&cursor={cursor}" if cursor else ""))
        for s in page.get("data", []):
            if s.get("environment_kind") != "bridge" or s.get("environment_id") not in active or s["id"] == here:
                continue
            # A session with no conversation yet is cached as {"empty": <its last event time>}, and looked at again
            # once it has newer events.
            last_event, local = s.get("last_event_at") or s["updated_at"], cache.get(s["id"])
            if not local or (isinstance(local, dict) and local != {"empty": last_event}):
                events = api("GET", f"/code/sessions/{s['id']}/events?limit=50").get("data", [])
                cache[s["id"]] = next((e["payload"]["session_id"] for e in events
                                       if e.get("source") == "worker" and e["payload"].get("session_id")),
                                      {"empty": last_event})
                STATE.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(cache))
                local = cache[s["id"]]
            path = next(PROJECTS.glob(f"*/{local}.jsonl"), None) if isinstance(local, str) else None
            summary = summaries.get(path) if path else None
            if summary and not summary["terminal"]:
                seen = calendar.timegm(time.strptime(last_event[:19], "%Y-%m-%dT%H:%M:%S"))
                yield {**summary, "cse": s["id"], "title": s.get("title") or summary["title"], "mtime": seen,
                       "text": f"{(s.get('title') or '').lower()}\n{summary['text']}"}
        cursor = page.get("next_cursor")
        if not page.get("data") or not cursor:
            return


def ago(t):
    s = time.time() - t
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit}"
    return "now"


def clip(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def render(items, start, heading, cwd):
    lines = [clip(heading, WIDTH), ""]
    for i, s in enumerate(items, start):
        parts = meta(s, cwd)
        if s["cwd"] != cwd:
            room = WIDTH - 3 - len(" · ".join(parts[:-1])) - 3
            parts = parts[:-1] + ([clip(parts[-1], room)] if room >= 4 else [])  # a letter or two says nothing
        lines += [f"{i:>2} {clip(s['title'], WIDTH - 3)}",
                  f"   › {clip(s['last'], WIDTH - 5)}",
                  f"   {clip(' · '.join(parts), WIDTH - 3)}", ""]
    return "\n".join(lines).rstrip()


def save_listing(current, query, items, start):
    """Per chat: the query, every session fetched so far (numbers index into it) and where the shown page starts."""
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"list-{current}.json").write_text(json.dumps({"query": query, "start": start, "sessions": [
        {k: s[k] for k in ("cse", "local", "cwd", "title") if k in s} for s in items]}))


def last_listing(current):
    cached = STATE / f"list-{current}.json"
    return json.loads(cached.read_text()) if cached.exists() else None


USAGE = """\
/rresume <n>     resume
/rresume next    older
/rresume prev    newer
/rresume <text>  search"""
USAGE_MD = "Resume with `/rresume <n>`. Page with `/rresume next` and `/rresume prev`. Search with `/rresume <text>`."


def escape(text):
    """Text shown as-is in markdown: titles and prompts can hold * _ ` [ and the like."""
    return re.sub(r"([\\`*_\[\]<>#|~])", r"\\\1", text)


def meta(s, cwd):
    parts = [ago(s["mtime"]), f"{s['turns']} turn{'s' * (s['turns'] != 1)}"]
    parts += [f"{round(s['tokens'] / 1000)}k"] if s["tokens"] else []
    return parts + ([s["project"]] if s["cwd"] != cwd else [])


def render_markdown(items, start, heading, cwd):
    rows = [f"**{heading}**", "", "| # | Session | Activity |", "|---:|---|---|"]
    for i, s in enumerate(items, start):
        rows.append(f"| {i} | **{escape(clip(s['title'], 70))}** — {escape(clip(s['last'], 90))} | "
                    f"{escape(' · '.join(meta(s, cwd)))} |")
    return "\n".join(rows)


def show(cse, items, start=1, heading="", cwd=None, empty=None):
    """Replies with a page of sessions, or with <empty> when there are none."""
    if empty:
        answer(cse, f"{empty}\n\n{USAGE}", f"{empty}\n\n{USAGE_MD}")
        return
    text = f"{render(items, start, heading, cwd)}\n\n{USAGE}"
    answer(cse, text, f"{render_markdown(items, start, heading, cwd)}\n\n{USAGE_MD}")


def api(method, path, body=None, auth=None, headers=None):
    return claude_api(method, path, body, token=auth, headers=headers)


def session(cse):
    reply = api("GET", f"/code/sessions/{cse}")
    return reply.get("response_shape") or reply.get("session") or {}


def wait_connected(cse, seconds=20):
    for _ in range(seconds):
        s = session(cse)
        if s.get("connection_status") == "connected":
            return s
        time.sleep(1)
    return None


def revive(cse):
    """Brings a Remote Control service's session back under its own id. Returns (link, problem)."""
    # The server's connection status can read "connected" with no worker left, so look for the worker process.
    s, link = session(cse), session_link(cse)
    if s.get("status") == "archived":
        api("POST", f"/code/sessions/{cse}/unarchive", {})
    elif worker_running(cse):
        return link, None
    api("POST", f"/environments/{s['environment_id']}/bridge/reconnect", {"session_id": cse})
    if not wait_until(lambda: worker_running(cse), 20):
        return None, "no Remote Control service picked it up. Start one in that project with `claude remote-control`"
    return link, None


def service_environment(cwd):
    """The active Remote Control service environment whose directory most closely contains <cwd>, else the one with
    the shortest directory."""
    envs = [e for e in api("GET", "/environments?limit=100").get("data", [])
            if e.get("state") == "active" and (e.get("config") or {}).get("type") == "bridge"]
    within = [e for e in envs if (cwd + "/").startswith(e["config"]["directory"].rstrip("/") + "/")]
    pick = max(within, key=lambda e: len(e["config"]["directory"]), default=None) or \
        min(envs, key=lambda e: len(e["config"]["directory"]), default=None)
    return (pick["id"], pick["config"]["directory"]) if pick else (None, None)


def batches(events):
    batch, size = [], 0
    for e in events:
        n = len(json.dumps(e))
        if batch and (len(batch) == BATCH or size + n > 4_000_000):
            yield batch
            batch, size = [], 0
        batch.append(e)
        size += n
    if batch:
        yield batch


def processes():
    """(pid, command line) of every process this user can see."""
    if not MACOS:
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    yield d.name, (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                except OSError:
                    continue
        return
    out = subprocess.run(["/bin/ps", "-axwwo", "pid=,args="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        yield pid, args


def worker_pid(cse):
    return next((pid for pid, args in processes() if f"--session-id {cse} " in args + " "), None)


def worker_running(cse):
    return worker_pid(cse) is not None


def worker_cwd(cse):
    pid = worker_pid(cse)
    if not pid:
        return None
    if not MACOS:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    out = subprocess.run(["/usr/sbin/lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
    return next((line[1:] for line in out.splitlines() if line.startswith("n")), None)


def wait_until(condition, seconds):
    deadline = time.time() + seconds
    while not condition():
        if time.time() > deadline:
            return False
        time.sleep(0.5)
    return True


def import_terminal(target):
    """Moves a terminal session into a Remote Control service as a new session. Returns (link, problem)."""
    transcript = next(PROJECTS.glob(f"*/{target['local']}.jsonl"))
    env, directory = service_environment(target["cwd"])
    if not env:
        return None, "no Remote Control service is running. Start one with `claude remote-control`"
    org = global_config()["oauthAccount"]["organizationUuid"]
    model, carry = terminal_settings(transcript)
    if carry.get("cwd") == directory:
        del carry["cwd"]
    created = api("POST", "/code/sessions", {"title": clip(target["title"], 80), "environment_id": env, "events": [],
                                             "config": {"sources": [], "outcomes": [], "cwd": directory,
                                                        **({"model": model} if model else {})}},
                  headers={"x-organization-uuid": org})
    cse = created["session"]["id"]
    link, problem = move_in(target, transcript, env, directory, cse, carry)
    if not link:
        api("POST", f"/code/sessions/{cse}/archive", {})
        carried_file(cse).unlink(missing_ok=True)
    return link, problem


def terminal_settings(transcript):
    """The model the terminal session last ran with, and the directory, effort and permission mode it ended in."""
    model, carry = None, {}
    for r in records(transcript):
        if r.get("cwd") and not r.get("isSidechain"):
            carry["cwd"] = r["cwd"]
        if r.get("type") == "assistant" and not r.get("isSidechain"):
            used = (r.get("message") or {}).get("model")
            model = used if used and used != "<synthetic>" else model
            carry["effort"] = r.get("effort") or carry.get("effort")
        elif r.get("type") == "permission-mode" and r.get("permissionMode"):
            carry["mode"] = r["permissionMode"]
    return model, {k: v for k, v in carry.items() if v}


def carried_file(cse):
    return STATE / "carried" / f"{cse}.json"


def project_dir(cwd):
    """Where Claude Code keeps transcripts for sessions in <cwd>."""
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def move_in(target, transcript, env, directory, cse, carry):
    link = session_link(cse)
    # Take over only once the service's own worker is up; otherwise it would arrive later and take the session back.
    if not wait_connected(cse, 15):
        return None, f"the Remote Control service for {directory} did not pick up the new session"
    worker = api("POST", f"/code/sessions/{cse}/bridge", {})
    url = f"{worker['api_base_url']}/v1/code/sessions/{cse}"
    local = str(uuid.uuid5(WORKER_ID_NAMESPACE, url))
    stored, shown, last = [], [], None
    for r in records(transcript):
        last = r.get("uuid") or last
        if not r.get("type") or r["type"] in TERMINAL_ONLY:
            continue
        stored.append({"payload": {**r, "sessionId": local, **({"entrypoint": "sdk-cli"} if "entrypoint" in r else {})}})
        if r["type"] in ("user", "assistant") and not (r.get("isMeta") or r.get("isSidechain") or r.get("isCompactSummary")):
            shown.append({"payload": {"type": r["type"], "message": r["message"], "parent_tool_use_id": None,
                                      "session_id": local, "uuid": r["uuid"], "timestamp": r.get("timestamp")}})
    for path, events in (("/worker/internal-events", stored), ("/worker/events", shown)):
        for batch in batches(events):
            api("POST", url + path, {"worker_epoch": int(worker["worker_epoch"]), "events": batch}, auth=worker["worker_jwt"])
    # Checkpoint backups are keyed by session id. Subagent transcripts sit next to the worker's transcript, and set_cwd
    # carries that folder along when it moves the session.
    backups = config_dir() / "file-history" / target["local"]
    if backups.is_dir():
        shutil.copytree(backups, config_dir() / "file-history" / local, dirs_exist_ok=True)
    subagents = transcript.parent / target["local"] / "subagents"
    if subagents.is_dir():
        shutil.copytree(subagents, project_dir(directory) / local / "subagents", dirs_exist_ok=True)
    if carry:
        carried_file(cse).parent.mkdir(parents=True, exist_ok=True)
        carried_file(cse).write_text(json.dumps(carry))
    # The service ignores a reconnect while the worker it is replacing still runs. The session's connection status
    # stays "connected" after a takeover, so only the new worker's transcript proves the hand-back worked.
    if not wait_until(lambda: not worker_running(cse), 10):
        return None, "the service's first worker did not step down"
    api("POST", f"/environments/{env}/bridge/reconnect", {"session_id": cse})
    newest = next(e["payload"]["uuid"] for e in reversed(stored) if e["payload"].get("uuid"))
    if not wait_until(lambda: any(p.exists() and newest in p.read_text(errors="replace")
                                  for p in PROJECTS.glob(f"*/{local}.jsonl")), 20):
        return None, "the service did not load the conversation"
    moved = not carry.get("cwd") or wait_until(lambda: worker_cwd(cse) == os.path.realpath(carry["cwd"]), 30)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "imports.json").write_text(json.dumps({**imports(), target["local"]: {"cse": cse, "last": last}}))
    return link, None if moved else f"it runs in {directory}, not {carry['cwd']} (see {STATE / 'carry.log'})"


def throwaway(transcript):
    """True when the chat holds only /rresume commands and at most one other message."""
    prompts = [t for t in (prompt_text(r) for r in records(transcript)) if t]
    return sum(not is_command(t) for t in prompts) <= 1


def archive_later(cse, transcript=None):
    """Detached copy of this script: archives the chat once the hook has returned, or, given the transcript, once
    the turn in which Claude repeats the reply has ended."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "archive.log", "a") as log:
        subprocess.Popen([sys.executable, __file__, "--archive", cse] + ([str(transcript)] if transcript else []),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def archive(cse, transcript=None):
    if transcript:
        ended = lambda: Path(transcript).read_text(errors="replace").count('"subtype":"stop_hook_summary"')
        start, deadline = ended(), time.time() + 600
        while ended() == start and time.time() < deadline:
            time.sleep(1)
    time.sleep(2)
    try:
        api("POST", f"/code/sessions/{cse}/archive", {})
    except urllib.error.HTTPError as e:
        if e.code != 409:  # 409: already archived
            raise
    print(time.strftime("%F %T"), "archived", cse, flush=True)


def resume(data, n, here):
    current = data.get("session_id", "")
    last = last_listing(current)
    if last:
        found = last["sessions"]
    else:
        summaries = Summaries()
        found = listing(current, here, None, 0, summaries)
        summaries.save()
    if not 1 <= n <= len(found):
        answer(here, f"No session {n} in the last list ({len(found)} entries). Send /rresume to see the list.")
        return
    target, title = found[n - 1], clip(found[n - 1]["title"], 60)
    try:
        link, problem = import_terminal(target) if "local" in target else revive(target["cse"])
    except (urllib.error.URLError, OSError) as e:
        link, problem = None, http_problem(e)
    except KeyError as e:
        link, problem = None, f"claude.ai sent a reply without {e}"
    transcript = Path(data.get("transcript_path", ""))
    archiving = bool(link and here and auto_archive() and transcript.is_file() and throwaway(transcript))
    archived = "This chat had no other content and will be archived."
    if not link:
        message = f"Could not resume \"{title}\": {problem}."
        markdown = f"**Couldn't resume** {escape(title)}\n\n> {problem[:1].upper()}{problem[1:]}."
    else:
        message = f"Resumed \"{title}\": {link}" + (f"\nBut {problem}." if problem else "") + \
            (f"\n{archived}" if archiving else "")
        markdown = f"**Resumed** [{escape(title)}]({link})" + (f"\n\n> But {problem}." if problem else "") + \
            (f"\n\n> {archived}" if archiving else "")
    posted = answer(here, message, markdown)
    if archiving:
        archive_later(here, None if posted else transcript)


def control(cse, request, seconds=30):
    """Sends a control request to the session's worker; returns its response payload, or None if none came."""
    rid = str(uuid.uuid4())
    api("POST", f"/code/sessions/{cse}/events", {"events": [{"payload": {
        "type": "control_request", "request_id": rid, "uuid": str(uuid.uuid4()), "request": request}}]})
    deadline = time.time() + seconds
    while time.time() < deadline:
        for e in api("GET", f"/code/sessions/{cse}/events?limit=50").get("data", []):
            if e["payload"].get("type") == "control_response" and e["payload"]["response"].get("request_id") == rid:
                return e["payload"]["response"]
        time.sleep(1)
    return None


def session_start(data):
    """SessionStart in a Remote Control worker for a session moved in from a terminal: puts back its directory,
    effort and permission mode, which a new worker loses."""
    if os.environ.get("CLAUDE_CODE_ENVIRONMENT_KIND") != "bridge" or data.get("source") not in ("startup", "resume"):
        return
    cse = remote_session_id()
    if not cse or not carried_file(cse).exists():
        return
    carry = json.loads(carried_file(cse).read_text())
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "carry.log", "a") as log:
        subprocess.Popen([sys.executable, __file__, "--carry", cse], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    here = os.path.realpath(data.get("cwd") or os.getcwd())
    if carry.get("cwd") and carry.get("moved") and here != os.path.realpath(carry["cwd"]):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
              f"This session works in {carry['cwd']}, but its worker restarted in {here}. It is moved back as soon as "
              f"no turn is running; until a message says the working directory changed, work in {carry['cwd']} using "
              "absolute paths."}}))


def apply_carried(cse):
    """Detached from session_start: sends the session's carried settings to its worker, retrying while a turn runs."""
    carry = json.loads(carried_file(cse).read_text())
    requests = [*([{"subtype": "set_permission_mode", "mode": carry["mode"]}] if carry.get("mode") else []),
                *([{"subtype": "apply_flag_settings", "settings": {"effortLevel": carry["effort"]}}]
                  if carry.get("effort") else []),
                *([{"subtype": "set_cwd", "path": carry["cwd"]}] if carry.get("cwd") else [])]
    for request in requests:
        deadline = time.time() + 1800
        while True:
            response = control(cse, request)
            result = (response or {}).get("response") or {}
            if result.get("status") == "needs_trust":
                # The terminal session already worked in this directory, which is the trust a dialog would ask for.
                request = {**request, "trust_accepted": True, "trusted_directory": result["directory"]}
                continue
            if not (result.get("status") == "rejected" and result.get("reason") == "busy") or time.time() > deadline:
                break
            time.sleep(5)
        print(time.strftime("%F %T"), cse, request["subtype"], json.dumps(response)[:300], flush=True)
        if request["subtype"] == "set_cwd" and result.get("status") == "ok":
            carried_file(cse).write_text(json.dumps({**carry, "moved": True}))
            restore_superseded(str(uuid.uuid5(WORKER_ID_NAMESPACE, f"{API}/code/sessions/{cse}")))


def restore_superseded(local):
    """Moving a session into a folder that already holds its files sets the old ones aside as
    <id>.superseded-<ms>. Put them back, keeping the new copy of any file both have, so subagent transcripts and
    saved tool output stay at the paths the conversation refers to. A set-aside transcript goes once the new one,
    rebuilt from the server, holds every message in it."""
    for old in PROJECTS.glob(f"*/{local}.jsonl.superseded-*"):
        current = old.parent / f"{local}.jsonl"
        uuids = lambda path: {r["uuid"] for r in records(path) if r.get("uuid")}
        if current.exists() and uuids(old) <= uuids(current):
            old.unlink()
    for old in PROJECTS.glob(f"*/{local}.superseded-*"):
        if not old.is_dir():
            continue
        for f in sorted(old.rglob("*")):
            dest = old.parent / local / f.relative_to(old)
            if f.is_file() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                f.rename(dest)
        shutil.rmtree(old)


def listing(current, here, query, start, summaries):
    """Sessions newest first (only those whose title or prompts contain <query>, if given); returns the page at
    <start> and remembers everything up to it."""
    found = (s for s in sessions(here, current, summaries) if query is None or query.lower() in s["text"])
    items = [s for _, s in zip(range(start + PAGE), found)]
    save_listing(current, query, items, start)
    return items[start:]


# /rresume --demo: the real list layout filled with made-up sessions, for screenshots and demos. Resuming one of them
# only gets a joke back.
DEMO = [  # title, last prompt, age in seconds, turns, context tokens, project folder (None: the chat's own)
    ("Fix flaky auth tests", "now run the whole suite", 720, 14, 120_000, None),
    ("Release notes for v2.3", "make it shorter", 2 * 3600, 3, 41_000, "docs"),
    ("Migrate CI to arm64 runners", "ok, merge it", 5 * 3600, 22, 188_000, "infra"),
    ("Why is the Docker image 4 GB", "found it: node_modules", 9 * 3600, 9, 76_000, None),
    ("Dark mode for settings", "the toggle flickers on load", 86400, 11, 93_000, "web"),
    ("Center the div", "it's centered. nobody touch it", 2 * 86400, 41, 196_000, "web"),
    ("Teach the toaster to make coffee", "it made toast again", 3 * 86400, 7, 52_000, "iot"),
    ("Rewrite it in Rust", "no", 5 * 86400, 1, 0, None),
    ("Plan the team offsite", "somewhere with good wifi", 6 * 86400, 5, 18_000, "notes"),
    ("Hello, world", "it works on my machine", 365 * 86400, 2, 3_000, None),
]
DEMO_REPLIES = {
    6: "Resumed \"Center the div\": it is still centered. Leave it.",
    7: "Resumed \"Teach the toaster to make coffee\": it made toast.",
    8: "Resumed \"Rewrite it in Rust\": still compiling.",
    10: "Resumed \"Hello, world\": hello.",
}


def demo(data, current, here, arg):
    if arg == "next":
        answer(here, "No more sessions. You've seen them all.")
        return
    if arg.isdigit():
        n = int(arg)
        answer(here, DEMO_REPLIES.get(n) or (f"\"{DEMO[n - 1][0]}\" is a demo session; there is nothing to resume. "
                                             "Send /rresume for your real sessions." if 1 <= n <= len(DEMO)
                                             else f"No session {n}. Even the demo has only {len(DEMO)}."))
        return
    cwd, now = data.get("cwd") or str(Path.home()), time.time()
    items = [{"title": title, "last": last, "mtime": now - age, "turns": turns, "tokens": tokens,
              "cwd": str(Path(cwd).parent / project) if project else cwd, "project": project or Path(cwd).name}
             for title, last, age, turns, tokens, project in DEMO]
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"list-{current}.json").write_text(json.dumps({"demo": True, "query": None, "start": 0, "sessions": []}))
    show(here, items, 1, f"Sessions 1–{len(items)}", cwd)


def main():
    if sys.argv[1:2] == ["--archive"]:
        archive(*sys.argv[2:4])
        return
    if sys.argv[1:2] == ["--carry"]:
        apply_carried(sys.argv[2])
        return
    data = json.load(sys.stdin)
    if sys.argv[1:2] == ["--session-start"]:
        session_start(data)
        return
    m = COMMAND.fullmatch(data.get("prompt", "").strip())
    if not m:
        return
    arg, current, here = (m.group(1) or "").strip(), data.get("session_id", ""), remote_session_id()
    if arg.split(" ", 1)[0] == "--demo":
        demo(data, current, here, arg[len("--demo"):].strip())
        return
    if arg.isdigit() and (last_listing(current) or {}).get("demo"):
        demo(data, current, here, arg)
        return
    if arg in ("next", "prev") and (last_listing(current) or {}).get("demo"):
        demo(data, current, here, "next" if arg == "next" else "")
        return
    try:
        if arg.isdigit():
            resume(data, int(arg), here)
            return
        last = last_listing(current) or {"query": None, "start": 0}
        if arg in ("next", "prev"):
            query = last["query"]
            start = last["start"] + PAGE if arg == "next" else max(last["start"] - PAGE, 0)
        else:
            query, start = arg or None, 0
        summaries = Summaries()
        items = listing(current, here, query, start, summaries)
        if not items and arg == "next":
            listing(current, here, query, last["start"], summaries)  # stay on the last real page, so prev steps back
        summaries.save()
    except Problem as problem:
        answer(here, f"/rresume: {problem}.")
        return
    except (urllib.error.URLError, OSError) as error:
        answer(here, f"/rresume: {http_problem(error)}.")
        return
    label = f"\"{query}\"" if query else "Sessions"
    if items:
        show(here, items, start + 1, f"{label} {start + 1}–{start + len(items)}", data.get("cwd"))
    elif arg == "next":
        show(here, [], empty="No more sessions.")
    else:
        show(here, [], empty=f"No sessions match \"{query}\"." if query else "No sessions.")


if __name__ == "__main__":
    main()
