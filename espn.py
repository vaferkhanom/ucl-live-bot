"""ESPN soccer API client - primary data source (no API key needed)."""
import json
import time
import urllib.request
import ssl

import netfix

ESPN_HOST = "site.api.espn.com"

UA = "curl/8.5.0"
_CTX = ssl.create_default_context()
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"


def _norm_minute(m):
    """ESPN \"90'+1'\" -> \"90+1'\" (reference-channel style)."""
    m = str(m or "").strip()
    if not m:
        return m
    m = m.replace("'", "")
    return m + "'"


def _get(url, timeout=12, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            netfix.ensure(ESPN_HOST)  # recover from poisoned/broken DNS
            time.sleep(0.8 * (i + 1))
    raise last


def scoreboard(league, date_from, date_to):
    """date_from/date_to as YYYYMMDD. Returns normalized fixture list."""
    data = _get(f"{BASE}/{league}/scoreboard?dates={date_from}-{date_to}&limit=100")
    out = []
    for e in data.get("events", []):
        comp = (e.get("competitions") or [{}])[0]
        home = away = None
        for c in comp.get("competitors", []):
            team = c.get("team", {}) or {}
            t = {
                "id": str(c.get("id")),
                "abbr": team.get("abbreviation", "?") or "?",
                "name": team.get("displayName", "?") or "?",
                "short": team.get("shortDisplayName", "?") or "?",
                "color": (team.get("color") or "").lstrip("#"),
                "score": int(str(c.get("score", "0") or "0")),
                "ha": c.get("homeAway"),
            }
            if c.get("homeAway") == "home":
                home = t
            else:
                away = t
        st = e.get("status", {}) or {}
        stt = st.get("type", {}) or {}
        out.append({
            "id": str(e["id"]),
            "date": e.get("date"),
            "name": e.get("shortName") or e.get("name") or "?",
            "home": home,
            "away": away,
            "state": stt.get("state", "pre"),
            "detail": stt.get("shortDetail", ""),
            "period": st.get("period", 0) or 0,
            "completed": bool(stt.get("completed")),
        })
    return out


def summary(league, event_id):
    """Full match payload: score, keyEvents, commentary, rosters, team stats."""
    data = _get(f"{BASE}/{league}/summary?event={event_id}&enable=commentary,roster")
    header = data.get("header", {}) or {}
    comp = (header.get("competitions") or [{}])[0]

    competitors = {}
    for c in comp.get("competitors", []):
        team = c.get("team", {}) or {}
        competitors[c.get("homeAway")] = {
            "id": str(c.get("id")),
            "abbr": team.get("abbreviation", "?") or "?",
            "name": team.get("displayName", "?") or "?",
            "short": team.get("shortDisplayName", "?") or "?",
            "color": (team.get("color") or "").lstrip("#"),
            "score": int(str(c.get("score", "0") or "0")),
        }

    st = comp.get("status", {}) or {}
    stt = st.get("type", {}) or {}
    status = {
        "state": stt.get("state", "pre"),
        "detail": stt.get("shortDetail", "") or stt.get("description", ""),
        "period": st.get("period", 0) or 0,
        "completed": bool(stt.get("completed")),
    }

    keys = []
    for k in data.get("keyEvents", []):
        typ = k.get("type", {}) or {}
        keys.append({
            "id": str(k.get("id")),
            "type": typ.get("text", "") or "",
            "type_id": str(typ.get("id") or ""),
            "minute": _norm_minute((k.get("clock") or {}).get("displayValue", "") or ""),
            "period": k.get("period", {}).get("number", 0) if isinstance(k.get("period"), dict) else (k.get("period") or 0),
            "players": [(p.get("athlete") or {}).get("displayName", "") for p in (k.get("participants") or [])],
            "team_id": str((k.get("team") or {}).get("id") or ""),
            "text": k.get("text", "") or "",
            "scoring": bool(k.get("scoringPlay")),
        })

    comments = []
    for c in data.get("commentary", []):
        comments.append({"seq": c.get("sequence"), "text": c.get("text", "") or ""})

    rosters = []
    for rb in data.get("rosters", []):
        entries = []
        for e in rb.get("roster", []):
            stats = {}
            for s in e.get("stats", []):
                if s.get("name"):
                    stats[s["name"]] = s.get("value") or 0
            entries.append({
                "player": (e.get("athlete") or {}).get("displayName", "?") or "?",
                "pos": ((e.get("position") or {}).get("abbreviation") or ""),
                "starter": bool(e.get("starter")),
                "subbed_in": bool(e.get("subbedIn")),
                "subbed_out": bool(e.get("subbedOut")),
                "stats": stats,
            })
        rosters.append({
            "team_id": str((rb.get("team") or {}).get("id") or ""),
            "team": (rb.get("team") or {}).get("displayName", "?"),
            "entries": entries,
        })

    team_stats = []
    for t in (data.get("boxscore", {}) or {}).get("teams", []):
        team_stats.append({
            "team_id": str((t.get("team") or {}).get("id") or ""),
            "abbr": (t.get("team") or {}).get("abbreviation", "?"),
            "stats": {s.get("name"): s.get("displayValue") for s in (t.get("statistics") or []) if s.get("name")},
        })

    return {
        "competitors": competitors,
        "status": status,
        "keyEvents": keys,
        "commentary": comments,
        "rosters": rosters,
        "team_stats": team_stats,
    }


def parse_score_from_goal_text(text):
    """'Goal! Paris Saint Germain 1, Slovan Bratislava 0.' -> (1, 0) or None."""
    import re
    m = re.search(r"(\d+)\s*,\s*[^,]*?(\d+)\s*\.", text or "")
    if m:
        try:
            return int(m.group(1)), int(m.group(2))
        except ValueError:
            return None
    return None
