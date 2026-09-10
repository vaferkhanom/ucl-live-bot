"""UCL Live Bot - real-time Champions League event feed for Telegram groups.

Source: ESPN public API (no key). Publishes once, edits in place:
goal -> assist added later -> same message edited; goal disallowed -> the
goal message is edited to DISALLOWED; scorer corrected -> edited. Muted
matches are never published (owner-controlled anti-spoiler).
"""
import datetime
import hashlib
import html
import re
import threading
import time
import traceback

from config import (BOT_TOKEN, OWNER_ID, LEAGUE_SLUG, POLL_SECONDS,
                    SCOREBOARD_SECONDS, DB_PATH, SHOW_STATS_BLOCK, SHOW_LINEUPS,
                    FANTASY, POINTS_LIST_MAX, KICKOFF_MSG)
from tg import TG, TGError
from store import Store
import espn
import scores365
import fotmob
import formatter as F
import fantasy as FY

TEHRAN = datetime.timezone(datetime.timedelta(seconds=12600))


def esc(s):
    return html.escape(str(s or ""), quote=False)


class MatchState:
    def __init__(self):
        self.seq = 0                 # max numeric keyEvent id seen
        self.statuses = set()        # 'ko','ht','second_half','ft','lineups'
        self.scores = (-1, -1)       # last seen score tuple (-1 = unknown)
        self.sigs = {}               # keid -> signature
        self.goal_msgs = []          # [(keid, side, event_key)]
        self.live = False
        self.next_poll = 0
        self.init = False
        self.disallowed = set()      # event_keys already edited/flagged disallowed
        self.last_summary = None
        self.pre_checks = 0
        self.watch_fp = ""          # 365scores fingerprint (watchdog)
        self.fm_ids = []            # candidate FotMob match ids for this match
        self.fm_seen = set()        # fotmob event keys already handled
        self.fm_sig = {}            # fotmob event key -> sig (revision detect)
        self.seen_ev = {}           # (kind, minute, player) -> event_key (cross-source dedupe)



class Bot:
    def __init__(self):
        self.tg = TG(BOT_TOKEN)
        self.store = Store(DB_PATH)
        self.matches = {}
        self.state = {}
        self.lock = threading.Lock()
        self.owner = OWNER_ID
        self.started = time.time()
        self.last_board = 0
        self._panel_fp = None
        if not self.owner:
            self.owner = int(self.store.kv_get("owner") or 0)

    # ---------------- helpers ----------------
    def is_owner(self, uid):
        return self.owner and int(uid or 0) == int(self.owner)

    def ensure_state(self, mid):
        with self.lock:
            st = self.state.get(mid)
            if st is None:
                st = MatchState()
                self.state[mid] = st
            return st

    def groups(self):
        return self.store.groups()

    # ---------------- admin panel ----------------
    PANEL_MAX_ROWS = 12

    def panel_row(self, m, mutes, pending=None):
        """One panel button: '96' FER v ROM 2-4 90' 🔇/🔴/⏳

        pending = {mid: 'mute'|'unmute'} changes staged but NOT applied yet."""
        h, a = m.get("home") or {}, m.get("away") or {}
        mid = str(m["id"])
        pend = (pending or {}).get(mid)
        muted = "🔇 " if mid in mutes else ""
        if pend == "mute":
            muted = "🕓🔇 "      # staged: will be muted on confirm
        elif pend == "unmute":
            muted = "🕓🔊 "      # staged: will be unmuted on confirm
        if m.get("state") == "in":
            st_emoji = "🔴"
            score = f" {h.get('score', 0)}-{a.get('score', 0)}"
            detail = str(m.get("detail") or "")
            clock = f" {detail}" if detail and detail.upper() not in ("IN PLAY",) else ""

        elif m.get("state") == "post":
            st_emoji = "✅"
            score = f" {h.get('score', 0)}-{a.get('score', 0)}"
            clock = ""
        else:
            st_emoji = "⏳"
            score, clock = "", ""
        when = ""
        try:
            dt = datetime.datetime.fromisoformat((m.get("date") or "").replace("Z", "+00:00"))
            when = (dt + datetime.timedelta(seconds=12600)).strftime("%H:%M")
        except Exception:  # noqa: BLE001
            pass
        label = (f"{st_emoji}{muted} {h.get('abbr')} v {a.get('abbr')}"
                 f"{score}{clock} · {when}")
        if pend == "mute":
            action = "pst unmute"     # second click cancels the staged mute
        elif pend == "unmute":
            action = "pst mute"
        elif mid in mutes:
            action = "pst unmute"
        else:
            action = "pst mute"
        return [{"text": label, "callback_data": f"{action}:{mid}"}]

    def _pend(self):
        """Staged panel changes: {match_id: 'mute'|'unmute'} (in RAM only)."""
        if not hasattr(self, "_pending"):
            self._pending = {}
        return self._pending

    def _panel_kb(self, ms, mutes, pending=None):
        btns = [self.panel_row(m, mutes, pending) for m in ms[: self.PANEL_MAX_ROWS]]
        pend = pending or {}
        rows = [[{"text": "🔇 موت همه", "callback_data": "pst mute:all"},
                 {"text": "🔊 وصل همه", "callback_data": "pst unmute:all"}]]
        if pend:
            rows.insert(0, [{"text": f"✔️ ثبت تغییرات ({len(pend)})", "callback_data": "papply"},
                            {"text": "✖️ لغو همه", "callback_data": "pcancel"}])
        rows.append([{"text": "🔄 رفرش", "callback_data": "prefresh"},
                     {"text": "❌ بستن", "callback_data": "close"}])
        return {"inline_keyboard": btns + rows}

    def _panel_head(self, ms, mutes, pending=None):
        live = sum(1 for m in ms if m.get("state") == "in")
        pend = pending or {}
        head = (f"🎮 <b>پنل ادمین</b> — {len(ms)} بازی"
                f" | 🔴 {live} لایو | 🔇 {len(mutes)} موت\n"
                f"تاریخ: {datetime.datetime.now(TEHRAN).strftime('%Y-%m-%d %H:%M')} تهران\n")
        if pend:
            head += (f"⚠️ <b>{len(pend)} تغییر در انتظار ثبت</b> (🕓)\n"
                     "<i>با ✔️ ثبت تغییرات اعمال میشه — تا قبلش خبری نمیاد!</i>")
        else:
            head += "<i>کلیک = انتخاب | ✔️ ثبت = اعمال</i>"
        return head

    def cmd_panel(self, chat_id):
        """One-tap admin panel: every match = one glass button. Changes are
        STAGED (🕓) until ✔️ ثبت. refresh_board() prunes old matches daily."""
        self.refresh_board()
        with self.lock:
            ms = sorted(self.matches.values(), key=lambda m: m.get("date") or "")
        mutes = self.store.mutes()
        if not ms:
            self.tg.send(chat_id, "🎮 <b>پنل ادمین</b>\n\nالان هیچ بازی‌ای نیست — "
                                  "به‌محض اینکه برنامه چمپیونزلیگ اعلام بشه خودش اینجا میاد.")
            return
        msg = self.tg.send(chat_id, self._panel_head(ms, mutes, self._pend()),
                           kb=self._panel_kb(ms, mutes, self._pend()))
        if msg and msg.get("message_id"):
            self.store.kv_set("panel_msg", f"{chat_id}:{msg['message_id']}")
            self._panel_fp = None   # force next push to re-sync

    # ---------------- scoreboard ----------------
    def refresh_board(self):
        now = datetime.datetime.now(TEHRAN).date()
        d0 = now.strftime("%Y%m%d")
        d1 = (now + datetime.timedelta(days=1)).strftime("%Y%m%d")
        try:
            fx = espn.scoreboard(LEAGUE_SLUG, d0, d1)
        except Exception as e:  # noqa: BLE001
            print("scoreboard fail:", e, flush=True)
            return
        with self.lock:
            fresh = set()
            for m in fx:
                m["_seen"] = time.time()
                fresh.add(m["id"])
                self.matches[m["id"]] = m
            if fx:
                # daily rollover: drop yesterday's/finished matches so the panel
                # only ever shows today+tomorrow; give live games a grace window
                # so their FT message still lands before removal.
                for mid in list(self.matches):
                    if mid in fresh:
                        continue
                    st = self.state.get(mid)
                    if st and st.live and \
                       time.time() - self.matches[mid].get("_seen", 0) < 1200:
                        continue
                    self.store.unmute(mid)   # stale mute rows die with the match
                    self.matches.pop(mid, None)
        self.last_board = time.time()

    def today_rows(self):
        self.refresh_board()
        mutes = self.store.mutes()
        with self.lock:
            ms = sorted(self.matches.values(), key=lambda m: m.get("date") or "")
        return [F.render_today(m, mutes) for m in ms[:20]], mutes

    # ---------------- classification ----------------
    def classify(self, ke):
        t = (ke.get("type") or "").lower()
        text = (ke.get("text") or "").lower()
        # ESPN emits goal variants: 'Goal', 'Goal - Header', 'Goal - Free Kick'...
        # FotMob emits 'OwnGoal' -> 'own goal'. Catch them all.
        if t == "goal" or t.startswith("goal -") or "own goal" in t:
            if "disallowed" in text:
                return "disallowed"
            if "own goal" in text or "own goal" in t:
                return "own_goal"
            return "goal"
        if t == "penalty - scored":
            return "goal"
        if t == "penalty - missed":
            return "pen_missed"
        if t == "penalty - saved":
            return "pen_saved"
        if t in ("penalty", "penalty - conceded", "penalty won"):
            return "pen_awarded"
        if t == "yellow card":
            return "second_yellow" if "second yellow" in text else "yellow"
        if t == "red card":
            return "second_yellow" if "second yellow" in text else "red"
        if t == "substitution":
            return "sub"
        if t == "kickoff":
            return "ko"
        if t in ("halftime", "half time", "half-time"):
            return "ht"
        if "start 2nd half" in t or "second half" in t and "start" in t:
            return "second_half"
        if t in ("end regular time", "full time", "match ends", "game ends") or \
           t.startswith("full time") or t == "end of game":
            return "ft"
        return None

    def event_sig(self, ke):
        blob = "|".join([str(ke.get("id")), ke.get("type") or "",
                         ",".join(ke.get("players") or []), ke.get("minute") or ""])
        return hashlib.md5(blob.encode()).hexdigest()

    # ---------------- per-match polling ----------------
    def poll_match(self, mid):
        try:
            s = espn.summary(LEAGUE_SLUG, mid)
        except Exception:  # noqa: BLE001
            return
        st = self.ensure_state(mid)
        comp = s.get("competitors") or {}
        if not comp.get("home"):
            return
        st.last_summary = s
        with self.lock:
            m = self.matches.get(mid)

        # ---- first sight: snapshot, never replay history ----
        if not st.init:
            st.init = True
            ids = [int(k["id"]) for k in s["keyEvents"] if str(k.get("id", "")).isdigit()]
            st.seq = max(ids) if ids else 0
            for k in s["keyEvents"]:
                st.sigs[str(k["id"])] = self.event_sig(k)
            hs = (comp.get("home") or {}).get("score", 0)
            as_ = (comp.get("away") or {}).get("score", 0)
            st.scores = (hs, as_)
            state = s["status"]["state"]
            detail = (s["status"].get("detail") or "").upper()
            if state == "in":
                st.live = True
                st.statuses.add("ko")
                if "HALF TIME" in detail or detail.startswith("HT"):
                    st.statuses.add("ht")
            elif state == "post":
                st.live = False
                st.statuses.update({"ko", "ht", "second_half", "ft"})
                if SHOW_LINEUPS:
                    st.statuses.add("lineups")
            elif state == "pre" and SHOW_LINEUPS:
                ros = s.get("rosters") or []
                starters = [e for rb in ros for e in rb.get("entries", []) if e.get("starter")]
                if len(starters) >= 22:
                    txt = F.render_lineups(comp, ros)
                    if txt:
                        st.statuses.add("lineups")
                        self.publish(txt, f"{mid}|lineups", mid)
            return

        # ---- new keyEvents ----
        news = sorted([k for k in s["keyEvents"]
                       if str(k.get("id", "")).isdigit() and int(k["id"]) > st.seq],
                      key=lambda k: int(k["id"]))
        for ke in news:
            st.seq = max(st.seq, int(ke["id"]))
            st.sigs[str(ke["id"])] = self.event_sig(ke)
            self.handle_event(mid, comp, ke)

        # ---- revisions of known events (assist added / players fixed) ----
        for ke in s["keyEvents"]:
            kid = str(ke.get("id"))
            if not kid.isdigit():
                continue
            sig = self.event_sig(ke)
            if kid in st.sigs and st.sigs[kid] != sig:
                st.sigs[kid] = sig
                self.handle_revision(mid, comp, ke)

        # ---- status transitions (HT can arrive without keyEvent) ----
        state = s["status"]["state"]
        detail = (s["status"].get("detail") or "").upper()
        at_ht = "HALF TIME" in detail or detail.startswith("HT")
        if state == "in":
            st.live = True
            if "ko" not in st.statuses and KICKOFF_MSG:
                st.statuses.add("ko")
                self.publish(F.render_ko(comp), f"{mid}|status-ko", mid)
            if at_ht and "ht" not in st.statuses:
                st.statuses.add("ht")
                pts = self.points_lines(s) if FANTASY else None
                self.publish(F.render_ht(comp, pts), f"{mid}|status-ht", mid)
            elif not at_ht and "ht" in st.statuses and \
                    "second_half" not in st.statuses and "ko" in st.statuses:
                # ESPN shortDetail never says 'HALF TIME' (it is 'HT'); and the
                # 2nd half may start without a usable keyEvent. After HT, any
                # in-play detail (clock minute) means the 2nd half is running.
                m_int = FY.minute_value(detail)
                if m_int is not None and m_int >= 46:
                    st.statuses.add("second_half")
                    self.publish(F.render_second_half(comp), f"{mid}|status-2h", mid)
        elif state == "post":
            st.live = False
            self.do_ft(mid, comp, s)

        # ---- score change detection: disallowed / corrections ----
        self.check_scores(mid, comp, s)

    def pre_match_check(self, mid):
        try:
            s = espn.summary(LEAGUE_SLUG, mid)
        except Exception:  # noqa: BLE001
            return
        st = self.ensure_state(mid)
        comp = s.get("competitors") or {}
        if not comp.get("home"):
            return
        st.last_summary = s
        if not st.init:
            self.poll_match(mid)
            return
        with self.lock:
            m = self.matches.get(mid)
        if m and m.get("state") == "pre":
            if SHOW_LINEUPS and "lineups" not in st.statuses:
                ros = s.get("rosters") or []
                starters = [e for rb in ros for e in rb.get("entries", []) if e.get("starter")]
                if len(starters) >= 22:
                    txt = F.render_lineups(comp, ros)
                    if txt:
                        st.statuses.add("lineups")
                        self.publish(txt, f"{mid}|lineups", mid)

    # ---------------- events ----------------
    def handle_event(self, mid, comp, ke):
        kind = self.classify(ke)
        st = self.ensure_state(mid)
        if kind is None:
            return
        if kind == "ko":
            if KICKOFF_MSG and "ko" not in st.statuses:
                st.statuses.add("ko")
                self.publish(F.render_ko(comp), f"{mid}|status-ko", mid)
            return
        if kind == "ht":
            if "ht" not in st.statuses:
                st.statuses.add("ht")
                pts = self.points_lines(st.last_summary) if FANTASY else None
                self.publish(F.render_ht(comp, pts), f"{mid}|status-ht", mid)
            return
        if kind == "second_half":
            if "second_half" not in st.statuses:
                st.statuses.add("second_half")
                self.publish(F.render_second_half(comp), f"{mid}|status-2h", mid)
            return
        if kind == "ft":
            self.do_ft(mid, comp, st.last_summary)
            return
        self.publish_event(mid, comp, ke, kind, new=True)

    def handle_revision(self, mid, comp, ke):
        kind = self.classify(ke)
        # re-publish only things we already sent as messages
        ev_key = self.event_key(mid, ke, kind)
        if ev_key and any(g[2] == ev_key for g in self.ensure_state(mid).goal_msgs):
            self.publish_event(mid, comp, ke, kind, new=False)
            return
        stored = self.store.msgs(ev_key or "")
        if stored:
            self.publish_event(mid, comp, ke, kind, new=False)

    def event_key(self, mid, ke, kind):
        kid = str(ke.get("id"))
        prefix = {"goal": "goal", "own_goal": "goal", "disallowed": "dis",
                  "yellow": "card", "red": "card", "second_yellow": "card",
                  "pen_saved": "pen", "pen_missed": "pen", "pen_awarded": "penw",
                  "sub": "sub"}.get(kind)
        return f"{mid}|{prefix}-{kid}" if prefix else None

    def publish_event(self, mid, comp, ke, kind, new=True):
        st = self.ensure_state(mid)
        minute = F.norm_minute(ke.get("minute"))
        players = ke.get("players") or []
        team_id = str(ke.get("team_id") or "")
        emoji_side, side = F.team_side_emoji(comp, team_id) if team_id else (None, None)
        if not side and ke.get("team") in ("home", "away"):
            side = ke.get("team")   # FotMob events carry 'home'/'away' directly
            cid = (comp.get(side) or {}).get("id")
            if cid:
                # map to the ESPN id so renderers resolve color emoji + abbr
                team_id = str(cid)
                emoji_side, _ = F.team_side_emoji(comp, team_id)
        text_l = (ke.get("text") or "").lower()
        # ---- cross-source dedupe: ESPN replays what FotMob already sent ----
        # names differ across sources (Matias/Matías) and stoppage minutes are
        # notated differently (90+2 vs 92): accent-fold the player and allow
        # +/-2 min once stoppage time is in play.
        probe = (kind, self._probe_key(players[0] if players else ""))
        m_int = FY.minute_value(minute)
        if new and kind != "pen_awarded":
            for (k2, p2), (m2, _ek) in st.seen_ev.items():
                if k2 == kind and p2 == probe[1] and \
                        m_int is not None and m2 is not None and \
                        abs(m_int - m2) <= (2 if max(m_int, m2) >= 45 else 0):
                    return

        if kind in ("goal", "own_goal"):
            scorer = F.event_lastname_upper(players[0]) if players else "?"
            assist = F.display_name(players[1]) if len(players) > 1 else None
            is_pen = "penalty" in text_l and "missed" not in text_l
            if not side:  # fallback: guess from text
                for ha in ("home", "away"):
                    nm = (comp.get(ha) or {}).get("name", "") or ""
                    sn = (comp.get(ha) or {}).get("short", "") or ""
                    if nm.lower() in text_l or sn.lower() in text_l:
                        side = ha
                        break
                side = side or "home"
            # FotMob knows the exact post-goal score — never render a stale one
            rcomp = comp
            ns = ke.get("new_score")
            if ns and (comp.get("home") and comp.get("away")):
                rcomp = {k: dict(v) for k, v in comp.items()}
                rcomp["home"]["score"] = int(ns[0])
                rcomp["away"]["score"] = int(ns[1])
            ev_key = self.event_key(mid, ke, kind)
            txt = F.render_goal(rcomp, side, scorer, assist, minute,
                                penalty=is_pen, own_goal=(kind == "own_goal"))
            if new:
                st.seen_ev[probe] = (m_int, ev_key)
                st.goal_msgs.append((str(ke.get("id")), side, ev_key))
                self.publish(txt, ev_key, mid)
            else:
                self.edit_event(ev_key, txt)
            return

        if kind == "disallowed":
            reason = "OFFSIDE" if "offside" in text_l else ("FOUL" if "foul" in text_l else "")
            ev_key = self.event_key(mid, ke, kind)
            if new:
                self.publish(F.render_disallowed(comp, minute, reason), ev_key, mid)
            return

        if kind in ("yellow", "red", "second_yellow"):
            p = F.event_lastname_upper(players[0]) if players else "?"
            ev_key = self.event_key(mid, ke, kind)
            txt = F.render_card(comp, team_id, p, kind, minute)
            if new:
                st.seen_ev[probe] = (m_int, ev_key)
                self.publish(txt, ev_key, mid)
            else:
                self.edit_event(ev_key, txt)
            return

        if kind in ("pen_saved", "pen_missed"):
            p = F.event_lastname_upper(players[0]) if players else "?"
            txt = F.render_penalty_outcome(comp, p, kind, minute)
            self.publish(txt, self.event_key(mid, ke, kind), mid)
            return

        if kind == "pen_awarded":
            p = F.event_lastname_upper(players[0]) if players else "?"
            txt = F.render_penalty_awarded(comp, team_id, p, minute)
            self.publish(txt, self.event_key(mid, ke, kind), mid)
            return

        if kind == "sub":
            pin = pout = "?"
            if len(players) >= 2:
                # ESPN lists participants as [IN, OUT] ('X replaces Y'); FotMob swap is same order
                pin, pout = players[0], players[1]
            txt = F.render_sub(comp, team_id, pin, pout, minute)
            ev_key = self.event_key(mid, ke, kind)
            if new:
                st.seen_ev[probe] = (m_int, ev_key)
                self.publish(txt, ev_key, mid)
            else:
                self.edit_event(ev_key, txt)
            return

    # ---------------- statuses & FT ----------------
    def do_ft(self, mid, comp, s=None):
        st = self.ensure_state(mid)
        if "ft" in st.statuses:
            return
        st.statuses.add("ft")
        st.live = False
        s = s or st.last_summary
        pts = self.points_lines(s) if FANTASY else None
        goal_lines = None
        if s:
            goal_lines = self.goal_lines(s, comp)
        stats = F.render_stats(comp, s.get("team_stats") or []) if (SHOW_STATS_BLOCK and s) else None
        self.publish(F.render_ft(comp, pts, goal_lines, stats), f"{mid}|status-ft", mid)

    def goal_lines(self, s, comp):
        """'12 - O. Dembélé' scorer tallies from final keyEvents."""
        tallies = {}
        for k in s.get("keyEvents") or []:
            kind = self.classify(k)
            if kind != "goal":
                continue
            players = k.get("players") or []
            if players:
                tallies[players[0]] = tallies.get(players[0], 0) + 1
        lines = [f"{n} - {F.fmt_name(p)}" for p, n in sorted(tallies.items(), key=lambda x: -x[1])]
        return lines or None

    def points_lines(self, s):
        if not s:
            return None
        return FY.points_block(s.get("rosters") or [], max_lines=POINTS_LIST_MAX,
                               key_events=s.get("keyEvents")) or None

    # ---------------- disallowed detection ----------------
    def check_scores(self, mid, comp, s):
        st = self.ensure_state(mid)
        hs = (comp.get("home") or {}).get("score", st.scores[0])
        as_ = (comp.get("away") or {}).get("score", st.scores[1])
        ph, pa = st.scores
        if (hs, as_) == (ph, pa):
            return
        st.scores = (hs, as_)
        if ph < 0:
            return
        if hs >= ph and as_ >= pa:
            # score increased elsewhere: refresh goal message score-lines
            for gid, side, ev_key in st.goal_msgs:
                if ev_key in st.disallowed:
                    continue
                # rebuild from summary ke
                ke = next((k for k in (s.get("keyEvents") or []) if str(k.get("id")) == gid), None)
                if not ke:
                    continue
                players = ke.get("players") or []
                scorer = F.event_lastname_upper(players[0]) if players else "?"
                assist = F.display_name(players[1]) if len(players) > 1 else None
                txt = F.render_goal(comp, side, scorer, assist, ke.get("minute") or "",
                                    penalty="penalty" in (ke.get("text") or "").lower())
                self.edit_event(ev_key, txt)
            return

        # drop: edit the most recent goal message of that side to DISALLOWED
        side = "home" if hs < ph else "away"
        cand = [g for g in st.goal_msgs if g[1] == side and g[2] not in st.disallowed]
        reason = ""
        for c in reversed((s.get("commentary") or [])[-20:]):
            t = (c.get("text") or "").lower()
            if "disallowed" in t:
                reason = "OFFSIDE" if "offside" in t else ("FOUL" if "foul" in t else "")
                break
        if cand:
            gid, side2, ev_key = cand[-1]
            ke = next((k for k in (s.get("keyEvents") or []) if str(k.get("id")) == gid), None)
            minute = (ke or {}).get("minute") or ""
            st.disallowed.add(ev_key)
            self.edit_event(ev_key, F.render_disallowed(comp, minute, reason))
        else:
            self.publish(F.render_disallowed(comp, "", reason), f"{mid}|dis-{int(time.time())}", mid)

    # ---------------- publishing ----------------
    def publish(self, text, event_key, match_id):
        if not text:
            return
        if self.store.is_muted(match_id):
            return
        for chat_id, title in self.groups():
            try:
                msg = self.tg.send(chat_id, text)
                self.store.save_msg(event_key, chat_id, msg["message_id"], text)
            except TGError as e:
                print(f"[send fail {chat_id}] {e}", flush=True)

    def edit_event(self, event_key, text):
        if not text:
            return
        for _ek, chat_id, message_id in self.store.msgs(event_key):
            try:
                self.tg.edit(chat_id, message_id, text)
                self.store.update_msg_text(event_key, chat_id, text)
            except TGError as e:
                print(f"[edit fail {chat_id}] {e}", flush=True)

    # ---------------- watchdog (365scores) ----------------
    def _norm(self, s):
        return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()

    def _probe_key(self, s):
        """Accent-folded key for cross-source player dedupe (Matias == Matías)."""
        import unicodedata as _ud
        n = _ud.normalize("NFKD", s or "")
        n = "".join(ch for ch in n if not _ud.combining(ch))
        return re.sub(r"[^a-z0-9 ]", "", n.lower()).strip()

    def watch_tick(self):
        """Cheap 365scores check (~30KB). On any clock/score/status change for a
        match we track, force an immediate ESPN poll of that match. Also fires
        FotMob fast-poll for live matches (main event publisher)."""
        with self.lock:
            any_live = any(m.get("state") == "in" for m in self.matches.values())
        if not any_live:
            return
        try:
            live = scores365.live_ucl()
        except Exception as e:  # noqa: BLE001
            print("watchdog fail:", e, flush=True)
            live = {}
        with self.lock:
            tracked = [(mid, (m.get("home") or {}).get("name"), (m.get("away") or {}).get("name"))
                       for mid, m in self.matches.items()]
        for mid, hname, aname in tracked:
            st = self.ensure_state(mid)
            with self.lock:
                mstate = self.matches.get(mid, {}).get("state")
            if mstate != "in":
                continue
            hn, an = self._norm(hname), self._norm(aname)
            if not hn or not an:
                continue
            row = next((r for r in live.values()
                        if (hn in r["home"] or r["home"] in hn)
                        and (an in r["away"] or r["away"] in an)), None)
            if row:
                fp = scores365.fingerprint(row)
                if fp != st.watch_fp:
                    changed = bool(st.watch_fp)
                    st.watch_fp = fp
                    # score/status change → poll right now; clock-only → within POLL_SECONDS
                    if changed:
                        st.next_poll = 0.0
            # FotMob fast path: publish events from FotMob (~70s faster than 365)
            try:
                self.fm_tick(mid, hname, aname)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    # ---------------- FotMob fast publisher ----------------
    def fm_resolve(self, mid, hname, aname):
        """Find FotMob id(s) matching this match (cached once found)."""
        st = self.ensure_state(mid)
        if st.fm_ids:
            return st.fm_ids
        try:
            ids = fotmob.find_ids(hname, aname)
        except Exception:
            ids = []
        if ids:
            st.fm_ids = ids
        return ids

    def fm_tick(self, mid, hname, aname):
        """Poll FotMob matchDetails; publish new events before ESPN does."""
        st = self.ensure_state(mid)
        for fmid in self.fm_resolve(mid, hname, aname):
            f, changed = fotmob.match_changed(fmid)
            if not f:
                continue
            stt = fotmob.status(f)
            if stt["cancelled"] or (not stt["started"]):
                continue
            kevs = fotmob.normalize_events(f)
            comp = self.espn_comp(mid)
            for ke in kevs:
                key = str(ke.get("id"))
                sig = self.event_sig(ke)
                if key not in st.fm_seen:
                    st.fm_seen.add(key)
                    st.fm_sig[key] = sig
                    kind = self.classify(ke)
                    if kind in ("goal", "own_goal", "yellow", "red", "second_yellow",
                                "pen_missed", "pen_saved", "pen_awarded", "sub",
                                "disallowed"):
                        self.publish_event(mid, comp, ke, kind, new=True)
                elif st.fm_sig.get(key) != sig:
                    st.fm_sig[key] = sig
                    kind = self.classify(ke)
                    if kind:
                        self.handle_revision(mid, comp, ke)

    def espn_comp(self, mid):
        """Last known ESPN competitors dict for this match (for renderers)."""
        st = self.ensure_state(mid)
        s = st.last_summary or {}
        comp = s.get("competitors") or {}
        if not comp.get("home"):
            # minimal fallback so renderers never crash before first ESPN poll
            m = self.matches.get(mid) or {}
            comp = {"home": dict(m.get("home") or {}), "away": dict(m.get("away") or {})}
        return comp


    # ---------------- tracker thread ----------------
    def tracker(self):
        self.refresh_board()
        while True:
            try:
                if time.time() - self.last_board >= SCOREBOARD_SECONDS:
                    self.refresh_board()
                self.watch_tick()
                now = time.time()
                with self.lock:
                    snapshot = [(mid, m.get("state")) for mid, m in self.matches.items()]
                for mid, state in snapshot:
                    st = self.ensure_state(mid)
                    if state == "in":
                        if now >= st.next_poll:
                            st.next_poll = now + POLL_SECONDS
                            self.poll_match(mid)
                    elif state == "pre":
                        # one immediate check + hourly lineup check
                        if st.next_poll <= now and (st.pre_checks < 1 or now - st.next_poll > 0):
                            st.pre_checks += 1
                            st.next_poll = now + 1800
                            self.pre_match_check(mid)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(2)

    # ---------------- commands ----------------
    def cmd_today(self, chat_id):
        rows, _ = self.today_rows()
        if not rows:
            self.tg.send(chat_id, "امروز و فردا بازی چمپیونزلیگی نیست.")
            return
        self.tg.send(chat_id, "📅 <b>UEFA Champions League</b>\n" + "\n".join(rows))

    # ---------------- updates ----------------
    def handle_update(self, u):
        if u.get("message"):
            self.handle_message(u["message"])
        mc = u.get("my_chat_member")
        if mc:
            chat = mc.get("chat", {})
            if chat.get("type") in ("group", "supergroup"):
                new = (mc.get("new_chat_member") or {}).get("status")
                if new in ("member", "administrator"):
                    self.store.upsert_group(chat["id"], chat.get("title") or "")
        cb = u.get("callback_query")
        if cb:
            self.handle_callback(cb)

    def handle_message(self, msg):
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        ctype = chat.get("type")
        uid = (msg.get("from") or {}).get("id")

        if ctype in ("group", "supergroup"):
            self.store.upsert_group(chat_id, chat.get("title") or "")

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        # private first contact records the owner
        if ctype == "private" and self.is_owner(uid) and not self.store.kv_get("owner"):
            self.store.kv_set("owner", str(uid))

        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            self.cmd_today(chat_id)   # only public command now
        elif cmd == "/panel" and ctype == "private" and self.is_owner(uid):
            self.cmd_panel(chat_id)
        elif cmd == "/today":
            self.cmd_today(chat_id)

    def handle_callback(self, cb):
        data = cb.get("data") or ""
        uid = (cb.get("from") or {}).get("id")
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        msg_id = (cb.get("message") or {}).get("message_id")
        if not (chat_id and self.is_owner(uid)):
            self.tg.answer_callback(cb.get("id"), "فقط ادمین")
            return
        if data == "close":
            self.tg.answer_callback(cb.get("id"))
            try:
                self.tg.api("deleteMessage", chat_id=chat_id, message_id=msg_id)
            except TGError:
                pass
            return
        if data.startswith("pst "):
            # STAGE a change (not applied yet): 'pst mute:ID' | 'pst unmute:ID'
            action, mid = data[4:].split(":", 1)
            pend = self._pend()
            if mid == "all":
                with self.lock:
                    ms = [m for m in self.matches.values() if m.get("state") != "post"]
                for m in ms:
                    if action == "mute":
                        if str(m["id"]) not in self.store.mutes():
                            pend[str(m["id"])] = "mute"
                    else:
                        if str(m["id"]) in self.store.mutes() or \
                           pend.get(str(m["id"])) == "mute":
                            pend[str(m["id"])] = "unmute"
                self.tg.answer_callback(cb.get("id"),
                                        f"🕓 {len(ms)} بازی انتخاب شد — با ✔️ ثبت کن")
            else:
                cur = self.store.mutes()
                cur_state = "mute" if mid in cur else (
                    "unmute" if pend.get(mid) == "mute" else None)
                nxt = "mute" if action == "mute" else "unmute"
                if nxt == cur_state or (pend.get(mid) == nxt):
                    pend.pop(mid, None)     # clicking back = cancel staged change
                else:
                    pend[mid] = nxt
                self.tg.answer_callback(cb.get("id"),
                                        f"🕓 انتخاب شد — با ✔️ ثبت ({len(pend)} در صف)")
            self._panel_reedit(chat_id, msg_id)
        elif data == "papply":
            pend = self._pend()
            if not pend:
                self.tg.answer_callback(cb.get("id"), "چیزی برای ثبت نیست")
                return
            cur = self.store.mutes()
            for mid, act in list(pend.items()):
                if act == "mute" and mid not in cur:
                    m = self.matches.get(mid) or {}
                    h, a = m.get("home") or {}, m.get("away") or {}
                    self.store.mute(mid, f"{h.get('abbr')} vs {a.get('abbr')}")
                elif act == "unmute":
                    self.store.unmute(mid)
            n = len(pend)
            pend.clear()
            self._panel_fp = None
            self.tg.answer_callback(cb.get("id"), f"✅ {n} تغییر ثبت شد")
            self._panel_reedit(chat_id, msg_id)
        elif data == "pcancel":
            self._pend().clear()
            self.tg.answer_callback(cb.get("id"), "✖️ تغییرات لغو شد")
            self._panel_reedit(chat_id, msg_id)
        elif data == "prefresh":
            self._panel_reedit(chat_id, msg_id)
            self.tg.answer_callback(cb.get("id"), "🔄 اورات به‌روز شد")

    def _panel_reedit(self, chat_id, msg_id, alert=False):
        """Re-render panel buttons in place (fresh minutes/scores/new day)."""
        try:
            self.refresh_board()
            with self.lock:
                ms = sorted(self.matches.values(), key=lambda m: m.get("date") or "")
            mutes = self.store.mutes()
            if not ms:
                return
            self.tg.edit(chat_id, msg_id,
                         self._panel_head(ms, mutes, self._pend()),
                         kb=self._panel_kb(ms, mutes, self._pend()))
        except TGError:
            pass

    # ---------------- main ----------------
    def panel_loop(self):
        """Live panel: every 60s push fresh minutes/scores into the pinned
        panel; at Tehran midnight the board refresh prunes yesterday and the
        panel re-renders — zero maintenance forever."""
        last_minute_push = 0
        last_day = datetime.datetime.now(TEHRAN).date()
        while True:
            time.sleep(10)
            try:
                now = time.time()
                today = datetime.datetime.now(TEHRAN).date()
                if today != last_day:
                    last_day = today
                    self.refresh_board()
                    self._push_panel(force=True)
                    continue
                if now - last_minute_push >= 60:
                    last_minute_push = now
                    self._push_panel()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def _push_panel(self, force=False):
        pid = self.store.kv_get("panel_msg")
        if not pid:
            return
        chat_id, msg_id = pid.split(":", 1)
        chat_id, msg_id = int(chat_id), int(msg_id)
        # only bump text when something actually moved (or forced: new day)
        with self.lock:
            fp = "|".join(sorted(
                f"{m['id']}:{m.get('state')}:{(m.get('home') or {}).get('score')}:"
                f"{(m.get('away') or {}).get('score')}" for m in self.matches.values()))
        if not force and fp == self._panel_fp:
            return
        self._panel_fp = fp
        self._panel_reedit(chat_id, msg_id)

    def run(self):
        threading.Thread(target=self.tracker, daemon=True).start()
        threading.Thread(target=self.panel_loop, daemon=True).start()
        print("UCL Live Bot started.", flush=True)
        while True:
            try:
                for u in self.tg.get_updates():
                    self.tg.offset = u["update_id"] + 1
                    try:
                        self.handle_update(u)
                    except Exception:  # noqa: BLE001
                        traceback.print_exc()
            except TGError as e:
                print("getUpdates error:", e, flush=True)
                time.sleep(3)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                time.sleep(3)


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN env var is required")
    Bot().run()
