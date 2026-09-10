"""365scores live-score API — fast lightweight WATCHDOG (no key needed).

One request (~30KB, <1s) returns ALL live football games with current score
and clock. We use it only to detect "something changed in match X" moments,
then immediately poll ESPN (which has the rich events + assists + stats).
365scores events lack assists, so ESPN stays the publishing source.
"""
import json
import time
import urllib.request
import ssl

import netfix

HOST = "webws.365scores.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_CTX = ssl.create_default_context()
Q = "appTypeId=5&langId=1&timezoneName=Etc/GMT&userCountryId=21"
UCL_COMP_ID = 572


def _get(url, timeout=10, retries=1):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            netfix.ensure(HOST)
            time.sleep(0.6 * (i + 1))
    raise last


def live_ucl():
    """{game_id: {home, away, hg, ag, clock, status_group, just_ended}} for live+recent UCL."""
    data = _get(f"https://{HOST}/web/games/current/?{Q}&gamesFilter=Live&sports=1")
    out = {}
    for g in data.get("games", []):
        if g.get("competitionId") != UCL_COMP_ID:
            continue
        hc = g.get("homeCompetitor") or {}
        ac = g.get("awayCompetitor") or {}
        out[g.get("id")] = {
            "home": (hc.get("longName") or hc.get("name") or "").lower(),
            "away": (ac.get("longName") or ac.get("name") or "").lower(),
            "hg": int(hc.get("score") or 0),
            "ag": int(ac.get("score") or 0),
            "clock": str(g.get("gameTimeDisplay") or g.get("gameTime") or ""),
            "status_group": g.get("statusGroup"),
            "just_ended": bool(g.get("justEnded")),
        }
    return out


def _norm(s):
    import re as _re
    return _re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def fingerprint(row):
    """Cheap change-detector: any of these moving = ESPN must be re-polled."""
    return f"{row['status_group']}|{row['clock']}|{row['hg']}-{row['ag']}"
