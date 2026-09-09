"""Provider: Claude Code transcripts (~/.claude/projects), CLI and IDE surfaces."""
import json
import os

from agentwatcher.core import (
    Session, iso_ts, pair_latencies, prompt_snippet, read_new_lines, recent_files,
)


def scan(state, cfg):
    pcfg = cfg["providers"]["claude_code"]
    sessions = state.setdefault("sessions", {})
    for path in recent_files(pcfg["glob"], cfg["history_hours"] * 3600):
        s = sessions.get(path)
        if s is None:
            s = sessions[path] = Session(key=path, tool="Claude Code", label="?")
        for line in read_new_lines(s, path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("type")
            # "last-prompt" reflects the newest typed prompt even while it is
            # still queued behind a running turn (no user record exists yet).
            if t == "last-prompt":
                s.title = prompt_snippet(r.get("lastPrompt")) or s.title
                continue
            if t == "queue-operation" and r.get("timestamp"):
                s.last_activity = max(s.last_activity, iso_ts(r["timestamp"]))
                continue
            if r.get("isSidechain") or r.get("isMeta") or t not in ("user", "assistant"):
                continue
            ts = r.get("timestamp")
            if not ts:
                continue
            if s.label == "?":
                ep = r.get("entrypoint", "cli")
                s.tool = "Claude Code" + ("" if ep == "cli" else " (IDE)")
                s.label = os.path.basename(r.get("cwd", "") or "?")
            if t == "user":
                s.title = prompt_snippet(r.get("message", {}).get("content")) or s.title
            else:
                s.model = r.get("message", {}).get("model") or s.model
                s.branch = r.get("gitBranch") or s.branch
                usage = r.get("message", {}).get("usage") or {}
                used = sum(usage.get(k, 0) or 0 for k in (
                    "input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens"))
                if used:
                    s.tokens_used = used
                    s.context_window = cfg["claude_context_window"]
            s.events.append({"type": t, "ts": iso_ts(ts)})
            s.last_activity = max(s.last_activity, iso_ts(ts))
        s.latencies = pair_latencies(s.events)
    return list(sessions.values())
