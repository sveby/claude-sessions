#!/usr/bin/env python3
"""
Claude Session Porter
=====================

A small interactive CLI to move Claude sessions between machines.

It handles two distinct kinds of session:

1. **Claude Code** sessions (the CLI and the VS Code "claude-code" extension),
   stored at ``~/.claude/projects/<encoded-path>/<session-id>.jsonl`` where
   ``<encoded-path>`` is the project's absolute path with every non-alphanumeric
   character replaced by ``-``. The codebase path is baked into the content
   thousands of times, so importing onto a machine where the code lives at a
   different path rewrites those occurrences.

2. **VS Code native Chat panel** sessions (GitHub Copilot Chat, which can use
   Claude models), stored at
   ``~/.config/Code/User/workspaceStorage/<hash>/chatSessions/<id>.jsonl`` (and
   ``globalStorage/emptyWindowChatSessions``). These are self-contained, so they
   are copied whole; on import they are placed under the workspace storage that
   maps to the chosen folder (looked up via ``workspace.json``).

Run it:
    python3 claude_session_porter.py

Works with the standard library alone. If the optional ``questionary`` package
is installed it is used automatically for nicer arrow-key / checkbox menus.
"""

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import questionary  # type: ignore
    HAVE_Q = True
except Exception:  # pragma: no cover - import guard
    questionary = None
    HAVE_Q = False

SCHEMA_VERSION = 2
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Sentinel returned by prompts/steps to mean "go back one step".
BACK = object()
# Sentinel returned by a step that has nothing to do: keep moving in the current
# direction so it is transparent to Back navigation.
SKIP = object()

# VS Code-family config roots to scan for native Chat sessions.
def _editor_bases():
    home = Path.home()
    if sys.platform == "darwin":
        root = home / "Library" / "Application Support"
        names = ["Code", "Code - OSS", "VSCodium", "Cursor"]
    elif os.name == "nt":  # pragma: no cover - windows
        root = Path(os.environ.get("APPDATA", home))
        names = ["Code", "Code - OSS", "VSCodium", "Cursor"]
    else:
        root = home / ".config"
        names = ["Code", "Code - OSS", "VSCodium", "Cursor"]
    return [root / n for n in names if (root / n).is_dir()]


# --------------------------------------------------------------------------- #
# Path encoding (matches Claude Code's rule, validated against real data)
# --------------------------------------------------------------------------- #
def encode_path(abs_path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", abs_path)


def uri_to_path(uri: str) -> str:
    """Convert a 'file://…' workspace URI to a plain filesystem path."""
    if uri and uri.startswith("file://"):
        return unquote(urlparse(uri).path)
    return uri or ""


def path_to_uri(path: str) -> str:
    return "file://" + str(path)


# --------------------------------------------------------------------------- #
# Prompt helpers (questionary when available, else stdlib). All support `back`.
# --------------------------------------------------------------------------- #
def _abort():
    print("\nAborted.")
    sys.exit(1)


def ask_text(message: str, default: str = "", allow_back: bool = False):
    hint = "  (':back' to go back)" if allow_back else ""
    if HAVE_Q:
        ans = questionary.text(message + hint, default=default).ask()
        if ans is None:
            _abort()
        ans = ans.strip()
    else:
        suffix = f" [{default}]" if default else ""
        try:
            ans = input(f"{message}{hint}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            _abort()
        ans = ans or default
    if allow_back and ans.lower() in (":back", "back"):
        return BACK
    return ans


def ask_confirm(message: str, default: bool = True, allow_back: bool = False):
    hint = "  (':back' to go back)" if allow_back else ""
    if HAVE_Q:
        if allow_back:
            # questionary.confirm can't express "back"; fall through to text-ish.
            raw = questionary.text(message + f" [{'Y/n' if default else 'y/N'}]" + hint).ask()
            if raw is None:
                _abort()
            raw = raw.strip().lower()
            if raw in (":back", "back"):
                return BACK
            if not raw:
                return default
            return raw.startswith("y")
        ans = questionary.confirm(message, default=default).ask()
        if ans is None:
            _abort()
        return ans
    d = "Y/n" if default else "y/N"
    try:
        raw = input(f"{message}{hint} [{d}]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _abort()
    if allow_back and raw in (":back", "back"):
        return BACK
    if not raw:
        return default
    return raw.startswith("y")


def ask_select(message: str, choices, allow_back: bool = False):
    """choices: list of (label, value). Returns chosen value (or BACK)."""
    if not choices and not allow_back:
        return None
    if HAVE_Q:
        q_choices = [questionary.Choice(title=lbl, value=val) for lbl, val in choices]
        if allow_back:
            q_choices.append(questionary.Choice(title="← Back", value=BACK))
        ans = questionary.select(message, choices=q_choices).ask()
        if ans is None:
            _abort()
        return ans
    print(f"\n{message}")
    for i, (lbl, _) in enumerate(choices, 1):
        print(f"  {i:>3}. {lbl}")
    if allow_back:
        print("    b. ← Back")
    while True:
        try:
            raw = input("Enter number: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _abort()
        if allow_back and raw in ("b", "back"):
            return BACK
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1][1]
        print("  Invalid choice, try again.")


def _parse_index_spec(spec: str, n: int):
    spec = spec.strip().lower()
    if spec in ("all", "*"):
        return list(range(n))
    if spec in ("", "none"):
        return []
    picked = set()
    for part in re.split(r"[,\s]+", spec):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                for k in range(int(a), int(b) + 1):
                    if 1 <= k <= n:
                        picked.add(k - 1)
            else:
                raise ValueError(part)
        elif part.isdigit():
            k = int(part)
            if 1 <= k <= n:
                picked.add(k - 1)
            else:
                raise ValueError(part)
        else:
            raise ValueError(part)
    return sorted(picked)


def ask_checkbox(message: str, choices, allow_back: bool = False):
    """choices: list of (label, value). Returns list of values (or BACK)."""
    if not choices:
        return []
    if HAVE_Q:
        q_choices = [questionary.Choice(title=lbl, value=val) for lbl, val in choices]
        if allow_back:
            q_choices.append(questionary.Choice(title="← Back (clear selection)", value=BACK))
        ans = questionary.checkbox(message, choices=q_choices).ask()
        if ans is None:
            _abort()
        if allow_back and BACK in ans:
            return BACK
        return ans
    print(f"\n{message}")
    for i, (lbl, _) in enumerate(choices, 1):
        print(f"  {i:>3}. {lbl}")
    extra = " or 'b' to go back" if allow_back else ""
    print(f"  (enter numbers like '1,3,5-7', or 'all' / 'none'{extra})")
    while True:
        try:
            raw = input("Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            _abort()
        if allow_back and raw.lower() in ("b", "back"):
            return BACK
        try:
            idxs = _parse_index_spec(raw, len(choices))
        except ValueError as e:
            print(f"  Invalid entry: {e}")
            continue
        return [choices[i][1] for i in idxs]


# --------------------------------------------------------------------------- #
# Tiny step runner for Back navigation.
# Each step is f(state) -> BACK (go back) | False (cancel to menu) | anything.
# Returns True if the flow completed, False if it was backed/cancelled out.
# --------------------------------------------------------------------------- #
def run_steps(steps, state):
    i, direction = 0, 1
    while 0 <= i < len(steps):
        res = steps[i](state)
        if res is BACK:
            direction = -1
            i -= 1
        elif res is SKIP:
            i += direction          # transparent: keep going the same way
        elif res is False:
            return False
        else:
            direction = 1
            i += 1
    return i >= len(steps)


# --------------------------------------------------------------------------- #
# Shared text helpers
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Leading markers that mean "this user turn is not a real typed prompt".
_WRAPPER_PREFIXES = (
    "<ide_opened_file>", "<ide_selection>", "<ide_diagnostics>",
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-stdout>", "<local-command-stderr>",
    "<system-reminder>", "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "Caveat:", "[Request interrupted",
)


def _strip_tags(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def _raw_user_text(obj):
    """Return raw text of a user turn, or None if it's a tool-result/non-text turn."""
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else msg
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # A turn that carries a tool_result is not a typed prompt.
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts) if parts else None
    return None


def _is_real_prompt(raw: str) -> bool:
    if not raw:
        return False
    s = raw.strip()
    if s.startswith(_WRAPPER_PREFIXES):
        return False
    return len(_strip_tags(s)) > 10


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def _fmt_date(ts) -> str:
    if not ts:
        return "????-??-?? ??:??"
    try:
        if isinstance(ts, (int, float)):  # epoch millis
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
        s = str(ts).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


# --------------------------------------------------------------------------- #
# Claude Code session discovery
# --------------------------------------------------------------------------- #
def inspect_session(jsonl_path: Path, max_lines: int = 4000):
    cwd = title = first = ts = entrypoint = None
    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if n >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and obj.get("cwd"):
                    cwd = obj["cwd"]
                if ts is None and obj.get("timestamp"):
                    ts = obj["timestamp"]
                if entrypoint is None and obj.get("entrypoint"):
                    entrypoint = obj["entrypoint"]
                if title is None:
                    if obj.get("type") == "ai-title":
                        title = obj.get("title") or obj.get("content")
                    elif obj.get("aiTitle"):
                        title = obj["aiTitle"]
                if first is None and obj.get("type") == "user" and not obj.get("isMeta") \
                        and not obj.get("isSidechain") and not obj.get("isCompactSummary"):
                    raw = _raw_user_text(obj)
                    if raw and _is_real_prompt(raw):
                        first = _strip_tags(raw)
                if cwd and first and entrypoint and (title or n > 200):
                    break
    except OSError:
        pass
    return {"cwd": cwd, "title": title, "first": first, "timestamp": ts, "entrypoint": entrypoint}


def discover_sessions():
    sessions = []
    if not PROJECTS_DIR.exists():
        return sessions
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            sid = jsonl.stem
            meta = inspect_session(jsonl)
            sidecar = proj_dir / sid
            try:
                st = jsonl.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0
            sessions.append({
                "kind": "claude",
                "id": sid,
                "jsonl": jsonl,
                "proj_dir": proj_dir,
                "encoded_dir": proj_dir.name,
                "cwd": meta["cwd"] or "",
                "sidecar": sidecar if sidecar.is_dir() else None,
                "title": meta["title"],
                "first": meta["first"],
                "timestamp": meta["timestamp"],
                "entrypoint": meta["entrypoint"] or "",
                "size": size,
                "sortkey": mtime,
            })
    return sessions


def _claude_source_tag(entrypoint: str) -> str:
    return "VS Code" if (entrypoint or "").startswith("claude-vscode") \
        or "vscode" in (entrypoint or "") else "CLI"


# --------------------------------------------------------------------------- #
# VS Code native Chat discovery
# --------------------------------------------------------------------------- #
def _vscode_chat_meta(path: Path):
    """Parse a VS Code chat .jsonl: model, responder, created, first text, count."""
    model = responder = created = first = None
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                v = obj.get("v", obj)
                if isinstance(v, dict):
                    if responder is None and v.get("responderUsername"):
                        responder = v["responderUsername"]
                    if created is None and v.get("creationDate"):
                        created = v["creationDate"]
                    sm = v.get("inputState", {}).get("selectedModel", {}) if isinstance(v.get("inputState"), dict) else {}
                    if sm:
                        md = sm.get("metadata", {})
                        model = model or md.get("family") or md.get("name")

                def walk(x):
                    nonlocal first, count
                    if isinstance(x, dict):
                        m = x.get("message")
                        if isinstance(m, dict) and isinstance(m.get("text"), str) and m["text"].strip():
                            count += 1
                            if first is None:
                                first = m["text"].strip()
                        for vv in x.values():
                            walk(vv)
                    elif isinstance(x, list):
                        for vv in x:
                            walk(vv)
                walk(obj)
    except OSError:
        pass
    return {"model": model, "responder": responder, "created": created,
            "first": first, "count": count}


def _workspace_folder_for(storage_dir: Path):
    wj = storage_dir / "workspace.json"
    if wj.exists():
        try:
            return json.loads(wj.read_text()).get("folder")
        except Exception:
            return None
    return None


def discover_vscode_chats():
    chats = []
    for base in _editor_bases():
        user = base / "User"
        # Per-workspace chat sessions.
        ws_root = user / "workspaceStorage"
        if ws_root.is_dir():
            for storage in sorted(ws_root.iterdir()):
                cs = storage / "chatSessions"
                if not cs.is_dir():
                    continue
                folder = _workspace_folder_for(storage)
                for jf in cs.glob("*.jsonl"):
                    chats.append(_make_chat_entry(jf, base, folder, "workspace"))
        # Empty-window (no folder) chat history.
        ews = user / "globalStorage" / "emptyWindowChatSessions"
        if ews.is_dir():
            for jf in ews.glob("*.jsonl"):
                chats.append(_make_chat_entry(jf, base, None, "empty-window"))
    return chats


def _make_chat_entry(jf: Path, base: Path, folder, location):
    meta = _vscode_chat_meta(jf)
    try:
        size = jf.stat().st_size
        mtime = jf.stat().st_mtime
    except OSError:
        size = mtime = 0
    created = meta["created"]
    return {
        "kind": "vscode-chat",
        "id": jf.stem,
        "path": jf,
        "editor_base": base,
        "workspace_folder": folder,                 # URI or None
        "workspace_path": uri_to_path(folder) if folder else "",
        "model": meta["model"],
        "responder": meta["responder"],
        "first": meta["first"],
        "created": created,
        "msg_count": meta["count"],
        "location": location,
        "size": size,
        "sortkey": (created / 1000) if created else mtime,
    }


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #
def _snip(text, n=68):
    text = text or "(no preview)"
    return text if len(text) <= n else text[: n - 3] + "..."


def session_label(s) -> str:
    if s["kind"] == "claude":
        tag = _claude_source_tag(s.get("entrypoint", ""))
        proj = Path(s["cwd"]).name if s["cwd"] else s["encoded_dir"]
        snippet = s.get("title") or s.get("first") or "(no preview)"
        return f'[{tag:<7}] {_fmt_date(s["timestamp"])}  {proj}  —  {_snip(snippet)}  ({_human_size(s["size"])})'
    # vscode-chat
    where = Path(s["workspace_path"]).name if s["workspace_path"] else s["location"]
    model = s.get("model") or s.get("responder") or "?"
    empty = "  [empty]" if not s["msg_count"] else f'  [{s["msg_count"]} msgs]'
    snippet = s.get("first") or "(no preview)"
    return (f'[VSCodeChat] {_fmt_date(s.get("created"))}  {where}  ({model})  —  '
            f'{_snip(snippet)}  ({_human_size(s["size"])}){empty}')


def discover_all():
    items = discover_sessions() + discover_vscode_chats()
    items.sort(key=lambda s: s.get("sortkey", 0), reverse=True)
    return items


# --------------------------------------------------------------------------- #
# EXPORT
# --------------------------------------------------------------------------- #
def do_export():
    print("\n=== EXPORT ===")
    items = discover_all()
    if not items:
        print(f"No sessions found under {PROJECTS_DIR} or VS Code storage.")
        return True

    state = {"items": items}

    def step_select(st):
        choices = [(session_label(s), s) for s in st["items"]]
        chosen = ask_checkbox("Select sessions to export:", choices, allow_back=True)
        if chosen is BACK:
            return BACK
        if not chosen:
            print("Nothing selected.")
            return BACK
        st["chosen"] = chosen
        return True

    def step_memory(st):
        include = {}
        claude_projects = {}
        for s in st["chosen"]:
            if s["kind"] == "claude":
                claude_projects.setdefault(s["encoded_dir"], s)
        for enc, s in claude_projects.items():
            mem = s["proj_dir"] / "memory"
            if mem.is_dir() and any(mem.iterdir()):
                name = Path(s["cwd"]).name if s["cwd"] else enc
                ans = ask_confirm(f"Include Claude memory for project '{name}'?", default=True, allow_back=True)
                if ans is BACK:
                    return BACK
                if ans:
                    include[enc] = mem
        st["include_memory"] = include
        return True if claude_projects and any(
            (s["proj_dir"] / "memory").is_dir() and any((s["proj_dir"] / "memory").iterdir())
            for s in claude_projects.values()) else SKIP

    def step_output(st):
        default_name = f"claude-sessions-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        out = ask_text("Output .zip path", default=str(Path.cwd() / default_name), allow_back=True)
        if out is BACK:
            return BACK
        p = Path(out).expanduser()
        if p.is_dir():
            p = p / default_name
        if p.suffix.lower() != ".zip":
            p = p.with_suffix(".zip")
        if p.exists():
            ans = ask_confirm(f"{p} exists. Overwrite?", default=False, allow_back=True)
            if ans is BACK:
                return BACK
            if not ans:
                print("Pick another path.")
                return step_output(st)
        st["out_path"] = p
        return True

    def step_write(st):
        _write_export(st["chosen"], st["include_memory"], st["out_path"])
        return True

    return run_steps([step_select, step_memory, step_output, step_write], state)


def _write_export(chosen, include_memory, out_path: Path):
    manifest = {
        "schema": SCHEMA_VERSION,
        "tool": "claude-session-porter",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_host": socket.gethostname(),
        "projects_dir": str(PROJECTS_DIR),
        "sessions": [],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_claude = n_chat = 0
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for s in chosen:
            if s["kind"] == "claude":
                n_claude += 1
                sid = s["id"]
                arc = f"sessions/{sid}.jsonl"
                zf.write(s["jsonl"], arc)
                arc_side = None
                if s["sidecar"]:
                    arc_side = f"sessions/{sid}"
                    for fp in s["sidecar"].rglob("*"):
                        if fp.is_file():
                            zf.write(fp, f"{arc_side}/{fp.relative_to(s['sidecar']).as_posix()}")
                manifest["sessions"].append({
                    "kind": "claude",
                    "id": sid,
                    "project_cwd": s["cwd"],
                    "encoded_dir": s["encoded_dir"],
                    "entrypoint": s.get("entrypoint", ""),
                    "jsonl": arc,
                    "sidecar": arc_side,
                    "memory": f"memory/{s['encoded_dir']}" if s["encoded_dir"] in include_memory else None,
                    "title": s["title"],
                    "first_message": s["first"],
                    "first_timestamp": s["timestamp"],
                    "size_bytes": s["size"],
                })
            else:  # vscode-chat
                n_chat += 1
                sid = s["id"]
                arc = f"vscode-chat/{sid}.jsonl"
                zf.write(s["path"], arc)
                manifest["sessions"].append({
                    "kind": "vscode-chat",
                    "id": sid,
                    "file": arc,
                    "workspace_folder": s["workspace_folder"],
                    "workspace_path": s["workspace_path"],
                    "workspace_basename": Path(s["workspace_path"]).name if s["workspace_path"] else "",
                    "model": s["model"],
                    "responder": s["responder"],
                    "first_message": s["first"],
                    "created": s["created"],
                    "msg_count": s["msg_count"],
                    "location": s["location"],
                    "size_bytes": s["size"],
                })
        for enc, mem in include_memory.items():
            for fp in mem.rglob("*"):
                if fp.is_file():
                    zf.write(fp, f"memory/{enc}/{fp.relative_to(mem).as_posix()}")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    parts = []
    if n_claude:
        parts.append(f"{n_claude} Claude")
    if n_chat:
        parts.append(f"{n_chat} VS Code Chat")
    print(f"\nExported {' + '.join(parts)} session(s) to:\n  {out_path}")
    print("Copy that file to the other machine and run this tool there with 'import'.")


# --------------------------------------------------------------------------- #
# IMPORT
# --------------------------------------------------------------------------- #
def _rewrite_line(line: str, replacements) -> str:
    for old, new in replacements:
        if old and old != new:
            line = line.replace(old, new)
    return line


def _copy_rewrite_file(src: Path, dst: Path, replacements):
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(src, encoding="utf-8") as fin, open(dst, "w", encoding="utf-8") as fout:
            for line in fin:
                fout.write(_rewrite_line(line, replacements))
    except UnicodeDecodeError:
        shutil.copy2(src, dst)


def _load_export(source: str):
    src = Path(source).expanduser()
    if not src.exists():
        print(f"Not found: {src}")
        return None
    if src.is_dir():
        mp = src / "manifest.json"
        if not mp.exists():
            print(f"No manifest.json in {src}")
            return None
        return src, json.loads(mp.read_text()), (lambda: None)
    tmp = Path(tempfile.mkdtemp(prefix="csp-import-"))
    try:
        with zipfile.ZipFile(src) as zf:
            zf.extractall(tmp)
    except zipfile.BadZipFile:
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"Not a valid .zip: {src}")
        return None
    mp = tmp / "manifest.json"
    if not mp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        print("Export is missing manifest.json")
        return None
    return tmp, json.loads(mp.read_text()), (lambda: shutil.rmtree(tmp, ignore_errors=True))


def do_import():
    print("\n=== IMPORT ===")
    state = {}

    def step_source(st):
        source = ask_text("Path to the export (.zip file or extracted folder)", allow_back=True)
        if source is BACK:
            return BACK
        loaded = _load_export(source)
        if not loaded:
            return step_source(st)
        st["root"], st["manifest"], st["cleanup"] = loaded
        return True

    def step_select(st):
        sess = st["manifest"].get("sessions", [])
        if not sess:
            print("Export contains no sessions.")
            return BACK
        host = st["manifest"].get("source_host", "?")
        when = st["manifest"].get("exported_at", "?")
        print(f"Export from host '{host}' at {when}, {len(sess)} session(s).")
        choices = [(_manifest_label(m), m) for m in sess]
        chosen = ask_checkbox("Select sessions to import:", choices, allow_back=True)
        if chosen is BACK:
            return BACK
        if not chosen:
            print("Nothing selected.")
            return BACK
        st["chosen"] = chosen
        return True

    def step_targets(st):
        # Claude: new codebase path per project. VS Code chat: editor base + folder.
        claude = [m for m in st["chosen"] if m.get("kind", "claude") == "claude"]
        chats = [m for m in st["chosen"] if m.get("kind") == "vscode-chat"]

        new_paths = {}
        for src_cwd in {m.get("project_cwd") or m.get("encoded_dir") for m in claude}:
            name = Path(src_cwd).name if src_cwd else "?"
            default = src_cwd if (src_cwd and Path(src_cwd).exists()) else ""
            print(f"\nClaude project '{name}' was at:\n  {src_cwd}")
            while True:
                new = ask_text("Where is this codebase on THIS machine?", default=default, allow_back=True)
                if new is BACK:
                    return BACK
                new = str(Path(new).expanduser())
                if not new:
                    print("  A path is required.")
                    continue
                if not Path(new).exists():
                    ans = ask_confirm(f"  '{new}' does not exist yet. Use it anyway?", default=False)
                    if not ans:
                        continue
                new_paths[src_cwd] = new
                break
        st["new_paths"] = new_paths

        chat_targets = {}
        if chats:
            bases = _editor_bases()
            if bases:
                base = bases[0] if len(bases) == 1 else ask_select(
                    "Which VS Code app should receive the chat sessions?",
                    [(b.name, b) for b in bases], allow_back=True)
                if base is BACK:
                    return BACK
            else:
                # No editor config yet; default to ~/.config/Code and create it.
                base = Path.home() / ".config" / "Code"
                print(f"No VS Code config found; will create under {base}")
            for src_folder in {m.get("workspace_path") or "" for m in chats}:
                default = src_folder if (src_folder and Path(src_folder).exists()) else ""
                print(f"\nVS Code chat workspace was:\n  {src_folder or '(empty-window / no folder)'}")
                new = ask_text("Folder for this workspace on THIS machine (blank = global chat history)",
                               default=default, allow_back=True)
                if new is BACK:
                    return BACK
                chat_targets[src_folder] = {"base": base, "new_folder": str(Path(new).expanduser()) if new else ""}
            st["chat_targets"] = chat_targets
        return True

    def step_apply(st):
        claude = [m for m in st["chosen"] if m.get("kind", "claude") == "claude"]
        chats = [m for m in st["chosen"] if m.get("kind") == "vscode-chat"]
        n_c = _import_claude(st["root"], st["manifest"], claude, st["new_paths"])
        n_v = _import_vscode_chats(st["root"], chats, st.get("chat_targets", {}))
        print(f"\nDone. Imported {n_c} Claude + {n_v} VS Code Chat session(s).")
        if n_c:
            print("Open the project in VS Code / Claude Code to continue those sessions.")
        if n_v:
            print("Open VS Code (the matching folder, or the chat history) to continue chats.")
        return True

    ok = run_steps([step_source, step_select, step_targets, step_apply], state)
    if state.get("cleanup"):
        state["cleanup"]()
    return ok


def _manifest_label(m) -> str:
    kind = m.get("kind", "claude")
    if kind == "claude":
        tag = _claude_source_tag(m.get("entrypoint", ""))
        proj = Path(m["project_cwd"]).name if m.get("project_cwd") else m.get("encoded_dir", "?")
        snip = m.get("title") or m.get("first_message") or "(no preview)"
        return f'[{tag:<7}] {_fmt_date(m.get("first_timestamp"))}  {proj}  —  {_snip(snip)}'
    where = Path(m.get("workspace_path", "")).name or m.get("location", "?")
    model = m.get("model") or m.get("responder") or "?"
    snip = m.get("first_message") or "(no preview)"
    return f'[VSCodeChat] {_fmt_date(m.get("created"))}  {where}  ({model})  —  {_snip(snip)}'


def _collision_action(what: str):
    return ask_select(
        f"{what} already exists.",
        [("Overwrite", "overwrite"), ("Skip", "skip"),
         ("Import as a new copy (new id)", "rename")],
    )


def _import_claude(root: Path, manifest: dict, items, new_paths) -> int:
    if not items:
        return 0
    src_projects_dir = manifest.get("projects_dir", "")
    imported = 0
    for m in items:
        src_cwd = m.get("project_cwd") or m.get("encoded_dir")
        new_cwd = new_paths[src_cwd]
        new_enc = encode_path(os.path.abspath(new_cwd))
        target_dir = PROJECTS_DIR / new_enc
        target_dir.mkdir(parents=True, exist_ok=True)

        replacements = [
            (src_projects_dir, str(PROJECTS_DIR)),
            (src_cwd, new_cwd),
            (encode_path(src_cwd) if src_cwd else "", new_enc),
        ]
        sid = m["id"]
        dst = target_dir / f"{sid}.jsonl"
        if dst.exists():
            action = _collision_action(f"Session {sid[:8]}… in {new_enc}")
            if action == "skip":
                print(f"  skipped {sid[:8]}…")
                continue
            if action == "rename":
                new_id = str(uuid.uuid4())
                replacements.append((sid, new_id))
                sid = new_id
                dst = target_dir / f"{sid}.jsonl"
        _copy_rewrite_file(root / m["jsonl"], dst, replacements)
        if m.get("sidecar"):
            src_side = root / m["sidecar"]
            if src_side.is_dir():
                for fp in src_side.rglob("*"):
                    if fp.is_file():
                        _copy_rewrite_file(fp, target_dir / sid / fp.relative_to(src_side), replacements)
        if m.get("memory"):
            src_mem = root / m["memory"]
            dst_mem = target_dir / "memory"
            if src_mem.is_dir() and (not dst_mem.exists()
                                     or ask_confirm(f"Memory exists for {new_enc}. Overwrite?", default=False)):
                for fp in src_mem.rglob("*"):
                    if fp.is_file():
                        _copy_rewrite_file(fp, dst_mem / fp.relative_to(src_mem), replacements)
        imported += 1
        print(f"  [Claude]  imported {sid[:8]}…  ->  {target_dir}")
    return imported


def _find_workspace_storage(base: Path, target_path: str):
    ws_root = base / "User" / "workspaceStorage"
    if not ws_root.is_dir():
        return None
    tgt = os.path.abspath(target_path)
    for d in ws_root.iterdir():
        folder = _workspace_folder_for(d)
        if folder and os.path.abspath(uri_to_path(folder)) == tgt:
            return d
    return None


def _import_vscode_chats(root: Path, items, chat_targets) -> int:
    if not items:
        return 0
    imported = 0
    for m in items:
        src_folder = m.get("workspace_path") or ""
        tgt = chat_targets.get(src_folder, {})
        base = tgt.get("base") or (Path.home() / ".config" / "Code")
        new_folder = tgt.get("new_folder") or ""

        dest_dir = None
        if new_folder:
            storage = _find_workspace_storage(base, new_folder)
            if storage:
                dest_dir = storage / "chatSessions"
            else:
                print(f"  No existing VS Code workspace storage for:\n    {new_folder}")
                choice = ask_select(
                    "That folder hasn't been opened in this VS Code yet.",
                    [("Put in global chat history (always visible)", "global"),
                     ("Skip (open the folder in VS Code once, then re-run)", "skip")])
                if choice == "skip":
                    print(f"  skipped {m['id'][:8]}…")
                    continue
                dest_dir = base / "User" / "globalStorage" / "emptyWindowChatSessions"
        else:
            dest_dir = base / "User" / "globalStorage" / "emptyWindowChatSessions"

        dest_dir.mkdir(parents=True, exist_ok=True)
        sid = m["id"]

        # Best-effort path rewrite so file references resolve on this machine.
        replacements = []
        if src_folder and new_folder and src_folder != new_folder:
            replacements = [
                (path_to_uri(src_folder), path_to_uri(new_folder)),
                (src_folder, new_folder),
            ]

        dst = dest_dir / f"{sid}.jsonl"
        if dst.exists():
            action = _collision_action(f"Chat {sid[:8]}…")
            if action == "skip":
                print(f"  skipped {sid[:8]}…")
                continue
            if action == "rename":
                new_id = str(uuid.uuid4())
                replacements.append((sid, new_id))
                sid = new_id
                dst = dest_dir / f"{sid}.jsonl"

        _copy_rewrite_file(root / m["file"], dst, replacements)
        imported += 1
        where = "global chat history" if dest_dir.name == "emptyWindowChatSessions" else "workspace storage"
        print(f"  [VSCodeChat]  imported {sid[:8]}…  ->  {where}")
    return imported


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    print("Claude Session Porter")
    if not HAVE_Q:
        print("(tip: `pip install questionary` for arrow-key menus; using plain prompts)")
    while True:
        action = ask_select(
            "What do you want to do?",
            [("Export sessions from this machine", "export"),
             ("Import sessions into this machine", "import"),
             ("Quit", "quit")],
        )
        if action == "export":
            do_export()
        elif action == "import":
            do_import()
        else:
            print("Bye.")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _abort()
