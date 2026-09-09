"""Shared session model, health logic, config, and log-reading helpers."""
import glob
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_config.json")
CONFIG_DIR = os.path.expanduser("~/.config/agentwatcher")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
USER_PROVIDER_DIR = os.path.join(CONFIG_DIR, "providers")

SPARK = "▁▂▃▄▅▆▇█"


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
    tokens_used: int = 0
    context_window: int = 0
    model: str = ""
    branch: str = ""
    in_flight: bool = False
    pending_tools: dict = field(default_factory=dict)  # tool_use_id -> tool name
    net_error_count: int = 0  # recent API/network errors, set fresh each scan
    error_times: list = field(default_factory=list)
    offset: int = 0
    events: list = field(default_factory=list)
    abort_times: list = field(default_factory=list)


def load_config():
    """Defaults overlaid with ~/.config/agentwatcher/config.json (created on first run)."""
    cfg = json.load(open(DEFAULT_CONFIG))
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    user = json.load(open(CONFIG_PATH))
    for k, v in user.items():
        if k == "providers":
            for name, pcfg in v.items():
                cfg["providers"].setdefault(name, {}).update(pcfg)
        else:
            cfg[k] = v
    return cfg


# --- health logic -------------------------------------------------------------

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


# tools that always mean "waiting for a human decision"
INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}


def status(s, cfg, now=None):
    """('blocked'|'network'|'thinking'|'idle', reason).
    blocked = waiting on the human (interactive tool pending, or an in-flight
    turn silent past blocked_after_s — permission prompt or hang);
    network = recent API/stream errors; thinking = turn in flight and moving."""
    now = time.time() if now is None else now
    waiting = [n for n in s.pending_tools.values() if n in INTERACTIVE_TOOLS]
    if waiting:
        return "blocked", f"waiting on {waiting[0]}"
    if s.net_error_count:
        return "network", f"{s.net_error_count} API errors recently"
    if s.in_flight or s.pending_tools:
        gap = now - s.last_activity
        if gap > 3600:  # a turn silent this long is dead (killed session), not waiting
            return "idle", ""
        if gap > cfg["blocked_after_s"]:
            return "blocked", f"stalled {age_str(s.last_activity)} — approval needed?"
        return "thinking", ""
    return "idle", ""


# --- rendering helpers --------------------------------------------------------

def sparkline(values, width=20):
    vals = values[-width:]
    if not vals:
        return ""
    top = max(vals) or 1
    return "".join(SPARK[min(int(v / top * (len(SPARK) - 1) + 0.5), len(SPARK) - 1)] for v in vals)


def context_bar(used, window, width=10):
    if not window:
        return ""
    frac = min(used / window, 1.0)
    filled = int(frac * width + 0.5)
    return f"{'▓' * filled}{'░' * (width - filled)} {frac * 100:.0f}% ({used / 1000:.0f}k/{window / 1000:.0f}k)"


def age_str(ts):
    d = max(0, time.time() - ts)
    if d < 90:
        return f"{d:.0f}s ago"
    if d < 5400:
        return f"{d / 60:.0f}m ago"
    return f"{d / 3600:.1f}h ago"


# --- log-reading helpers ------------------------------------------------------

def iso_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def recent_files(pattern, idle_s):
    cutoff = time.time() - idle_s
    return [p for p in glob.glob(os.path.expanduser(pattern)) if os.path.getmtime(p) >= cutoff]


def read_new_lines(s, path):
    size = os.path.getsize(path)
    if size <= s.offset:
        return []
    with open(path, "rb") as f:
        f.seek(s.offset)
        data = f.read()
    # A writer may flush mid-line between polls; consuming the partial line
    # now would corrupt it (and its UTF-8 tail) once the rest arrives. Only
    # advance past complete lines and keep the remainder for the next scan.
    keep = len(data) if data.endswith(b"\n") else data.rfind(b"\n") + 1
    s.offset += keep
    return data[:keep].decode("utf-8", "replace").splitlines()


def prompt_snippet(content, limit=40):
    """First line of a user prompt, skipping tool results and <command> wrappers."""
    if isinstance(content, list):
        content = next((b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), "")
    if not isinstance(content, str):
        return ""
    # drop leading tags like "[Image #1]"; pure system text ("[Request
    # interrupted by user]", "<bash-input>…") reduces to nothing and is skipped
    text = re.sub(r"^(\[[^\]]*\]\s*)+", "", content.strip())
    if not text or text.startswith("<"):
        return ""
    text = text.splitlines()[0]
    return text[:limit] + ("…" if len(text) > limit else "")
