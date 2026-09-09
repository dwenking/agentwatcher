"""Provider: Claude Code transcripts (~/.claude/projects), CLI and IDE surfaces."""
import json
import os
import time

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
            if t == "ai-title":
                s.name = r.get("aiTitle") or s.name
                continue
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
                content = r.get("message", {}).get("content")
                results = [b for b in content if isinstance(b, dict)
                           and b.get("type") == "tool_result"] if isinstance(content, list) else []
                if results:
                    for b in results:
                        s.pending_tools.pop(b.get("tool_use_id"), None)
                else:
                    # a fresh human prompt starts a turn and supersedes any
                    # tools left dangling by an interrupted one
                    s.in_flight = True
                    s.pending_tools.clear()
                s.title = prompt_snippet(content) or s.title
                s.first_prompt = s.first_prompt or prompt_snippet(content)
            else:
                msg = r.get("message", {})
                if r.get("isApiErrorMessage"):
                    s.error_times.append(iso_ts(ts))
                if msg.get("model") and msg["model"] != "<synthetic>":
                    s.model = msg["model"]
                s.branch = r.get("gitBranch") or s.branch
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        s.pending_tools[b.get("id")] = b.get("name", "?")
                if msg.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens", "refusal"):
                    s.in_flight = False
                usage = msg.get("usage") or {}
                used = sum(usage.get(k, 0) or 0 for k in (
                    "input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "output_tokens"))
                if used:
                    s.tokens_used = used
                    s.context_window = cfg["claude_context_window"]
            s.events.append({"type": t, "ts": iso_ts(ts)})
            s.last_activity = max(s.last_activity, iso_ts(ts))
        s.latencies = pair_latencies(s.events)
        # API errors also count as failure strikes
        err_cutoff = time.time() - cfg["network_error_window_s"]
        s.net_error_count = sum(1 for e in s.error_times if e >= err_cutoff)
        s.external_strikes = s.net_error_count
    return list(sessions.values())
