"""Menu bar app: worst-session traffic light, per-session detail cards."""
import statistics
import time

from agentwatcher import core
from agentwatcher.core import age_str, context_bar, health, sparkline, strikes
from agentwatcher.providers import load_providers

EMOJI = {"green": "\U0001f7e2", "yellow": "\U0001f7e1", "red": "\U0001f534"}
_NOOP = lambda _: None  # menu items without a callback render disabled on macOS


def session_card(rumps, s, cfg):
    h = health(s, cfg)
    title = f" “{s.title}”" if s.title else ""
    item = rumps.MenuItem(f"{EMOJI[h]} {s.tool} ({s.label}){title}", callback=_NOOP)
    lines = []
    if s.context_window:
        lines.append(f"context  {context_bar(s.tokens_used, s.context_window)}")
    if s.latencies:
        med = statistics.median(s.latencies)
        lines.append(f"latency  {sparkline(s.latencies)}  last {s.latencies[-1]:.0f}s, med {med:.0f}s")
    n = strikes(s, cfg)
    cause = "slow turns" if s.use_latency_strikes else "errors/aborts"
    lines.append(f"strikes  {n}" + (f" ({cause})" if n else ""))
    lines.append(f"active   {age_str(s.last_activity)}")
    for line in lines:
        item.add(rumps.MenuItem(line, callback=_NOOP))
    return item


def main():
    import rumps

    cfg = core.load_config()
    providers = load_providers(cfg)
    states = {name: {} for name, _ in providers}
    idle_s = cfg["idle_minutes"] * 60

    app = rumps.App("agentwatcher", title=EMOJI["green"], quit_button="Quit")

    def poll(_):
        found, errors = [], []
        for name, scan in providers:
            try:
                found.extend(scan(states[name], cfg))
            except Exception as e:
                errors.append(f"{name}: {e}")
        cutoff = time.time() - idle_s
        live = sorted(
            (s for s in found if s.last_activity >= cutoff and not s.hidden),
            key=lambda s: -s.last_activity,
        )
        worst = "green"
        items = [rumps.MenuItem(f"⚠️ {e}", callback=_NOOP) for e in errors]
        for s in live:
            worst = max(worst, health(s, cfg), key=["green", "yellow", "red"].index)
            items.append(session_card(rumps, s, cfg))
        app.title = "⚠️" if errors else EMOJI[worst]
        app.menu.clear()
        app.menu = (items or [rumps.MenuItem("no active sessions", callback=_NOOP)]) + [None]

    rumps.Timer(poll, cfg["poll_seconds"]).start()
    app.run()


if __name__ == "__main__":
    main()
