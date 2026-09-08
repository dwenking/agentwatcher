"""Menu bar watcher: shows health of active AI coding sessions.

Surfaces: Claude Code (CLI + Cursor) via ~/.claude/projects transcripts,
Codex (CLI + Cursor) via ~/.codex/sessions rollouts + logs_2.sqlite errors.
Claude strikes are latency-based (no API errors in its logs); Codex strikes
are failure-based (ERROR rows + turn_aborted).
"""
import glob
import json
import os
import re
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CLAUDE_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")
CODEX_GLOB = os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")
CODEX_DB = os.path.expanduser("~/.codex/logs_2.sqlite")
EMOJI = {"green": "\U0001f7e2", "yellow": "\U0001f7e1", "red": "\U0001f534"}


@dataclass
class Session:
    key: str
    tool: str
    label: str
    use_latency_strikes: bool = True
    title: str = ""
    hidden: bool = False  # subagent/sidechain threads: tracked but not displayed
    latencies: list = field(default_factory=list)
    external_strikes: int = 0
    last_activity: float = 0.0
    offset: int = 0
    events: list = field(default_factory=list)
    abort_times: list = field(default_factory=list)


# --- pure logic -------------------------------------------------------------

def pair_latencies(events):
    """Latency of each API call: gap from a user-side record (prompt or tool
    result) to the next assistant record. Unanswered trailing prompts ignored."""
    out, pending = [], None
    for e in events:
        if e["type"] == "user":
            pending = e["ts"]
        elif e["type"] == "assistant" and pending is not None:
            out.append(e["ts"] - pending)
            pending = None
    return out


def strikes(s, cfg):
    n = s.external_strikes
    if s.use_latency_strikes:
        base_n = cfg["baseline_turns"]
        if len(s.latencies) > base_n:
            baseline = statistics.median(s.latencies[:base_n])
            recent = s.latencies[base_n:][-cfg["strike_window"]:]
            n += sum(
                1 for x in recent
                if x >= baseline * cfg["strike_ratio"] or x >= cfg["strike_absolute_s"]
            )
    return n


def health(s, cfg):
    n = strikes(s, cfg)
    if n >= cfg["alert_strikes"]:
        return "red"
    return "yellow" if n else "green"


# --- scanners ---------------------------------------------------------------

def _iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _recent_files(pattern, idle_s):
    cutoff = time.time() - idle_s
    return [p for p in glob.glob(pattern) if os.path.getmtime(p) >= cutoff]


def _read_new_lines(s, path):
    size = os.path.getsize(path)
    if size <= s.offset:
        return []
    with open(path, "rb") as f:
        f.seek(s.offset)
        data = f.read()
    s.offset = size
    return data.decode("utf-8", "replace").splitlines()


def _prompt_snippet(content, limit=40):
    """First line of a user prompt, skipping tool results and <command> wrappers."""
    if isinstance(content, list):
        content = next((b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), "")
    if not isinstance(content, str):
        return ""
    text = content.strip()
    if not text or text.startswith(("<", "[")):
        return ""
    text = text.splitlines()[0]
    return text[:limit] + ("…" if len(text) > limit else "")


def scan_claude(sessions, idle_s):
    for path in _recent_files(CLAUDE_GLOB, idle_s):
        s = sessions.get(path)
        if s is None:
            s = sessions[path] = Session(key=path, tool="claude", label="?")
        for line in _read_new_lines(s, path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("isSidechain") or r.get("isMeta") or r.get("type") not in ("user", "assistant"):
                continue
            ts = r.get("timestamp")
            if not ts:
                continue
            if s.label == "?":
                ep = r.get("entrypoint", "cli")
                s.tool = "Claude Code" + ("" if ep == "cli" else " (Cursor)")
                s.label = os.path.basename(r.get("cwd", "") or "?")
            if not s.title and r["type"] == "user":
                s.title = _prompt_snippet(r.get("message", {}).get("content"))
            s.events.append({"type": r["type"], "ts": _iso(ts)})
            s.last_activity = max(s.last_activity, _iso(ts))
        s.latencies = pair_latencies(s.events)


def scan_codex(sessions, idle_s, err_window_s):
    active = []
    for path in _recent_files(CODEX_GLOB, idle_s):
        s = sessions.get(path)
        if s is None:
            s = sessions[path] = Session(
                key=path, tool="Codex", label="?", use_latency_strikes=False
            )
        starts = {}
        for line in _read_new_lines(s, path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            p = r.get("payload", {})
            t = p.get("type")
            ts = _iso(r["timestamp"]) if r.get("timestamp") else None
            if t == "turn_context" or r.get("type") == "session_meta":
                if p.get("parent_thread_id"):
                    s.hidden = True
                if "codex_vscode" in line:
                    s.tool = "Codex (Cursor)"
                cwd = p.get("cwd")
                if cwd:
                    s.label = os.path.basename(cwd) or cwd
            elif t == "user_message" and not s.title:
                s.title = _prompt_snippet(p.get("message"))
            elif t == "task_started" and ts:
                starts[p.get("turn_id")] = ts
                s.last_activity = max(s.last_activity, ts)
            elif t == "task_complete" and ts:
                st = starts.pop(p.get("turn_id"), None)
                if st:
                    s.latencies.append(ts - st)
                s.last_activity = max(s.last_activity, ts)
            elif t == "turn_aborted" and ts:
                s.abort_times.append(ts)
        m = re.search(r"([0-9a-f-]{36})\.jsonl$", path)
        uuid = m.group(1) if m else None
        cutoff = time.time() - err_window_s
        s.external_strikes = sum(1 for a in s.abort_times if a >= cutoff)
        active.append((s, uuid))
    _add_codex_db_errors(active, err_window_s)


def _add_codex_db_errors(active, err_window_s):
    """Best-effort: count recent ERROR rows per session thread_id."""
    if not active:
        return
    try:
        con = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT feedback_log_body FROM logs WHERE level='ERROR' AND ts >= ?",
            (int(time.time() - err_window_s),),
        ).fetchall()
        con.close()
    except Exception:
        return
    body = "\n".join(r[0] or "" for r in rows)
    for s, uuid in active:
        if uuid:
            s.external_strikes += body.count(f"thread_id={uuid}")


# --- app --------------------------------------------------------------------

def main():
    import rumps

    cfg = json.load(open(CONFIG_PATH))
    idle_s = cfg["idle_minutes"] * 60
    sessions = {}

    app = rumps.App("session-watcher", title=EMOJI["green"], quit_button="Quit")

    def poll(_):
        try:
            scan_claude(sessions, idle_s)
            scan_codex(sessions, idle_s, cfg["codex_error_window_s"])
        except Exception as e:
            app.title = "⚠️"
            app.menu.clear()
            app.menu = [rumps.MenuItem(f"error: {e}"), None]
            return
        cutoff = time.time() - idle_s
        live = sorted(
            (s for s in sessions.values()
             if s.last_activity >= cutoff and not s.hidden),
            key=lambda s: -s.last_activity,
        )
        worst = "green"
        items = []
        for s in live:
            h = health(s, cfg)
            worst = max(worst, h, key=["green", "yellow", "red"].index)
            med = statistics.median(s.latencies) if s.latencies else 0
            last = s.latencies[-1] if s.latencies else 0
            title = f" “{s.title}”" if s.title else ""
            # menu items without a callback render disabled (grey) on macOS
            items.append(rumps.MenuItem(
                f"{EMOJI[h]} {s.tool} ({s.label}){title} — last {last:.0f}s,"
                f" med {med:.0f}s, {strikes(s, cfg)} strikes",
                callback=lambda _: None))
        app.title = EMOJI[worst]
        app.menu.clear()
        app.menu = items + [None] if items else [rumps.MenuItem("no active sessions"), None]

    rumps.Timer(poll, cfg["poll_seconds"]).start()
    app.run()


if __name__ == "__main__":
    main()
