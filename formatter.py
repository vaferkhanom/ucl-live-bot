"""Message formatting - mirrors the reference channel's style."""
import colorsys
import datetime
import re
from config import NAME_OVERRIDES


def _pct(v):
    """ESPN displayValue pass pct like '0.9' or '67.5' -> integer string."""
    try:
        f = float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return None
    if 0 < f <= 1:
        f *= 100
    return str(int(round(f)))


def norm_minute(m):
    """ESPN '90'+1'' -> \"90+1'\" ; \"45'+2'\" -> \"45+2'\"."""
    m = str(m or "").strip()
    if not m:
        return m
    m = m.replace("'", "")            # drop all apostrophes
    m = re.sub(r"(\d+)\+(\d+)", r"\1+\2", m)
    return m + "'" if m and not m.endswith("'") else m


def display_name(name):
    """Override if present, else original ESPN name."""
    return NAME_OVERRIDES.get(name, name)


def short_name(name):
    """Short-name for ⚽/🅰️ lines: override verbatim, else UPPERCASE last name."""
    if name in NAME_OVERRIDES:
        return NAME_OVERRIDES[name]
    parts = (name or "?").split()
    return (parts[-1] if parts else "?").upper()


def event_lastname_upper(name):
    return short_name(display_name(name) if name in NAME_OVERRIDES else name)


def fmt_name(name):
    """'Ousmane Dembélé' -> 'O. Dembélé' ; 'Suleiman Camara' -> 'S. Camara'"""
    n = display_name(name or "?")
    parts = n.split()
    if len(parts) == 1:
        return parts[0]
    return parts[0][0] + ". " + " ".join(parts[1:])


def color_emoji(hexstr, fallback):
    try:
        h = (hexstr or "").strip().lstrip("#")
        if len(h) != 6:
            return fallback
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except Exception:  # noqa: BLE001
        return fallback
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    if ss < 0.18:
        return "⚪" if ll > 0.55 else "⚫"
    deg = hh * 360
    bright = ll > 0.5
    if deg < 20 or deg >= 330:
        return "🔴" if not bright else "🔴"
    if deg < 70:
        return "🟡" if bright else "🟤"
    if deg < 170:
        return "🟢"
    if deg < 260:
        return "🔵" if bright else "🔵"
    return "🟣" if bright else "🟣"


def team_side_emoji(competitors, team_id):
    for ha in ("home", "away"):
        c = competitors.get(ha) or {}
        if str(c.get("id")) == str(team_id):
            return color_emoji(c.get("color"), "🔵" if ha == "home" else "🔴"), ha
    return "⚪", None


def score_line(competitors, bracket_home=False, bracket_away=False, no_score=False):
    """'🔵 PSG [6] - 1 SBR 🔵' ; brackets mark the side whose number just changed."""
    h, a = competitors.get("home") or {}, competitors.get("away") or {}
    he = color_emoji(h.get("color"), "🔵")
    ae = color_emoji(a.get("color"), "🔴")
    hs, as_ = h.get("score", 0), a.get("score", 0)
    if no_score:
        return f"{he} {h.get('abbr', '?')} - {a.get('abbr', '?')} {ae}"
    hs = f"[{hs}]" if bracket_home else str(hs)
    as_ = f"[{as_}]" if bracket_away else str(as_)
    return f"{he} {h.get('abbr', '?')} {hs} - {as_} {a.get('abbr', '?')} {ae}"


def hashtag(competitors):
    h, a = competitors.get("home") or {}, competitors.get("away") or {}
    return f"#{(h.get('abbr') or '').upper()}{(a.get('abbr') or '').upper()}"


def render_goal(comp, side, scorer, assist, minute, penalty=False, own_goal=False):
    he = color_emoji((comp.get("home") or {}).get("color"), "🔵")
    ae = color_emoji((comp.get("away") or {}).get("color"), "🔴")
    lines = []
    suffix = ""
    if penalty:
        suffix = " (PENALTY)"
    elif own_goal:
        suffix = " (OG)"
    lines.append(f"⚽ {scorer}{suffix}")
    if assist:
        lines.append(f"🅰️ {short_name(assist)}")
    lines.append("")
    bh = side == "home"
    ba = side == "away"
    lines.append(f"⌚️ {minute} | {score_line(comp, bracket_home=bh, bracket_away=ba)}")
    lines.append("")
    lines.append(f"{hashtag(comp)} | Live")
    return "\n".join(lines)


def render_disallowed(comp, minute, reason=""):
    head = "❌ GOAL DISALLOWED" + (f" | {reason}" if reason else "")
    return "\n".join([head, "", f"⌚️ {minute} | {score_line(comp, no_score=True)}",
                      "", f"{hashtag(comp)} | Live"])


def render_card(comp, team_id, player, kind, minute):
    side_emoji, _ = team_side_emoji(comp, team_id)
    abbr = ""
    for ha in ("home", "away"):
        c = comp.get(ha) or {}
        if str(c.get("id")) == str(team_id):
            abbr = c.get("abbr", "")
            break
    action = {"yellow": "YELLOW", "red": "RED", "second_yellow": "2ND YELLOW → RED"}[kind]
    icon = "🟨" if kind == "yellow" else "🟥"
    return "\n".join([f"{icon} {player} {action} ({minute}) {side_emoji} {abbr}", "",
                      f"{hashtag(comp)} | Live"])


def render_penalty_outcome(comp, player, kind, minute):
    word = "SAVED" if kind == "pen_saved" else "MISSED"
    icon = "🧤" if kind == "pen_saved" else "❌"
    return "\n".join([f"{icon} PENALTY {word} — {player}", "",
                      f"⌚️ {minute} | {score_line(comp)}", "",
                      f"{hashtag(comp)} | Live"])


def render_penalty_awarded(comp, team_id, player, minute):
    side_emoji, _ = team_side_emoji(comp, team_id)
    abbr = ""
    for ha in ("home", "away"):
        c = comp.get(ha) or {}
        if str(c.get("id")) == str(team_id):
            abbr = c.get("abbr", "")
            break
    return "\n".join([f"🥅 PENALTY FOR {side_emoji} {abbr} {minute}", "",
                      f"{player}", "", f"{hashtag(comp)} | Live"])


def render_sub(comp, team_id, pin, pout, minute):
    side_emoji, _ = team_side_emoji(comp, team_id)
    abbr = ""
    for ha in ("home", "away"):
        c = comp.get(ha) or {}
        if str(c.get("id")) == str(team_id):
            abbr = c.get("abbr", "")
            break
    return "\n".join([f"⬇️ {event_lastname_upper(pout)} OUT ({minute}) {side_emoji} {abbr}",
                      f"⬆️ {event_lastname_upper(pin)} IN", "",
                      f"{hashtag(comp)} | Live"])


def render_ko(comp):
    return "\n".join([f"🟢 KICK-OFF | {score_line(comp)}", "", f"{hashtag(comp)} | Live"])


def render_second_half(comp):
    return "\n".join([f"🟢 2ND HALF | {score_line(comp)}", "", f"{hashtag(comp)} | Live"])


def render_ht(comp, points_lines=None):
    lines = [f"🏁 HT | {score_line(comp)}"]
    if points_lines:
        lines += [""] + points_lines
    lines += ["", f"{hashtag(comp)} | Live"]
    return "\n".join(lines)


def render_ft(comp, points_lines=None, goal_lines=None, stats_text=None):
    lines = [f"🏁 FT | {score_line(comp)}"]
    if points_lines:
        lines += [""] + points_lines
    if goal_lines:
        lines += [""] + goal_lines
    if stats_text:
        lines += ["", stats_text]
    return "\n".join(lines)


STATS_KEYS = [
    ("possessionPct", "Possession", "%"),
    ("totalShots", "Total Shots", ""),
    ("shotsOnTarget", "Shots On Target", ""),
    ("accuratePasses", "Accurate Passes", ""),
    ("wonCorners", "Corners", ""),
    ("saves", "Saves", ""),
    ("foulsCommitted", "Fouls", ""),
    ("blockedShots", "Blocked Shots", ""),
    ("totalTackles", "Tackles", ""),
    ("interceptions", "Interceptions", ""),
    ("effectiveClearance", "Clearances", ""),
    ("offsides", "Offsides", ""),
]


def render_stats(competitors, team_stats):
    by_id = {t["team_id"]: t for t in team_stats}
    home = by_id.get(str((competitors.get("home") or {}).get("id")))
    away = by_id.get(str((competitors.get("away") or {}).get("id")))
    if not home or not away:
        return None
    lines = ["📊 Stats"]
    for key, label, suffix in STATS_KEYS:
        hv = (home.get("stats") or {}).get(key)
        av = (away.get("stats") or {}).get(key)
        if hv is None and av is None:
            continue
        hv, av = hv or "0", av or "0"
        if key == "possessionPct":
            hp, ap = _pct(hv), _pct(av)
            hv = f"{hp}{suffix}" if hp else hv
            av = f"{ap}{suffix}" if ap else av
            lines.append(f"{label}: {hv} - {av}")
            continue
        if key == "accuratePasses":
            hp = _pct((home.get("stats") or {}).get("passPct"))
            ap = _pct((away.get("stats") or {}).get("passPct"))
            hv = f"{hv} ({hp}%)" if hp else hv
            av = f"{av} ({ap}%)" if ap else av
        if suffix:
            import re as _re
            if _re.fullmatch(r"\d+(\.\d+)?", str(hv).strip()):
                hv = f"{hv}{suffix}"
            if _re.fullmatch(r"\d+(\.\d+)?", str(av).strip()):
                av = f"{av}{suffix}"
        lines.append(f"{label}: {hv} - {av}")
    return "\n".join(lines)


def render_lineups(competitors, rosters):
    parts = ["📋 LINEUPS", ""]
    for ha in ("home", "away"):
        comp = competitors.get(ha) or {}
        ros = next((r for r in rosters if str(r.get("team_id")) == str(comp.get("id"))), None)
        if not ros:
            continue
        starters = [e for e in ros.get("entries", []) if e.get("starter")]
        if not starters:
            return None
        emoji = color_emoji(comp.get("color"), "🔵" if ha == "home" else "🔴")
        names = [display_name(e.get("player", "?")) for e in starters]
        parts.append(f"{emoji} {comp.get('short') or comp.get('name')} XI: " + ", ".join(names))
        parts.append("")
    parts.append(f"{hashtag(competitors)} | Live")
    return "\n".join(parts).rstrip()


def render_today(match, mutes, tz_offset=12600):
    """One /today row."""
    he = color_emoji((match.get("home") or {}).get("color"), "🔵")
    ae = color_emoji((match.get("away") or {}).get("color"), "🔴")
    h, a = match.get("home") or {}, match.get("away") or {}
    when = ""
    try:
        dt = datetime.datetime.fromisoformat(match["date"].replace("Z", "+00:00"))
        loc = dt + datetime.timedelta(seconds=tz_offset)
        when = loc.strftime("%H:%M")
    except Exception:  # noqa: BLE001
        pass
    st = {"pre": "⏳", "in": "🔴", "post": "✅"}.get(match.get("state", "pre"), "")
    muted = "🔇" if str(match["id"]) in mutes else ""
    return (f"{st}{muted} {he} {h.get('abbr')} vs {a.get('abbr')} {ae} — {when} Tehran "
            f"(<code>{match['id']}</code>)")
