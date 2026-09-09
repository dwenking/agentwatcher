# agentwatcher

macOS menu bar health monitor for AI coding sessions. Watches the local logs
your AI coding tools already write and shows, per active session: context
usage, latency trend, and "strikes" — the early signs that a conversation has
degraded and a fresh session would be faster.

```
🔴  <- menu bar icon: worst active session
 ├─ 🟢 Claude Code (myproject) "fix the login bug"
 │    context  ▓▓▓▓▓▓▓░░░ 67% (134k/200k)
 │    latency  ▂▅▆▄▃█▆▃▅▄  last 8s, med 6s
 │    strikes  0
 │    active   12s ago
 └─ 🔴 Codex (api-server) "refactor the auth module"
      strikes  3 (errors/aborts)
```

No hooks, no proxies, no per-tool integration: it only reads log files.

## Supported tools

| Provider | Surfaces | Signal |
|---|---|---|
| `claude_code` | Claude Code CLI, Claude Code IDE extension | per-API-call latency vs. session baseline, tokens from `usage` |
| `codex` | Codex CLI, Codex IDE extension | errors/aborts from rollouts + `logs_2.sqlite`, tokens from `token_count` |

Anything else: write a drop-in provider (below).

## Install

```sh
brew install dwenking/tap/agentwatcher
brew services start agentwatcher
```

or with pip:

```sh
pipx install git+https://github.com/dwenking/agentwatcher
agentwatcher   # runs in the foreground; use launchd to run at login
```

To start at login without brew services, copy `com.dwenking.agentwatcher.plist`
to `~/Library/LaunchAgents/` (adjust the binary path) and:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dwenking.agentwatcher.plist
```

## Configuration

First run creates `~/.config/agentwatcher/config.json`:

```jsonc
{
  "poll_seconds": 5,
  "baseline_turns": 5,        // session baseline = median of first N turns
  "strike_ratio": 3.0,        // turn ≥ ratio × baseline -> strike
  "strike_absolute_s": 90,    // turn ≥ this -> strike regardless of baseline
  "strike_window": 5,         // strikes counted over the last N turns
  "alert_strikes": 2,         // red at this many strikes
  "history_hours": 48,        // how far back to scan for sessions
  "max_sessions": 10,         // show at most this many (blocked first, then most recent)
  "blocked_after_s": 120,     // in-flight turn silent this long -> blocked (✋)
  "claude_context_window": 200000,
  "providers": {
    "claude_code": { "enabled": true, "glob": "~/.claude/projects/*/*.jsonl" },
    "codex": {
      "enabled": true,
      "glob": "~/.codex/sessions/*/*/*/rollout-*.jsonl",
      "error_db": "~/.codex/logs_2.sqlite",
      "error_window_s": 300
    }
  }
}
```

Paths and thresholds are yours to change; restart the app to apply.

## Add your own provider

Drop a `.py` file into `~/.config/agentwatcher/providers/`. It needs one
function:

```python
from agentwatcher.core import Session

def scan(state: dict, cfg: dict) -> list[Session]:
    """Called every poll. `state` is a dict kept between calls (put your
    file offsets / caches there). Return all known sessions each time."""
```

Fill in `Session(key, tool, label)` plus whatever you can derive:
`title`, `latencies` (seconds per turn), `external_strikes` (errors),
`tokens_used`, `context_window`, `last_activity` (unix ts), and set
`use_latency_strikes=False` if strikes come from errors instead of latency.

Tip: this interface is small enough that an AI assistant can usually write a
provider for a new tool from one sample of its log format.

## License

MIT
