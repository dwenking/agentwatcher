"""Tests for the pure health logic: baseline, strikes, health color, alert-once."""
import watcher


CFG = {
    "baseline_turns": 5,
    "strike_ratio": 3.0,
    "strike_absolute_s": 90,
    "strike_window": 5,
    "alert_strikes": 2,
}


def _session(latencies, extra_strikes=0):
    s = watcher.Session(key="k", tool="claude-cli", label="proj")
    s.latencies = list(latencies)
    s.external_strikes = extra_strikes
    return s


def test_no_strikes_while_baseline_forming():
    # Fewer samples than baseline_turns: nothing counts as a strike yet.
    s = _session([10, 200, 300])
    assert watcher.strikes(s, CFG) == 0


def test_latency_strike_on_ratio_and_absolute():
    # Baseline median of first 5 = 10s. 35s > 3x baseline -> strike; 95s > 90s absolute -> strike.
    s = _session([10, 10, 10, 10, 10, 35, 12, 95])
    assert watcher.strikes(s, CFG) == 2


def test_strikes_only_counted_in_recent_window():
    # Two old strikes scrolled out of the last-5-turn window; one recent.
    s = _session([10, 10, 10, 10, 10, 99, 99, 10, 10, 10, 10, 99])
    assert watcher.strikes(s, CFG) == 1


def test_health_color():
    assert watcher.health(_session([10] * 5), CFG) == "green"
    assert watcher.health(_session([10] * 5 + [40]), CFG) == "yellow"
    assert watcher.health(_session([10] * 5 + [40, 40]), CFG) == "red"


def test_external_strikes_count():
    # Codex: errors/aborts arrive as external strikes, no latency needed.
    s = _session([], extra_strikes=2)
    assert watcher.health(s, CFG) == "red"


def test_prompt_snippet():
    assert watcher._prompt_snippet("fix the bug\nmore detail") == "fix the bug"
    assert watcher._prompt_snippet([{"type": "text", "text": "hello"}]) == "hello"
    assert watcher._prompt_snippet("<bash-input>ls</bash-input>") == ""
    assert watcher._prompt_snippet("[Request interrupted by user]") == ""
    assert watcher._prompt_snippet("[Image #1] why is this slow") == "why is this slow"
    assert watcher._prompt_snippet("x" * 50) == "x" * 40 + "…"


def test_claude_turn_latencies_from_events():
    # user@0 -> assistant@46 (46s), tool-result user@50 -> assistant@62 (12s).
    # Sidechain and meta records ignored.
    events = [
        {"type": "user", "ts": 0.0},
        {"type": "assistant", "ts": 46.0},
        {"type": "user", "ts": 50.0},
        {"type": "assistant", "ts": 62.0},
        {"type": "user", "ts": 70.0},  # unanswered trailing prompt
    ]
    assert watcher.pair_latencies(events) == [46.0, 12.0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
