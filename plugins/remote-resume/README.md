<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ethbak/claude-code-minis/main/assets/remote-resume-header-dark.png">
  <img src="https://raw.githubusercontent.com/ethbak/claude-code-minis/main/assets/remote-resume-header-light.png" alt="remote-resume: resume any Claude Code session from your phone" width="880">
</picture>

# remote-resume: resume Claude Code sessions from the Claude app

<p align="center"><img src="https://raw.githubusercontent.com/ethbak/claude-code-minis/main/assets/screenshots/rresume-list.png" alt="/rresume in the Claude iPhone app: a table of recent sessions with their title, last prompt, age, turns and context size" width="320"></p>

`/rresume` lists, searches and resumes your Claude Code sessions from the Claude app. It works in [Remote Control](https://code.claude.com/docs/en/remote-control) sessions, where the built-in `/resume` does not. Archived chats come back under their own id. Sessions you started in a terminal move into Remote Control with their full history.

The app has no `/resume` and does not show terminal sessions. `/rresume` lists all of them, and `/rresume 3` resumes the third.

> [!TIP]
> Part of [claude-code-minis](https://github.com/ethbak/claude-code-minis), small, focused plugins for Claude Code.

## ✨ Features

- 📋 **Lists your sessions**, newest first, with title, last prompt, age, turns and context size.
- 🔎 **Searches** titles and every prompt you typed.
- ♻️ **Resumes a Remote Control chat** under its own id, archived or not, the way `/resume` would.
- 💻 **Moves a terminal session into Remote Control** with its full history, file checkpoints, model, effort, permission mode and working directory.

<p align="center"><img src="https://raw.githubusercontent.com/ethbak/claude-code-minis/main/assets/screenshots/rresume-resumed.png" alt="/rresume 10: the reply links to the resumed chat and says this chat had no other content and will be archived; the app then shows it as archived" width="320"></p>

> [!IMPORTANT]
> **Archives its own leftovers and costs no tokens.** After a resume, the chat you typed `/rresume` in is archived if it held nothing else, so throwaway chats do not pile up. The plugin writes every reply itself, so `/rresume` uses no Claude turn.

## ⚖️ Compared with `/resume`

| | `/resume` | `/rresume` |
|---|:---:|:---:|
| Works from the Claude app | No | Yes |
| Moves a terminal session into the app | No | Yes |
| Archives the empty chat you ran it from | No | Yes |

## 📦 Install

```
/plugin marketplace add ethbak/claude-code-minis
/plugin install remote-resume@claude-code-minis
```

Resuming needs a running Remote Control server: `claude remote-control` in the project folder, or `/remote-control` in a session.

## 🚀 Usage

| You type | What happens |
|---|---|
| `/rresume` | The 10 most recent sessions, numbered |
| `/rresume deploy` | The 10 most recent whose title or prompts mention "deploy" |
| `/rresume next` / `prev` | The next 10, or back; numbering continues |
| `/rresume 3` | Resume number 3 from the last list this chat showed |

## 🔐 What it accesses

- **Your session files** in `~/.claude/projects`, read locally to build the list (titles and the prompts you typed).
- **Your Claude Code login**, read from the macOS Keychain or `~/.claude/.credentials.json`, to call the claude.ai API at `api.anthropic.com`. It lists your sessions and Remote Control environments, reads a session's recent events, archives, unarchives and reconnects sessions, and posts its reply into the chat. Moving a terminal session in creates a new Remote Control session and uploads that session's transcript to it.
- **A cache** in the plugin's data folder, so later lists only read what changed.

It sends nothing anywhere else.

## ⚙️ Options

| Option | Default | What it does |
|---|---|---|
| `auto_archive` | on | After a resume, archives the chat you typed `/rresume` in when it holds only `/rresume` commands and at most one other message. Chats with real content stay as they are. Turn it off in `/config` or with `claude plugin configure remote-resume`. |

## 🔍 What resuming does

- **A Remote Control chat**, archived or not, comes back under its own id, the way `/resume` would. The reply links to it.
- **A terminal session** is moved into Remote Control as a new chat, with its history, file checkpoints, model, effort, permission mode and working directory. It drops out of the list until you use it in a terminal again.

> [!NOTE]
> Moving a terminal session into Remote Control uploads its transcript to claude.ai, like any Remote Control chat.

## 🩺 Troubleshooting

<details>
<summary><b>"No Remote Control service picked it up"</b></summary>

Nothing is running to resume the chat into. Start one in that project folder with `claude remote-control`, then send `/rresume <n>` again.

</details>

<details>
<summary><b>My <code>/rresume</code> went to Claude as text</b></summary>

- **The chat started before you installed the plugin.** Open a new chat.
- **You sent it while Claude was replying.** Wait for the reply to finish, then send it again.

</details>

<details>
<summary><b>The first list is slow</b></summary>

The first `/rresume` reads every session on your computer and can take several seconds. Later lists read only what changed.

</details>

## 🗑️ Uninstall

```
/plugin uninstall remote-resume@claude-code-minis
```

## ❓ FAQ

<details>
<summary><b>Why is <code>/resume</code> unavailable in Remote Control?</b></summary>

`/resume` is part of the terminal interface, and Remote Control sessions run without one. `/rresume` does the same job from the app.

</details>

<details>
<summary><b>Can I unarchive a Claude Code chat?</b></summary>

Yes: `/rresume` lists archived Remote Control chats too, and resuming one unarchives it.

</details>

<details>
<summary><b>Can I continue a terminal Claude Code session on my phone?</b></summary>

Yes. Close it in the terminal, then `/rresume` it from the app. It moves into Remote Control with its history and settings.

</details>
