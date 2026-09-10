"""uefa.py — Official UEFA match API client (stdlib only, no auth needed).

Endpoints verified live 2026-09-10 against match.uefa.com:
  GET /v5/matches?competitionId=1&offset=0&limit=30
      -> kickoff-desc list; paginate back for older days. Params are SINGULAR
      'competitionId'; unknown params (dateFrom...) invalidate the whole
      MatchFilter -> 404.
  GET /v5/matches/{id}/events?filter=GOALS&offset=0&limit=60   (also CARDS, VAR)
      -> [{id uuid, type GOAL|YELLOW_CARD|RED_CARD|..., time{minute,second},
          phase FIRST_HALF|SECOND_HALF, primaryActor.person{internationalName,
          clubId}, totalScore{home, away}}]
  Team codes: match.{home,away}Team.translations.displayTeamCode.EN
      (BAY/BOD/MUN/FEN/... — the official reference-channel style codes)
  Statuses: UPCOMING | LIVE | FINISHED

NOTE: secondaryActor on UEFA goals is NOT the assist (verified: Brown 48'
listed keeper Svilar while FotMob/ESPN credit Greenwood) — assists come from
FotMob/ESPN revision edits, never from UEFA.
"""
import datetime
import difflib
import json
import re
import threading
import time
import urllib.error
import urllib.request
import unicodedata

import netfix

HOST = "match.uefa.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"
BASE = f"https://{HOST}/v5"
COMPETITION_ID = 1            # UEFA Champions League

_lock = threading.Lock()
_etag = {}
_body = {}
_list_cache = {"ts": 0.0, "matches": []}

# UEFA short international names -> canonical (accent-stripped alnum) forms
ALIASES = {
    "manutd": "manchesterunited",
    "mancity": "manchestercity",
    "paris": "parissaintgermain",
    "atleti": "atleticomadrid",
    "bdortmund": "borussiadortmund",
    "sbratislava": "slovanbratislava",
    "spcp": "sportingcp",
    "clubbrugge": "clubbrugge",
}


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _get(url, timeout=10):
    """GET json with ETag revalidation; returns cached body on 304."""
    netfix.ensure(HOST)
    with _lock:
        et = _etag.get(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    if et:
        req.add_header("If-None-Match", et)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            new_et = r.headers.get("ETag")
            with _lock:
                if new_et:
                    _etag[url] = new_et
                _body[url] = data
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304:
            with _lock:
                return _body.get(url)
        raise


def _page(offset, limit=30):
    return _get(f"{BASE}/matches?competitionId={COMPETITION_ID}&offset={offset}&limit={limit}") or []


def all_matches(max_pages=8, ttl=60):
    """Fresh kickoff-desc list of UCL matches (cached ttl seconds)."""
    now = time.time()
    with _lock:
        if now - _list_cache["ts"] < ttl and _list_cache["matches"]:
            return _list_cache["matches"]
    out = []
    for off in range(0, max_pages * 30, 30):
        try:
            ms = _page(off)
        except Exception:
            break
        if not ms:
            break
        out.extend(ms)
    with _lock:
        _list_cache["ts"] = now
        _list_cache["matches"] = out
    return out


def _window():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.date().isoformat(), (now + datetime.timedelta(days=1)).date().isoformat()


def window_matches(max_pages=8):
    """Matches kicking off today or tomorrow (list covers the future first)."""
    d0, d1 = _window()
    out = []
    for m in all_matches(max_pages):
        kt = ((m.get("kickOffTime") or {}).get("dateTime") or "")[:10]
        if kt > d1:
            continue
        out.append(m)
        if kt and kt < d0:
            break
    return out


def resolve_match(home_name, away_name, kickoff_iso=None):
    """UEFA match id for an external (ESPN) fixture: fuzzy names + kickoff
    proximity (required) makes false matches practically impossible."""
    cands = window_matches()
    if not cands:
        return None
    h, a = _norm(home_name), _norm(away_name)
    h, a = ALIASES.get(h, h), ALIASES.get(a, a)
    best = None
    for m in cands:
        kt = (m.get("kickOffTime") or {}).get("dateTime") or ""
        if kickoff_iso:
            try:
                t1 = datetime.datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
                t2 = datetime.datetime.fromisoformat(kt.replace("Z", "+00:00"))
                if abs((t1 - t2).total_seconds()) > 5 * 3600:
                    continue
            except ValueError:
                pass
        uh = _norm((m.get("homeTeam") or {}).get("internationalName"))
        ua_ = _norm((m.get("awayTeam") or {}).get("internationalName"))
        uh, ua_ = ALIASES.get(uh, uh), ALIASES.get(ua_, ua_)
        sh = difflib.SequenceMatcher(None, h, uh).ratio()
        sa = difflib.SequenceMatcher(None, a, ua_).ratio()
        if sh >= 0.75 and sa >= 0.75:
            total = sh + sa
            if best is None or total > best[0]:
                best = (total, m)
    return best[1] if best else None


def match_codes(m):
    """{'home': 'BAY', 'away': 'BOD'} — official displayTeamCode."""
    codes = {}
    for side in ("homeTeam", "awayTeam"):
        code = ((((m.get(side) or {}).get("translations") or {}).get("displayTeamCode") or {}).get("EN"))
        codes["home" if side == "homeTeam" else "away"] = code
    return codes


def match_status(m):
    return (m.get("status") or "").upper()


def status_by_id(mid):
    for m in all_matches():
        if str(m.get("id")) == str(mid):
            return match_status(m), m
    return None, None


def club_map(m):
    """{clubId: 'home'|'away'} for event side mapping."""
    return {str((m.get("homeTeam") or {}).get("id")): "home",
            str((m.get("awayTeam") or {}).get("id")): "away"}


def events(mid):
    """GOALS + CARDS for a match (VAR ignored: goal-cancellation is handled
    by ESPN score-drop detection / FotMob DisallowedGoal)."""
    out = []
    for f in ("GOALS", "CARDS"):
        try:
            evs = _get(f"{BASE}/matches/{mid}/events?filter={f}&offset=0&limit=60")
        except Exception:
            continue
        out.extend(evs or [])
    return out


def normalize_event(e, clubs):
    """UEFA event -> bot ke schema. Returns None for unmapped types."""
    t = (e.get("type") or "").upper()
    tm = (e.get("time") or {}).get("minute")
    person = (e.get("primaryActor") or {}).get("person") or {}
    name = person.get("internationalName") or ""
    ke = {"id": f"uefa-{e.get('id')}", "type": "", "minute": f"{tm}'" if tm is not None else "",
          "team": clubs.get(str(person.get("clubId") or "")), "players": [], "text": ""}
    ts = e.get("totalScore") or {}
    if isinstance(ts, dict) and "home" in ts:
        try:
            ke["new_score"] = [int(ts.get("home") or 0), int(ts.get("away") or 0)]
        except (TypeError, ValueError):
            pass
    if t == "GOAL":
        ke["type"] = "goal"
        ke["players"] = [name]
    elif "YELLOW" in t or "RED" in t:
        if "SECOND" in t:
            ke["type"] = "red card"
            ke["text"] = "second yellow"
        elif "RED" in t:
            ke["type"] = "red card"
        else:
            ke["type"] = "yellow card"
        ke["players"] = [name]
    else:
        return None
    return ke
