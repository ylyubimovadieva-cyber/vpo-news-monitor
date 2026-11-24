"""Публікація на сторінці Facebook через Graph API (/{page-id}/feed)."""
from __future__ import annotations

import httpx


class FacebookError(Exception):
    def __init__(self, code: int | None, message: str, token_invalid: bool = False):
        super().__init__(f"Facebook {code}: {message}")
        self.code, self.token_invalid = code, token_invalid


class FacebookAPI:
    def __init__(self, page_id: str, token: str, version: str = "v23.0", client: httpx.Client | None = None):
        if not page_id or not token:
            raise ValueError("Для Facebook потрібні facebook.page_id у config.yaml і FACEBOOK_PAGE_TOKEN")
        self.page_id, self._token, self.version = page_id, token, version
        self._http = client or httpx.Client(timeout=30)

    def post(self, message: str, link: str) -> str:
        try:
            r = self._http.post(
                f"https://graph.facebook.com/{self.version}/{self.page_id}/feed",
                data={"message": message, "link": link, "access_token": self._token},
            )
        except httpx.HTTPError as exc:
            raise FacebookError(None, type(exc).__name__) from None
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            code = err.get("code")
            # 190 – недійсний або прострочений токен; 10/200 – бракує дозволів
            raise FacebookError(code, err.get("message", "невідома помилка"), token_invalid=code in (190, 102))
        return data["id"]
