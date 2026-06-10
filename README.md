# Claude Session Porter

Move Claude sessions between machines — export on one computer, import on
another and keep working, even when the project lives at a different path.

It handles **two kinds** of session:

- **`[CLI]` / `[VS Code]` — Claude Code sessions** (the `claude` CLI and the
  Claude Code VS Code extension). Stored in `~/.claude/projects/`. Each session
  is tagged by where it came from (`entrypoint` inside the transcript).
- **`[VS Code Chat]` — VS Code's built-in Chat panel** (GitHub Copilot Chat,
  which can use Claude models). Stored in
  `~/.config/Code/User/workspaceStorage/<hash>/chatSessions/` and the global
  empty-window chat history.

## Usage

```bash
python3 claude_session_porter.py
```

It asks one thing at a time. **`b` / `:back`** at any menu goes back a step.

**Export**
1. Pick sessions from one combined, newest-first list. Each row is tagged
   `[CLI]`, `[VS Code]`, or `[VS Code Chat]`, with date, project/model, a preview
   of your first real prompt, and size.
2. Optionally include each Claude project's `memory/` folder.
3. Choose an output `.zip` path.

Copy the `.zip` to the other machine.

**Import** — the two kinds are handled separately:
1. Point it at the export (`.zip` or extracted folder) and pick sessions.
2. **Claude sessions:** tell it where each codebase lives on this machine; it
   rewrites the embedded paths and installs under
   `~/.claude/projects/<encoded>/`.
3. **VS Code Chat sessions:** tell it the matching folder on this machine. If
   that folder has been opened in VS Code before, the chat is placed in its
   workspace storage; otherwise it goes to the **global chat history** (always
   visible) — or you can skip, open the folder in VS Code once, and re-run.

## How it works

### Claude Code sessions
Stored at `~/.claude/projects/<encoded-path>/<id>.jsonl`, where `<encoded-path>`
is the project's absolute path with every non-alphanumeric character replaced by
`-` (lossy, so the real path is always read from the `cwd` field, never decoded
from the folder name). The codebase path is baked into the content thousands of
times, so on import the tool rewrites, throughout each session:

- the old codebase path → the new one you give it,
- the old encoded directory name → the new one,
- the source machine's `~/.claude/projects` base → this machine's (handles a
  different home directory / username),

then installs the files (plus `subagents/` / `tool-results/` sidecars and
optional `memory/`) under the correct directory.

### VS Code Chat sessions
Each chat `.jsonl` is self-contained, so it's copied whole. The workspace it
belongs to is found by reading each `workspaceStorage/*/workspace.json`
(`hash → folder` map) — the hash is **never** computed, only looked up. File
references inside are best-effort rewritten to the new folder path.

Session-id collisions (either kind) offer **overwrite**, **skip**, or **import as
a new copy** (fresh id).

## Optional: nicer menus

Pure standard library — nothing to install. If
[`questionary`](https://pypi.org/project/questionary/) is present it's used
automatically for arrow-key / checkbox menus:

```bash
pip install questionary   # optional
```

## Caveats

- Path rewriting is a literal string replace. If a session's codebase path is a
  strict prefix of an *unrelated* sibling (e.g. `/foo/bar` vs `/foo/barbaz`), the
  sibling could be rewritten too. In practice Claude paths are the exact project
  dir or files beneath it, so this is rare.
- Copy a session while its app is **not actively writing** to it, to avoid
  grabbing a half-written line.
- VS Code Chat support targets the official `Code` config layout (also tries
  `Code - OSS`, `VSCodium`, `Cursor`). A chat placed in workspace storage only
  appears when you open that same folder in VS Code.
