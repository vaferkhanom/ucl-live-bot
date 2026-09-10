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
import tempfile
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ.setdefault("BOT_TOKEN", "test:dummy")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

import bot as botmod
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
