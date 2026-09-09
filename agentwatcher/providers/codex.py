"""Provider: Codex rollouts (~/.codex/sessions), CLI and IDE surfaces.

Strikes are failure-based (turn_aborted events + ERROR rows in logs_2.sqlite);
latencies are kept for display only.
"""
import json
import os
import re
import sqlite3
import time

from agentwatcher.core import (
    Session, iso_ts, prompt_snippet, read_new_lines, recent_files,
)


def scan(state, cfg):
    pcfg = cfg["providers"]["codex"]
    sessions = state.setdefault("sessions", {})
    err_window_s = pcfg["error_window_s"]
    active = []
    for path in recent_files(pcfg["glob"], cfg["history_hours"] * 3600):
        s = sessions.get(path)
        if s is None:
            s = sessions[path] = Session(
                key=path, tool="Codex", label="?", use_latency_strikes=False
            )
        starts = state.setdefault("starts", {}).setdefault(path, {})
        for line in read_new_lines(s, path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            p = r.get("payload", {})
            t = p.get("type")
            ts = iso_ts(r["timestamp"]) if r.get("timestamp") else None
            if t == "turn_context" or r.get("type") in ("turn_context", "session_meta"):
                if p.get("parent_thread_id"):
                    s.hidden = True
                if "codex_vscode" in line:
                    s.tool = "Codex (IDE)"
                s.model = p.get("model") or s.model
                cwd = p.get("cwd")
                if cwd:
                    s.label = os.path.basename(cwd) or cwd
            elif t == "user_message":
                s.title = prompt_snippet(p.get("message")) or s.title
            elif t == "token_count":
                info = p.get("info") or {}
                usage = info.get("last_token_usage") or {}
                if usage.get("total_tokens"):
                    s.tokens_used = usage["total_tokens"]
                if info.get("model_context_window"):
                    s.context_window = info["model_context_window"]
            elif t == "task_started" and ts:
                starts[p.get("turn_id")] = ts
                if p.get("model_context_window"):
                    s.context_window = p["model_context_window"]
                s.last_activity = max(s.last_activity, ts)
            elif t == "task_complete" and ts:
                st = starts.pop(p.get("turn_id"), None)
                if st:
                    s.latencies.append(ts - st)
                s.last_activity = max(s.last_activity, ts)
            elif t == "turn_aborted" and ts:
                s.abort_times.append(ts)
                starts.pop(p.get("turn_id"), None) if p.get("turn_id") else starts.clear()
        s.in_flight = bool(starts)
        m = re.search(r"([0-9a-f-]{36})\.jsonl$", path)
        cutoff = time.time() - err_window_s
        s.external_strikes = sum(1 for a in s.abort_times if a >= cutoff)
        active.append((s, m.group(1) if m else None))
    _add_db_errors(active, pcfg)
    return list(sessions.values())


def _add_db_errors(active, pcfg):
    """Best-effort per-thread counts: ERROR rows (failure strikes) and network
    retry rows (codex_core::responses_retry WARNs, e.g. 'Reconnecting…')."""
    if not active:
        return
    db = os.path.expanduser(pcfg["error_db"])
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        rows = con.execute(
            "SELECT level, target, feedback_log_body FROM logs WHERE ts >= ?"
            " AND (level='ERROR' OR target='codex_core::responses_retry')",
            (int(time.time() - pcfg["error_window_s"]),),
        ).fetchall()
        con.close()
    except Exception:
        return
    for s, uuid in active:
        if not uuid:
            continue
        mine = [(lv, tg) for lv, tg, body in rows if f"thread_id={uuid}" in (body or "")]
        errors = sum(1 for lv, _ in mine if lv == "ERROR")
        retries = sum(1 for _, tg in mine if tg == "codex_core::responses_retry")
        s.net_error_count = errors + retries
        s.external_strikes += errors
