"""Menu bar app: worst-session traffic light, per-session detail cards."""
import statistics
import subprocess
import time

from agentwatcher import core
from agentwatcher.core import (age_str, context_bar, display_name, health,
                               sparkline, status, strikes)
from agentwatcher.providers import load_providers

EMOJI = {"green": "\U0001f7e2", "yellow": "\U0001f7e1", "red": "\U0001f534"}
_NOOP = lambda _: None  # menu items without a callback render disabled on macOS


def _attr_item(rumps, segments, plain):
    """MenuItem from (text, style) segments; falls back to the plain string.
    Styles: label (small dim), dim, value, bold, mono, green/orange/red."""
    item = rumps.MenuItem(plain, callback=_NOOP)
    try:
        from AppKit import (NSColor, NSFont, NSFontAttributeName,
                            NSFontWeightRegular, NSForegroundColorAttributeName,
                            NSMutableAttributedString)
        styles = {
            "label": (NSFont.menuFontOfSize_(11), NSColor.secondaryLabelColor()),
            "dim": (NSFont.menuFontOfSize_(12), NSColor.secondaryLabelColor()),
            "value": (NSFont.menuFontOfSize_(13), NSColor.labelColor()),
            "bold": (NSFont.boldSystemFontOfSize_(13), NSColor.labelColor()),
            "mono": (NSFont.monospacedSystemFontOfSize_weight_(12, NSFontWeightRegular),
                     NSColor.labelColor()),
            "green": (NSFont.boldSystemFontOfSize_(13), NSColor.systemGreenColor()),
            "orange": (NSFont.boldSystemFontOfSize_(13), NSColor.systemOrangeColor()),
            "red": (NSFont.boldSystemFontOfSize_(13), NSColor.systemRedColor()),
        }
        out = NSMutableAttributedString.alloc().init()
        for text, style in segments:
            font, color = styles[style]
            out.appendAttributedString_(
                NSMutableAttributedString.alloc().initWithString_attributes_(
                    text, {NSFontAttributeName: font,
                           NSForegroundColorAttributeName: color}))
        item._menuitem.setAttributedTitle_(out)
    except Exception:
        pass
    return item


def _native_row(item, chip, emoji_prefix, title, status_seg, age):
    """Two-part row: human message, then the agent's status as its own
    segment, then dimmed age. Native NSMenuItemBadge pill (trailing edge,
    macOS 14+) carries tool·project. Best effort; plain text stays on
    failure."""
    try:
        from AppKit import (NSColor, NSFont, NSFontAttributeName,
                            NSForegroundColorAttributeName,
                            NSMenuItemBadge, NSMutableAttributedString)
        item._menuitem.setBadge_(NSMenuItemBadge.alloc().initWithString_(chip))
        out = NSMutableAttributedString.alloc().initWithString_(f"{emoji_prefix}{title}")
        for text, size in ((f"   {status_seg}", 12), (f"  ·  {age}", 11)):
            out.appendAttributedString_(
                NSMutableAttributedString.alloc().initWithString_attributes_(
                    text, {
                        NSFontAttributeName: NSFont.menuFontOfSize_(size),
                        NSForegroundColorAttributeName: NSColor.secondaryLabelColor(),
                    }))
        item._menuitem.setAttributedTitle_(out)
    except Exception:
        pass


def session_card(rumps, s, cfg):
    h = health(s, cfg)
    st, reason = status(s, cfg)
    status_seg = {"blocked": "🙋 waiting for human", "network": "🌐 network issue",
                  "thinking": "🤔 thinking"}.get(st, "✅ finished")
    chip = f"{s.tool} · {s.label}"
    name = display_name(s)
    item = rumps.MenuItem(
        f"{EMOJI[h]} {name}  {status_seg}  [{chip}] · {age_str(s.last_activity)}",
        callback=_NOOP)
    _native_row(item, chip, f"{EMOJI[h]} ", name, status_seg,
                age_str(s.last_activity))
    def add(*segments):
        plain = "".join(t for t, _ in segments)
        item.add(_attr_item(rumps, segments, plain))

    if s.title and s.title != name:
        add(("last message   ", "label"), (f"“{s.title}”", "value"))
    if st != "idle":
        add(("status   ", "label"), (status_seg, "bold"),
            (f" — {reason}" if reason else "", "dim"))
    info = " · ".join(x for x in (
        s.model, s.branch, f"{len(s.latencies)} turns" if s.latencies else "") if x)
    if info:
        add((info, "dim"))
    if s.context_window:
        frac = min(s.tokens_used / s.context_window, 1.0)
        pct_style = "red" if frac >= 0.8 else "orange" if frac >= 0.6 else "green"
        bar, pct, detail = context_bar(s.tokens_used, s.context_window).split(" ", 2)
        add(("context  ", "label"), (bar + " ", "mono"), (pct, pct_style),
            (f" {detail}", "dim"))
    if s.latencies:
        med = statistics.median(s.latencies)
        add(("latency  ", "label"), (sparkline(s.latencies) + "  ", "mono"),
            (f"last {s.latencies[-1]:.0f}s", "bold"), (f" · med {med:.0f}s", "dim"))
    n = strikes(s, cfg)
    cause = "slow turns" if s.use_latency_strikes else "errors/aborts"
    add(("strikes  ", "label"), (str(n), "red" if n else "green"),
        (f" ({cause})" if n else "", "dim"))
    return item


def config_item(rumps, cfg):
    item = rumps.MenuItem("⚙️ Config", callback=_NOOP)
    for k, v in cfg.items():
        if k == "providers":
            enabled = [n for n, p in v.items() if p.get("enabled", True)]
            item.add(rumps.MenuItem(f"providers: {', '.join(enabled)}", callback=_NOOP))
        else:
            item.add(rumps.MenuItem(f"{k}: {v}", callback=_NOOP))
    item.add(rumps.MenuItem(
        "Edit config file… (restart to apply)",
        callback=lambda _: subprocess.call(["open", core.CONFIG_PATH])))
    return item


def main():
    import rumps

    cfg = core.load_config()
    providers = load_providers(cfg)
    states = {name: {} for name, _ in providers}
    history_s = cfg["history_hours"] * 3600

    app = rumps.App("agentwatcher", title=EMOJI["green"], quit_button="Quit")

    def poll(_):
        found, errors = [], []
        for name, scan in providers:
            try:
                found.extend(scan(states[name], cfg))
            except Exception as e:
                errors.append(f"{name}: {e}")
        cutoff = time.time() - history_s
        live = sorted(
            (s for s in found
             if s.last_activity >= cutoff and not s.hidden and display_name(s)),
            key=lambda s: -s.last_activity,
        )[:cfg["max_sessions"]]
        worst = "green"
        blocked = 0
        items = [rumps.MenuItem(f"⚠️ {e}", callback=_NOOP) for e in errors]
        for s in live:
            worst = max(worst, health(s, cfg), key=["green", "yellow", "red"].index)
            blocked += status(s, cfg)[0] == "blocked"
            items.append(session_card(rumps, s, cfg))
        title = "⚠️" if errors else EMOJI[worst]
        app.title = title + (f"🙋{blocked}" if blocked else "")
        app.menu.clear()
        app.menu = (items or [rumps.MenuItem("no active sessions", callback=_NOOP)]) \
            + [None, config_item(rumps, cfg), None]

    rumps.Timer(poll, cfg["poll_seconds"]).start()
    app.run()


if __name__ == "__main__":
    main()
