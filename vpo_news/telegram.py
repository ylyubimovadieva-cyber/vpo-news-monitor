"""Мінімальний клієнт Telegram Bot API на прямих HTTP-запитах."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, method: str, code: int | None, description: str | None):
        super().__init__(f"Telegram {method}: {code} {description}")
        self.method, self.code, self.description = method, code, description


class TelegramAPI:
    def __init__(self, token: str, client: httpx.Client | None = None):
        if not token:
            raise ValueError("Не задано токен бота Telegram")
        self._token = token
        self._http = client or httpx.Client(timeout=30)

    def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(2):
            try:
                r = self._http.post(f"https://api.telegram.org/bot{self._token}/{method}", json=params)
            except httpx.HTTPError as exc:
                # Не передаємо далі текст винятку: у ньому може бути URL з токеном.
                raise TelegramError(method, None, type(exc).__name__) from None
            try:
                data = r.json()
            except ValueError:
                raise TelegramError(method, r.status_code, "некоректна відповідь") from None
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if data.get("error_code") == 429 and retry and attempt == 0 and retry <= 30:
                time.sleep(retry)
                continue
            raise TelegramError(method, data.get("error_code"), data.get("description"))
        raise TelegramError(method, 429, "забагато запитів")

    # ---- зручні обгортки
    def send_message(self, chat_id, text, reply_markup=None, message_thread_id=None, disable_preview=False):
        return self.call(
            "sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup,
            message_thread_id=message_thread_id or None,
            link_preview_options={"is_disabled": disable_preview},
        )

    def send_photo(self, chat_id, photo: bytes, caption=None, message_thread_id=None):
        """Фото з підписом (до 1024 символів), завантаження файлом."""
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        if message_thread_id:
            data["message_thread_id"] = str(message_thread_id)
        try:
            r = self._http.post(f"https://api.telegram.org/bot{self._token}/sendPhoto", data=data,
                                files={"photo": ("image.png", photo)}, timeout=60)
        except httpx.HTTPError as exc:
            raise TelegramError("sendPhoto", None, type(exc).__name__) from None
        try:
            res = r.json()
        except ValueError:
            raise TelegramError("sendPhoto", r.status_code, "некоректна відповідь") from None
        if not res.get("ok"):
            raise TelegramError("sendPhoto", res.get("error_code"), res.get("description"))
        return res["result"]

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         reply_markup=reply_markup, link_preview_options={"is_disabled": True})

    def answer_callback(self, callback_id, text=None):
        return self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=allowed_updates)
