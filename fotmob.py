"""fotmob.py — fast FotMob client (stdlib only).

FotMob proved ~70-80s FASTER than 365scores publishing events on the same
live game (2026-09-10 U19 races). Live pages: CDN cache max-age=10s.
Payload ~36KB for matchDetails — cheap to poll every 2-3s.

Match details: https://www.fotmob.com/api/data/matchDetails?matchId=<id>
Events: content.matchFacts.events.events[]
  type: Goal|Card|Substitution|Penalty|MissedPenalty|OwnGoal(?)|DisallowedGoal(?)
  Goal: player{name}, assistStr ('assist by X'), suffix ('Penalty'), time,
        isHome, newScore[hs,as], shotmapEvent{...}, ownGoal
  Card: player{name}, cardDescription, isRed(?)  -> verify at runtime
  Substitution: swap[{name,id},{name,id}] = [OUT?, IN? or IN? OUT?] -> verify
Status: header.status{started, finished, ongoing, cancelled, liveTime{short,long,...},
        halfs{firstHalfStarted,...}}
ID resolution: /api/data/matches?date=YYYYMMDD -> leagues[].matches[] (id,
  home{name}, away{name}, status{utcTime}).
"""
import difflib
import json
import re
import urllib.request
import unicodedata

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

MATCH_URL = "https://www.fotmob.com/api/data/matchDetails?matchId={mid}"
DATE_URL = "https://www.fotmob.com/api/data/matches?date={date}"

_cache = {"etag": {}, "body": {}}


def _req(url, timeout=10):
    """GET with ETag revalidation; returns parsed json or None (304/error)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    et = _cache["etag"].get(url)
    if et:
        req.add_header("If-None-Match", et)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
            body = r.read()
            new_et = r.headers.get("ETag")
            if new_et:
                _cache["etag"][url] = new_et
            if code == 304:
                return None
            data = json.loads(body.decode("utf-8", "replace"))
            _cache["body"][url] = data
            return data
    except Exception:
        # 304 raises HTTPError in urllib -> return cached body (unchanged)
        cached = _cache["body"].get(url)
        return cached


def _fetch(url, timeout=10):
    """GET returning (data, changed: bool)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    et = _cache["etag"].get(url)
    if et:
        req.add_header("If-None-Match", et)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            new_et = r.headers.get("ETag")
            if new_et:
                _cache["etag"][url] = new_et
            data = json.loads(body.decode("utf-8", "replace"))
            changed = data != _cache["body"].get(url)
            _cache["body"][url] = data
            return data, changed
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return _cache["body"].get(url), False
        return None, False
    except Exception:
        return None, False


def match(mid, timeout=10):
    """Full matchDetails dict (uses 304 revalidation). None if unchanged/error."""
    return _req(MATCH_URL.format(mid=mid), timeout)


def match_changed(mid, timeout=10):
    """(data, changed) — changed=True when payload differs from last poll."""
    return _fetch(MATCH_URL.format(mid=mid), timeout)


def day_ids(date_yyyymmdd, timeout=10):
    """{fotmob_match_id: {'home': name, 'away': name, 'utc': iso}} for a date."""
    d = _fetch(DATE_URL.format(date=date_yyyymmdd), timeout)[0] or {}
    out = {}
    for lg in d.get("leagues", []) or []:
        for m in lg.get("matches", []) or []:
            out[m.get("id")] = {
                "home": (m.get("home") or {}).get("name"),
                "away": (m.get("away") or {}).get("name"),
                "utc": (m.get("status") or {}).get("utcTime"),
            }
    return out


def strip_minute(s):
    """'80\\u200e\\u2019\\u200e' -> '80'; '45+2' stays '45+2'."""
    if s is None:
        return ""
    s = re.sub(r"[\u200e\u200f\u200b]", "", str(s))
    s = s.replace("\u2019", "").replace("'", "").strip()
    return s


_day_cache = {"date": None, "ids": None}


def find_ids(home_name, away_name, timeout=10):
    """FotMob match id for today's game matching team names.

    Returns AT MOST ONE id (the best candidate). Youth/II teams never match
    senior targets (and vice versa) — a senior game must never inherit events
    from its U19 twin. Transliteration variants (München/Munich, Bodø/Bodo)
    are matched via difflib ratio on accent-folded names.
    """
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    if _day_cache["date"] != today or not _day_cache["ids"]:
        _day_cache["date"] = today
        _day_cache["ids"] = day_ids(today, timeout)
    ids = _day_cache["ids"] or {}

    def norm(s):
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        return re.sub(r"[^a-z0-9]", "", s.lower())

    youth = re.compile(r"(u19|u21|u23|ii)$")

    def score(target, cand):
        if not target or not cand:
            return 0.0
        # senior <-> youth mismatch is always a wrong match
        if bool(youth.search(target)) != bool(youth.search(cand)):
            return 0.0
        if target == cand:
            return 3.0
        t2, c2 = youth.sub("", target), youth.sub("", cand)
        if t2 and t2 == c2:
            return 2.5
        r = difflib.SequenceMatcher(None, target, cand).ratio()
        if r >= 0.84:                      # transliteration variants
            return 1.5 + r
        if target in cand or cand in target:
            return 1.0
        return 0.0

    h, a = norm(home_name), norm(away_name)
    best = None
    for mid, row in ids.items():
        fh, fa = norm(row.get("home")), norm(row.get("away"))
        if not fh or not fa:
            continue
        sh, sa = score(h, fh), score(a, fa)
        if sh >= 1.0 and sa >= 1.0:
            total = sh + sa
            if best is None or total > best[0] or (total == best[0] and mid < best[1]):
                best = (total, mid)
    return [best[1]] if best and best[0] >= 3.0 else []


# ---------------- event normalization -> bot's ke schema ----------------
def normalize_events(f):
    """Convert FotMob events to bot keyEvents schema:
    {id, type, minute, players: [..], team: 'home'|'away', text}
    players[0] = main player; goal: players[1] = assist if any.
    """
    if not f:
        return []
    evs = (f.get("content", {}).get("matchFacts", {}).get("events", {}) or {}).get("events", []) or []
    out = []
    for i, e in enumerate(evs):
        t = e.get("type")
        minute = strip_minute(e.get("time"))
        pname = (e.get("player") or {}).get("name") or ""
        team = "home" if e.get("isHome") else "away"
        base_id = e.get("eventId") or e.get("reactKey") or f"{t}-{i}"
        ke = {"id": f"fm-{base_id}", "type": t, "minute": minute,
              "team": team, "players": [], "text": ""}
        ns = e.get("newScore")
        if isinstance(ns, (list, tuple)) and len(ns) == 2:
            try:
                ke["new_score"] = [int(ns[0]), int(ns[1])]
            except (TypeError, ValueError):
                pass
        if t == "Goal" or (t == "OwnGoal") or e.get("ownGoal"):
            ke["type"] = "goal" if not (t == "OwnGoal" or e.get("ownGoal")) else "own goal"
            if ke["type"] == "own goal":
                ke["players"] = [pname]
            else:
                players = [pname]
                ast = e.get("assistStr") or ""
                m = re.match(r"assist by (.+)", ast)
                if m:
                    players.append(m.group(1))
                txt = []
                if e.get("suffix"):
                    txt.append(e["suffix"])      # 'Penalty', 'Header'
                if e.get("goalDescription"):
                    txt.append(e["goalDescription"])
                ke["text"] = " - ".join(txt)
                ke["players"] = players
        elif t == "Card":
            card = (e.get("card") or "Yellow")
            if card == "Yellow":
                ke["type"] = "yellow card"
            elif card in ("YellowRed", "SecondYellow"):
                ke["type"] = "red card"
                ke["text"] = "second yellow"
            else:
                ke["type"] = "red card"
            ke["players"] = [pname]
        elif t == "Substitution":
            sw = e.get("swap") or []
            names = [p.get("name") for p in sw if isinstance(p, dict)]
            ke["type"] = "substitution"
            ke["players"] = names            # [IN, OUT] — same order as ESPN
        elif t in ("Penalty", "MissedPenalty"):
            ke["players"] = [pname]
            ke["text"] = "penalty"
            ke["type"] = "penalty - missed" if t == "MissedPenalty" else "penalty - scored"
        elif t == "DisallowedGoal":
            ke["type"] = "goal"
            ke["text"] = "disallowed"
            ke["players"] = [pname]
        else:
            continue
        out.append(ke)
    return out


def status(f):
    h = (f or {}).get("header", {})
    st = h.get("status", {}) or {}
    lt = st.get("liveTime") or {}
    return {
        "started": bool(st.get("started")),
        "finished": bool(st.get("finished")),
        "ongoing": bool(st.get("ongoing")),
        "cancelled": bool(st.get("cancelled")),
        "clock": strip_minute(lt.get("short")),
        "clock_long": lt.get("long"),
    }


def teams(f):
    h = (f or {}).get("header", {})
    ts = h.get("teams", []) or []
    return {
        "home": {"name": ts[0].get("name"), "score": ts[0].get("score"), "id": ts[0].get("id")} if len(ts) > 0 else {},
        "away": {"name": ts[1].get("name"), "score": ts[1].get("score"), "id": ts[1].get("id")} if len(ts) > 1 else {},
    }
