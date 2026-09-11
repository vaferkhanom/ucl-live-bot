"""Reproduction tests for the 2026-09-10 reliability fixes.

Uses REAL payloads: fixtures/ ESPN summaries and live FotMob/ESPN APIs when
reachable. Each test maps to a diagnosed root cause:
  T1  classify goal variants (Goal - Header)          [P1: missing goals]
  T2  goal_lines dead code at FT                      [P1: FT tallies]
  T3  HT detail 'HT' + 2nd-half status fallback       [P1: lifecycle]
  T4  FotMob newScore in fast goal messages           [P2: stale scores]
  T5  FotMob team side -> ESPN id (color + abbr)      [P3: malformed cards]
  T6  find_ids youth-league exclusion                 [P4: U19 cross-publish]
  T7  find_ids transliteration (München/Munich)       [P5: identity]
  T8  accent-insensitive stoppage-tolerant dedupe     [P4: duplicates]
"""
import json
import os
import sys
import time
import tempfile
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("BOT_TOKEN", "test:dummy")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

import bot as botmod
from store import Store
import espn
import fotmob
from bot import Bot
from config import BOT_TOKEN


def _load(eid):
    with open(os.path.join(HERE, "fixtures", f"summary_{eid}.json")) as f:
        return json.load(f)


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, kb=None):
        self.sent.append((chat_id, text))
        return {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text, kb=None):
        self.sent.append((chat_id, "EDIT:" + text))
        return {"message_id": message_id}

    def api(self, method, **params):
        return {}

    def get_updates(self, timeout=25):
        return []

    def answer_callback(self, *a, **k):
        pass


def make_bot():
    b = Bot()
    b.tg = FakeTG()
    b.store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))  # fresh DB per bot
    b.store.upsert_group(12345, "Test Group")
    return b


class TestClassifyGoalVariants(unittest.TestCase):
    def test_t1_header_goal_from_fixture(self):
        data = _load("401915445")  # PSG 6-1 Slovan contains a 'Goal - Header'
        b = make_bot()
        kinds = []
        for k in data.get("keyEvents", []):
            ke = {"id": str(k.get("id")), "type": (k.get("type") or {}).get("text", ""),
                  "minute": (k.get("clock") or {}).get("displayValue", ""),
                  "players": [(p.get("athlete") or {}).get("displayName", "") for p in k.get("participants", [])],
                  "team_id": str((k.get("team") or {}).get("id") or ""), "text": k.get("text", "")}
            kinds.append((ke["type"], b.classify(ke)))
        self.assertIn("goal", [k for _, k in kinds])
        # any real event whose type is goal-ish MUST classify (no silent drops)
        goalish = [k for t, k in kinds if "goal" in t.lower()]
        self.assertNotIn(None, goalish, f"goal-ish event dropped: {goalish}")

    def test_t1_goal_variant_texts(self):
        b = make_bot()
        for t in ("Goal", "Goal - Header", "Goal - Free Kick", "Penalty - Scored"):
            self.assertEqual(b.classify({"type": t, "text": ""}), "goal", t)
        self.assertEqual(b.classify({"type": "Goal - Own Goal", "text": ""}), "own_goal")
        self.assertEqual(b.classify({"type": "Goal", "text": "Disallowed by offside"}), "disallowed")


class TestGoalLines(unittest.TestCase):
    def test_t2_ft_tallies_no_longer_dead(self):
        b = make_bot()
        from tests_replay import summarize
        comp, keys, rosters, tstats = summarize(_load("401915445"))
        s = {"keyEvents": keys}
        lines = b.goal_lines(s, comp)
        self.assertTrue(lines, "goal_lines returned None — dead code not fixed")
        self.assertTrue(any("Dembélé" in l and l.startswith("2") for l in lines),
                        f"expected Dembélé brace in {lines}")


class TestLifecycle(unittest.TestCase):
    def test_t3_second_half_fires_from_status(self):
        b = make_bot()
        mid = "999001"
        b.matches[mid] = {"id": mid, "state": "in", "home": {"id": "1", "abbr": "ABC", "name": "Alpha", "short": "Alpha", "color": "0000FF", "score": 0},
                          "away": {"id": "2", "abbr": "XYZ", "name": "Xray", "short": "Xray", "color": "FF0000", "score": 1}}
        st = b.ensure_state(mid)
        st.init = True
        st.statuses.update({"ko", "ht"})
        st.scores = (0, 1)
        summary = {"competitors": b.matches[mid] and {
            "home": dict(b.matches[mid]["home"]), "away": dict(b.matches[mid]["away"])},
            "status": {"state": "in", "detail": "46'"}, "keyEvents": [], "commentary": [],
            "rosters": [], "team_stats": []}
        orig = espn.summary
        espn.summary = lambda league, event: summary
        try:
            b.poll_match(mid)
        finally:
            espn.summary = orig
        texts = [t for _, t in b.tg.sent]
        self.assertTrue(any("2ND HALF" in t for t in texts), f"2ND HALF never fired: {texts}")

    def test_t3_ht_fires_on_shortDetail_HT(self):
        b = make_bot()
        mid = "999002"
        comp = {"home": {"id": "1", "abbr": "ABC", "name": "Alpha", "short": "Alpha", "color": "0000FF", "score": 0},
                "away": {"id": "2", "abbr": "XYZ", "name": "Xray", "short": "Xray", "color": "FF0000", "score": 1}}
        b.matches[mid] = {"id": mid, "state": "in", "home": comp["home"], "away": comp["away"]}
        st = b.ensure_state(mid)
        st.init = True
        st.statuses.add("ko")
        st.scores = (0, 1)
        summary = {"competitors": comp, "status": {"state": "in", "detail": "HT"},
                   "keyEvents": [], "commentary": [], "rosters": [], "team_stats": []}
        orig = espn.summary
        espn.summary = lambda league, event: summary
        try:
            b.poll_match(mid)
        finally:
            espn.summary = orig
        texts = [t for _, t in b.tg.sent]
        self.assertTrue(any("🏁 HT" in t for t in texts), f"HT never fired on shortDetail 'HT': {texts}")


class TestFotMobFastPath(unittest.TestCase):
    def _comp(self):
        return {"home": {"id": "111", "abbr": "FEN", "name": "Fenerbahce", "short": "Fenerbahce", "color": "000080", "score": 0},
                "away": {"id": "222", "abbr": "ROM", "name": "Roma", "short": "Roma", "color": "8E1F2F", "score": 0}}

    def test_t4_newsCore_used_for_fast_goal(self):
        b = make_bot()
        mid = "999003"
        st = b.ensure_state(mid)
        ke = {"id": "fm-1", "type": "goal", "minute": "39'", "team": "away",
              "players": ["Bryan Cristante", "Donyell Malen"], "text": "", "new_score": [0, 1]}
        b.publish_event(mid, self._comp(), ke, "goal", new=True)
        self.assertTrue(b.tg.sent)
        txt = b.tg.sent[-1][1]
        self.assertIn("0 - [1]", txt, f"fast goal must show post-goal score: {txt!r}")
        self.assertNotIn("0 - 0", txt)

    def test_t5_fotmob_card_resolves_team(self):
        b = make_bot()
        mid = "999004"
        ke = {"id": "fm-2", "type": "yellow card", "minute": "47'", "team": "away",
              "players": ["Devyne Rensch"], "text": ""}
        b.publish_event(mid, self._comp(), ke, "yellow", new=True)
        txt = b.tg.sent[-1][1]
        self.assertIn("🟨", txt)
        self.assertIn("ROM", txt, f"card must carry the team abbr: {txt!r}")
        self.assertNotIn("⚪", txt, f"card must not fall back to white emoji: {txt!r}")

    def test_t8_dedupe_accent_and_stoppage(self):
        b = make_bot()
        mid = "999005"
        fm = {"id": "fm-3", "type": "yellow card", "minute": "45", "team": "home",
              "players": ["Matias Soule"], "text": ""}
        esp = {"id": "777", "type": "yellow card", "minute": "45'+2'", "team_id": "111",
               "players": ["Mat\u00edas Soul\u00e9"], "text": ""}
        b.publish_event(mid, self._comp(), fm, "yellow", new=True)
        n_before = len(b.tg.sent)
        b.publish_event(mid, self._comp(), esp, "yellow", new=True)
        self.assertEqual(len(b.tg.sent), n_before, "accent/stoppage variants must dedupe")
        # regulation-minute distinct events still pass through (well away from
        # the half-time stoppage window where notation differs by source)
        esp2 = dict(esp, id="778", minute="60'")
        b.publish_event(mid, self._comp(), esp2, "yellow", new=True)
        self.assertEqual(len(b.tg.sent), n_before + 1, "distinct minute must not dedupe")


class TestFantasyFixes(unittest.TestCase):
    """2026-09-10: '19 - N. Haykin' + 14-player 0-0 HT block."""
    def _gk(self, saves):
        return {"player": "N. Haikin", "pos": "G", "starter": True, "subbed_in": False,
                "subbed_out": False,
                "stats": {"totalGoals": 0.0, "goalAssists": 0.0, "shotsOnTarget": 0.0,
                          "yellowCards": 0.0, "redCards": 0.0, "ownGoals": 0.0,
                          "goalsConceded": 0.0, "saves": float(saves)}}

    def test_saves_official_rule_no_19pt_gk(self):
        import fantasy as FY
        gk = self._gk(5)
        ht = FY.calc(gk, final=False)
        ft = FY.calc(gk, final=True)
        self.assertEqual(ht, 3, f"HT: 2app + 1pt/3saves, no clean sheet yet: {ht}")
        self.assertEqual(ft, 5, f"FT: 3 + clean sheet 2: {ft}")
        self.assertLess(max(ht, ft), 10, "per-save scoring must never return")

    def test_zero_zero_ht_block_is_empty(self):
        import fantasy as FY
        keys = [{"type": "Kickoff", "minute": "1'", "players": []}]
        rosters = [{"team_id": "1", "entries": [self._gk(3),
                    {"player": "Star", "pos": "F", "starter": True, "subbed_in": False,
                     "subbed_out": False, "stats": {"totalGoals": 0.0, "goalAssists": 0.0,
                     "shotsOnTarget": 2.0, "goalsConceded": 0.0, "saves": 0.0,
                     "yellowCards": 0.0, "redCards": 0.0, "ownGoals": 0.0}}]}]
        self.assertEqual(FY.points_block(rosters, key_events=keys, final=False), [],
                         "0-0 HT must not list appearance-only players")


class TestUefaSource(unittest.TestCase):
    """Official UEFA API lane (endpoints verified live 2026-09-10)."""
    BROWN_GOAL = {  # real captured event: Fenerbahce 1-1 Roma, Brown 48'
        "id": "52e10501-83eb-4e3e-8463-b0b38ad5d607", "type": "GOAL",
        "phase": "SECOND_HALF", "time": {"minute": 48, "second": 42},
        "totalScore": {"home": 1, "away": 1},
        "primaryActor": {"person": {"internationalName": "Archie Brown",
                                    "clubId": "52692"}},
        "secondaryActor": {"person": {"internationalName": "Mile Svilar",
                                      "clubId": "50137"}},
    }

    def test_normalize_goal(self):
        import uefa
        clubs = {"52692": "home", "50137": "away"}
        ke = uefa.normalize_event(self.BROWN_GOAL, clubs)
        self.assertEqual(ke["type"], "goal")
        self.assertEqual(ke["players"], ["Archie Brown"])
        self.assertEqual(ke["team"], "home")
        self.assertEqual(ke["minute"], "48'")
        self.assertEqual(ke["new_score"], [1, 1])
        self.assertTrue(ke["id"].startswith("uefa-"))

    def test_official_codes_not_espn_quirks(self):
        import uefa
        m = {"homeTeam": {"id": "50037", "translations": {"displayTeamCode": {"EN": "BAY"}}},
             "awayTeam": {"id": "59333", "translations": {"displayTeamCode": {"EN": "BOD"}}}}
        codes = uefa.match_codes(m)
        self.assertEqual(codes, {"home": "BAY", "away": "BOD"})
        self.assertNotEqual(codes["home"], "MUN", "ESPN's Bayern abbr must never leak")

    def test_abbr_patched_from_uefa(self):
        b = make_bot()
        mid = "999010"
        b.matches[mid] = {"id": mid, "state": "in",
                          "home": {"id": "111", "abbr": "MUN", "name": "Bayern Munich",
                                   "short": "Bayern", "color": "DC0000", "score": 0},
                          "away": {"id": "222", "abbr": "BODO", "name": "Bodo/Glimt",
                                   "short": "Bodo/Glimt", "color": "FFCD00", "score": 0}}
        b._patch_abbrs(mid, {"home": "BAY", "away": "BOD"})
        comp = b.espn_comp(mid)
        self.assertEqual(comp["home"]["abbr"], "BAY")
        self.assertEqual(b.matches[mid]["home"]["abbr"], "BAY")

    def test_uefa_goal_then_fotmob_assist_merges(self):
        b = make_bot()
        mid = "999011"
        comp = {"home": {"id": "111", "abbr": "FEN", "name": "Fenerbahce", "short": "Fenerbahce",
                         "color": "000080", "score": 0},
                "away": {"id": "222", "abbr": "ROM", "name": "Roma", "short": "Roma",
                         "color": "8E1F2F", "score": 0}}
        uefa_goal = {"id": "uefa-abc", "type": "goal", "minute": "48'", "team": "home",
                     "players": ["Archie Brown"], "text": "", "new_score": [1, 1]}
        b.publish_event(mid, comp, uefa_goal, "goal", new=True)
        n = len(b.tg.sent)
        self.assertEqual(n, 1)
        self.assertNotIn("🅰️", b.tg.sent[-1][1])
        # FotMob catches up WITH the assist -> same message edited, not duplicated
        fm_goal = {"id": "fm-xyz", "type": "goal", "minute": "48'", "team": "home",
                   "players": ["Archie Brown", "Mason Greenwood"], "text": ""}
        b.publish_event(mid, comp, fm_goal, "goal", new=True)
        # FakeTG records edits in .sent too: exactly ONE edit, no new send
        self.assertEqual(len(b.tg.sent), n + 1, "assist must merge into the same message")
        self.assertIn("EDIT:", b.tg.sent[-1][1])
        self.assertIn("🅰️ GREENWOOD", b.tg.sent[-1][1])
        # the merged edit must keep the post-goal score (1-1, home bracketed)
        self.assertIn("FEN [1] - 1 ROM", b.tg.sent[-1][1])

    def test_uefa_live_flips_board_state(self):
        b = make_bot()
        mid = "999012"
        b.matches[mid] = {"id": mid, "state": "pre", "date": "2026-09-10T18:45:00Z",
                          "home": {"id": "111", "abbr": "BAY", "name": "Bayern Munich",
                                   "short": "Bayern", "color": "DC0000", "score": 0},
                          "away": {"id": "222", "abbr": "BOD", "name": "Bodo/Glimt",
                                   "short": "Bodo", "color": "FFCD00", "score": 0}}
        st = b.ensure_state(mid)
        st.uefa_mid = "2049565"
        import uefa
        orig_status, orig_events = uefa.status_by_id, uefa.events
        uefa.status_by_id = lambda mid_: ("LIVE", {"homeTeam": {"id": "50037"},
                                                   "awayTeam": {"id": "59333"}})
        uefa.events = lambda mid_: []
        try:
            b.uefa_tick(mid)
        finally:
            uefa.status_by_id, uefa.events = orig_status, orig_events
        self.assertEqual(b.matches[mid]["state"], "in", "UEFA LIVE must trigger kickoff")
        self.assertEqual(st.next_poll, 0.0, "ESPN lane must poll immediately")


class TestSelfReporting(unittest.TestCase):
    """Bot verifies its own Railway deploy via Telegram — no Railway token."""

    def test_receipt_sent_to_owner(self):
        b = make_bot()
        b._send_receipt()
        self.assertTrue(b.tg.sent, "receipt must be DM'd to OWNER_ID on start")
        txt = b.tg.sent[-1][1]
        self.assertIn("online", txt)
        self.assertIn("commit:", txt)
        self.assertIn("volume:", txt)
        self.assertNotIn("ghp_", txt)  # never leak secrets

    def test_receipt_no_owner_no_crash(self):
        b = make_bot()
        b.owner = 0
        self.assertFalse(b._send_receipt())   # skip silently

    def test_health_marks_and_report(self):
        b = make_bot()
        b._mark("uefa", True)
        b._mark("espn", False, "boom <timeout>")
        txt = b._report(short=False)
        self.assertIn("uefa: ok", txt)
        self.assertIn("espn: ok never", txt)
        self.assertIn("boom &lt;timeout&gt;", txt)   # HTML-escaped, no raw tags
        b._mark("espn", True)   # recovery clears the error field
        self.assertNotIn("err:", b._report().split("espn:")[1].split("\n")[0])

    def test_anomaly_alert_only_when_all_stale_and_live(self):
        b = make_bot()
        mid = "999020"
        b.matches[mid] = {"id": mid, "state": "in",
                          "home": {"id": "1", "abbr": "A", "name": "Alpha", "short": "A", "color": "0000FF", "score": 0},
                          "away": {"id": "2", "abbr": "X", "name": "Xray", "short": "X", "color": "FF0000", "score": 0}}
        old = time.time() - 3600
        for s in ("uefa", "fotmob", "espn"):
            b._mark(s, True)
            b.health[s]["ok"] = old        # force stale
        b.anomaly_check()
        b._stale_since -= 120              # stale window elapsed
        b.anomaly_check()
        self.assertTrue(any("ALL sources stale" in t for _, t in b.tg.sent),
                        "must DM the owner on total source outage")
        n = len(b.tg.sent)
        b.anomaly_check()                  # throttle: no immediate repeat
        self.assertEqual(len(b.tg.sent), n)
        # fresh source clears the alert state
        b._mark("uefa", True)
        b._stale_since = 0.0
        b2_sent = len(b.tg.sent)
        b.anomaly_check()
        self.assertEqual(len(b.tg.sent), b2_sent)

    def test_debug_command_owner_only(self):
        b = make_bot()
        b.handle_message({"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "text": "/debug"})
        self.assertTrue(any("status" in t for _, t in b.tg.sent))
        n = len(b.tg.sent)
        b.handle_message({"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": "/debug"})
        self.assertEqual(len(b.tg.sent), n, "non-owner must get nothing")
        b.handle_message({"from": {"id": 1}, "chat": {"id": -100, "type": "supergroup"}, "text": "/debug"})
        self.assertEqual(len(b.tg.sent), n, "group /debug must be ignored")


class TestFindIds(unittest.IsolatedAsyncioTestCase):
    def _fetch_ok(self):
        try:
            urllib.request.urlopen("https://www.fotmob.com/api/data/matches?date=20260910", timeout=8)
            return True
        except Exception:
            return False

    def test_t6_no_youth_cross_match(self):
        if not self._fetch_ok():
            self.skipTest("FotMob unreachable")
        ids = fotmob.find_ids("PSV Eindhoven", "Shakhtar Donetsk")
        self.assertEqual(len(ids), 1, f"exactly one candidate allowed: {ids}")
        self.assertLess(int(ids[0]), 1000000000, f"U19 phantom leaked: {ids}")
        f, _ = fotmob.match_changed(ids[0])
        teams = fotmob.teams(f)
        self.assertEqual(teams["home"]["name"], "PSV Eindhoven")
        self.assertNotIn("U19", teams["home"]["name"])
        self.assertNotIn("U19", teams["away"]["name"])

    def test_t7_transliteration_bayern(self):
        if not self._fetch_ok():
            self.skipTest("FotMob unreachable")
        ids = fotmob.find_ids("Bayern Munich", "Bodo/Glimt")
        self.assertEqual(ids, [6106240], f"Bayern München match must resolve: {ids}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
