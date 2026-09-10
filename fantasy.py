"""Fantasy points approximation from ESPN player match stats.

Calibrated against official UCL Fantasy samples (PSG 6-1 Slovan, NAP 0-1 ARS):
  Ødegaard 7 = 2app + 4goal + 1SoT ; Dembélé 16 ; Fabián 7 ; Nuno Mendes 5.
ESPN per-player shot/assist counting can differ ±1 from official Opta data.
"""
import re
from config import POINTS

DEF_POSITIONS = {"D", "CB", "CD", "CD-L", "CD-R", "LB", "RB", "LWB", "RWB",
                 "FB-L", "FB-R", "WB-L", "WB-R", "D-L", "D-R", "D-C"}


def minute_value(minute):
    """'90+1' -> 91, '45+2' -> 47, '65' -> 65, bad -> None"""
    if not minute:
        return None
    m = re.match(r"(\d+)(?:\+(\d+))?", str(minute).strip().replace("'", ""))
    if not m:
        return None
    base = int(m.group(1))
    extra = int(m.group(2) or 0)
    # stoppage time of 1st half counts toward elapsed time for the 60' rule
    return base + extra if base in (45, 90) else base + extra


def sub_minutes(key_events):
    """{player_lower: (on_minute, off_minute)} from substitution events."""
    out = {}
    for ke in key_events or []:
        if (ke.get("type") or "").lower() != "substitution":
            continue
        players = ke.get("players") or []
        if len(players) < 2:
            continue
        mv = minute_value(ke.get("minute"))
        if mv is None:
            continue
        pin, pout = players[0], players[1]
        d = out.setdefault(pin.lower(), [None, None])
        d[0] = mv if d[0] is None else d[0]
        d = out.setdefault(pout.lower(), [None, None])
        d[1] = mv if d[1] is None else d[1]
    return out


def played(entry):
    return bool(entry.get("starter") or entry.get("subbed_in"))


def appearance_points(entry, subs):
    """>=60 min = 2 pts, <60 = 1 pt (UCL rule)."""
    name = (entry.get("player") or "").lower()
    on_m, off_m = subs.get(name, (None, None))
    if entry.get("starter"):
        if off_m is not None and off_m < 60:
            return POINTS["app_less60"]
        return POINTS["app"]
    # substitute
    if on_m is not None and on_m <= 30:
        return POINTS["app"]  # could reach 60+
    return POINTS["app_less60"]


def calc(entry, subs=None):
    subs = subs or {}
    s = entry.get("stats") or {}
    pos = (entry.get("pos") or "").upper()
    gk = pos == "G"
    d = pos in DEF_POSITIONS

    if not played(entry):
        return 0

    pts = appearance_points(entry, subs)
    pts += POINTS["goal"] * int(s.get("totalGoals") or 0)
    pts += POINTS["assist"] * int(s.get("goalAssists") or 0)
    pts += POINTS["sot"] * int(s.get("shotsOnTarget") or 0)
    if gk:
        pts += POINTS["saves"] * int(s.get("saves") or 0)
    if gk or d:
        pts += POINTS["conceded_per2"] * (int(s.get("goalsConceded") or 0) // 2)
    pts += POINTS["yellow"] * int(s.get("yellowCards") or 0)
    pts += POINTS["red"] * int(s.get("redCards") or 0)
    pts += POINTS["own_goal"] * int(s.get("ownGoals") or 0)

    if (entry.get("starter") and not entry.get("subbed_out")
            and int(s.get("goalsConceded") or 0) == 0):
        if gk:
            pts += POINTS["clean_sheet_gk"]
        elif d:
            pts += POINTS["clean_sheet_def"]

    return int(pts)


def goal_contributors(key_events):
    """Set of player names who scored or assisted (reference channel lists them at HT/FT)."""
    out = set()
    for ke in key_events or []:
        t = (ke.get("type") or "").lower()
        text = (ke.get("text") or "").lower()
        if t != "goal" or "disallowed" in text:
            continue
        players = ke.get("players") or []
        for p in players[:2]:
            if p:
                out.add(p)
    return out


def points_block(rosters, max_lines=14, key_events=None):
    """'16 - O. Dembélé' style list of goal contributors, sorted by points desc."""
    subs = sub_minutes(key_events)
    contributors = goal_contributors(key_events)
    rows = []
    for rb in rosters:
        for e in rb.get("entries", []):
            name = e.get("player", "?")
            if contributors and name not in contributors:
                continue
            p = calc(e, subs)
            if p != 0 and played(e):
                rows.append((p, name))
    rows.sort(key=lambda x: (-x[0], x[1]))
    lines = [f"{p} - {fmt_points_name(n)}" for p, n in rows[:max_lines]]
    return lines


def fmt_points_name(name):
    """'Ousmane Dembélé' -> 'O. Dembélé' ; keeps multi-word surnames."""
    parts = (name or "?").split()
    if len(parts) == 1:
        return parts[0]
    return parts[0][0] + ". " + " ".join(parts[1:])
