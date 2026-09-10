"""Minimal Telegram Bot API client (stdlib only)."""
import json
import time
import urllib.request
import ssl

import netfix

API_HOST = "api.telegram.org"


class TGError(Exception):
    pass


class TG:
    def __init__(self, token):
        self.token = token
        self.offset = 0
        self.ctx = ssl.create_default_context()

    def api(self, method, **params):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(params).encode()
        last_exc = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=40, context=self.ctx) as r:
                    res = json.loads(r.read().decode())
                if not res.get("ok"):
                    # do not retry logical errors
                    raise TGError(res.get("description", "unknown error"))
                return res.get("result")
            except TGError:
                raise
            except Exception as e:  # noqa: BLE001
                last_exc = e
                netfix.ensure(API_HOST)  # recover from poisoned/broken DNS
                time.sleep(1.5 * (attempt + 1))
        raise TGError(str(last_exc))

    def get_updates(self, timeout=25):
        res = self.api("getUpdates", offset=self.offset, timeout=timeout,
                       allowed_updates=["message", "callback_query"])
        return res or []

    def send(self, chat_id, text, kb=None):
        params = {"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True,
                  "link_preview_options": {"is_disabled": True}}
        if kb:
            params["reply_markup"] = kb
        return self.api("sendMessage", **params)

    def edit(self, chat_id, message_id, text, kb=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text,
                  "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if kb:
            params["reply_markup"] = kb
        try:
            return self.api("editMessageText", **params)
        except TGError as e:
            if "message is not modified" in str(e).lower():
                return None
            if "message to edit not found" in str(e).lower() or \
               "message can't be edited" in str(e).lower():
                return None
            raise

    def answer_callback(self, callback_query_id, text=None):
        try:
            self.api("answerCallbackQuery", callback_query_id=callback_query_id, text=text or "")
        except TGError:
            pass

    def send_chat_action(self, chat_id, action="typing"):
        try:
            self.api("sendChatAction", chat_id=chat_id, action=action)
        except TGError:
            pass
