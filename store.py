"""SQLite persistence: groups, muted matches, sent messages (for edits)."""
import sqlite3
import threading
import time


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._init()

    def _conn(self):
        return sqlite3.connect(self.path, timeout=10)

    def _init(self):
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS groups(
                chat_id INTEGER PRIMARY KEY, title TEXT, added_at INTEGER);
            CREATE TABLE IF NOT EXISTS mutes(
                match_id TEXT PRIMARY KEY, label TEXT, created_at INTEGER);
            CREATE TABLE IF NOT EXISTS msgs(
                event_key TEXT, chat_id INTEGER, message_id INTEGER, text TEXT,
                PRIMARY KEY(event_key, chat_id));
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            """)

    # groups
    def upsert_group(self, chat_id, title):
        with self.lock, self._conn() as c:
            c.execute("INSERT INTO groups(chat_id,title,added_at) VALUES(?,?,?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                      (chat_id, title, int(time.time())))

    def groups(self):
        with self.lock, self._conn() as c:
            return [(r[0], r[1]) for r in c.execute("SELECT chat_id,title FROM groups")]

    # mutes
    def mute(self, match_id, label):
        with self.lock, self._conn() as c:
            c.execute("INSERT OR IGNORE INTO mutes VALUES(?,?,?)",
                      (str(match_id), label, int(time.time())))

    def unmute(self, match_id):
        with self.lock, self._conn() as c:
            c.execute("DELETE FROM mutes WHERE match_id=?", (str(match_id),))

    def mutes(self):
        with self.lock, self._conn() as c:
            return {str(r[0]): r[1] for r in c.execute("SELECT match_id,label FROM mutes")}

    def is_muted(self, match_id):
        with self.lock, self._conn() as c:
            r = c.execute("SELECT 1 FROM mutes WHERE match_id=?", (str(match_id),)).fetchone()
            return bool(r)

    # sent messages
    def save_msg(self, event_key, chat_id, message_id, text):
        with self.lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO msgs VALUES(?,?,?,?)",
                      (event_key, chat_id, message_id, text))

    def msgs(self, event_key):
        with self.lock, self._conn() as c:
            return [(r[0], r[1], r[2]) for r in
                    c.execute("SELECT event_key,chat_id,message_id FROM msgs WHERE event_key=?",
                              (event_key,))]

    def msg_text(self, event_key, chat_id):
        with self.lock, self._conn() as c:
            r = c.execute("SELECT text FROM msgs WHERE event_key=? AND chat_id=?",
                          (event_key, chat_id)).fetchone()
            return r[0] if r else None

    def update_msg_text(self, event_key, chat_id, text):
        with self.lock, self._conn() as c:
            c.execute("UPDATE msgs SET text=? WHERE event_key=? AND chat_id=?",
                      (text, event_key, chat_id))

    def clear_match_msgs(self, match_id):
        with self.lock, self._conn() as c:
            c.execute("DELETE FROM msgs WHERE event_key LIKE ?", (f"{match_id}|%",))

    # kv
    def kv_set(self, k, v):
        with self.lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))

    def kv_get(self, k, default=None):
        with self.lock, self._conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
            return r[0] if r else default
