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
            for m in fx:
                self.matches[m["id"]] = m
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
        if t == "goal":
            if "disallowed" in text:
                return "disallowed"
            if "own goal" in text:
                return "own_goal"
            return "goal"
        if t in ("own goal", "goal - own goal"):
            return "own_goal"
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
        if state == "in":
            st.live = True
            if "ko" not in st.statuses and KICKOFF_MSG:
                st.statuses.add("ko")
                self.publish(F.render_ko(comp), f"{mid}|status-ko", mid)
            if ("HALF TIME" in detail) and "ht" not in st.statuses:
                st.statuses.add("ht")
                pts = self.points_lines(s) if FANTASY else None
                self.publish(F.render_ht(comp, pts), f"{mid}|status-ht", mid)
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
        team_id = ke.get("team_id") or ""
        side, _ = None, None
        emoji_side, side = F.team_side_emoji(comp, team_id)
        text_l = (ke.get("text") or "").lower()

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
            ev_key = self.event_key(mid, ke, kind)
            txt = F.render_goal(comp, side, scorer, assist, minute,
                                penalty=is_pen, own_goal=(kind == "own_goal"))
            if new:
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
                # ESPN lists participants as [IN, OUT] ('X replaces Y')
                pin, pout = players[0], players[1]
            txt = F.render_sub(comp, team_id, pin, pout, minute)
            ev_key = self.event_key(mid, ke, kind)
            if new:
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
            if kind in ("goal", "own_goal", "pen_saved", "pen_missed"):
                continue
            players = k.get("players") or []
            if kind == "goal" and players:
                t = tallies.setdefault(players[0], 0)
                tallies[players[0]] = t + 1
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

    # ---------------- tracker thread ----------------
    def tracker(self):
        self.refresh_board()
        while True:
            try:
                if time.time() - self.last_board >= SCOREBOARD_SECONDS:
                    self.refresh_board()
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
    def cmd_help(self, chat_id):
        self.tg.send(chat_id, (
            "🤖 <b>UCL Live Bot</b>\n\n"
            "پوشش لحظه‌ای همه بازی‌های چمپیونزلیگ:\n"
            "⚽ گل + اسیست (ادیت خودکار) | ❌ گل مردود | 🟨🟥 کارت\n"
            "🥅 پنالتی | 🔄 تعویض | 🏁 HT/FT + امتیاز فانتزی + آمار\n\n"
            "<b>دستورات (فقط ادمین بات):</b>\n"
            "/today — بازی‌های امروز و فردا\n"
            "/mute — قطع نوتیف یک بازی (ضد اسپویل)\n"
            "/unmute — وصل کردن دوباره\n"
            "/mutes — لیست بازی‌های موت‌شده\n"
            "/status — وضعیت ربات\n\n"
            "بات روی بازیِ موت‌شده هیچ پیامی نمی‌فرسته و ادیت هم نمی‌زنه.")
        )

    def cmd_today(self, chat_id):
        rows, _ = self.today_rows()
        if not rows:
            self.tg.send(chat_id, "امروز و فردا بازی چمپیونزلیگی نیست.")
            return
        self.tg.send(chat_id, "📅 <b>UEFA Champions League</b>\n" + "\n".join(rows))

    def cmd_mute(self, chat_id):
        with self.lock:
            ms = sorted(self.matches.values(), key=lambda m: m.get("date") or "")
        mutes = self.store.mutes()
        btns = []
        for m in ms:
            if str(m["id"]) in mutes or m.get("state") == "post":
                continue
            h, a = m.get("home") or {}, m.get("away") or {}
            when = (m.get("date") or "")[11:16]
            btns.append([{"text": f"🔇 {h.get('abbr')} vs {a.get('abbr')} {when} UTC",
                          "callback_data": f"mute:{m['id']}"}])
        if not btns:
            self.tg.send(chat_id, "بازی فعالی برای موت کردن نیست.")
            return
        kb = {"inline_keyboard": btns + [[{"text": "❌ بستن", "callback_data": "close"}]]}
        self.tg.send(chat_id, "کدوم بازی نوتیفش قطع بشه؟", kb=kb)

    def cmd_unmute(self, chat_id):
        mutes = self.store.mutes()
        if not mutes:
            self.tg.send(chat_id, "چیزی موت نشده 🔊")
            return
        btns = [[{"text": f"🔊 {label}", "callback_data": f"unmute:{mid}"}]
                for mid, label in mutes.items()]
        kb = {"inline_keyboard": btns + [[{"text": "❌ بستن", "callback_data": "close"}]]}
        self.tg.send(chat_id, "کدوم بازی وصل بشه؟", kb=kb)

    def cmd_mutes(self, chat_id):
        mutes = self.store.mutes()
        if not mutes:
            self.tg.send(chat_id, "لیست خالیه — همه بازی‌ها فعال‌ان 🔊")
            return
        self.tg.send(chat_id, "🔇 <b>موت‌شده‌ها:</b>\n" +
                     "\n".join(f"• {label} (<code>{mid}</code>)" for mid, label in mutes.items()))

    def cmd_status(self, chat_id):
        with self.lock:
            live = [m for m in self.matches.values() if m.get("state") == "in"]
            total = len(self.matches)
        up = int(time.time() - self.started)
        self.tg.send(chat_id, (
            "🤖 <b>Status</b>\n"
            f"Uptime: {up//3600}h {(up%3600)//60}m\n"
            f"Groups: {len(self.groups())}\n"
            f"Matches: {total} | Live: {len(live)}\n"
            f"Muted: {len(self.store.mutes())}\n"
            f"Scoreboard refresh: {int(time.time()-self.last_board)}s ago"))

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
        owner_cmds = {"/mute", "/unmute", "/mutes", "/status"}
        if cmd in owner_cmds and not self.is_owner(uid):
            self.tg.send(chat_id, "⛔ فقط ادمین بات دسترسی داره.")
            return
        if cmd in ("/start", "/help"):
            self.cmd_help(chat_id)
        elif cmd == "/today":
            self.cmd_today(chat_id)
        elif cmd == "/mute":
            self.cmd_mute(chat_id)
        elif cmd == "/unmute":
            self.cmd_unmute(chat_id)
        elif cmd == "/mutes":
            self.cmd_mutes(chat_id)
        elif cmd == "/status":
            self.cmd_status(chat_id)
        elif cmd == "/ping":
            self.tg.send(chat_id, "pong ✅")

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
        if data.startswith("mute:"):
            mid = data.split(":", 1)[1]
            m = self.matches.get(mid) or {}
            h, a = m.get("home") or {}, m.get("away") or {}
            label = f"{h.get('abbr')} vs {a.get('abbr')}"
            self.store.mute(mid, label)
            self.tg.answer_callback(cb.get("id"), f"🔇 {label} موت شد")
            try:
                kb = ((cb.get("message") or {}).get("reply_markup") or {}).get("inline_keyboard") or []
                new_kb = [row for row in kb if not any(b.get("callback_data") == f"mute:{mid}" for b in row)]
                if not new_kb:
                    self.tg.api("deleteMessage", chat_id=chat_id, message_id=msg_id)
                else:
                    self.tg.api("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
                                reply_markup={"inline_keyboard": new_kb})
            except TGError:
                pass
        elif data.startswith("unmute:"):
            mid = data.split(":", 1)[1]
            self.store.unmute(mid)
            self.tg.answer_callback(cb.get("id"), "🔊 وصل شد")

    # ---------------- main ----------------
    def run(self):
        threading.Thread(target=self.tracker, daemon=True).start()
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
