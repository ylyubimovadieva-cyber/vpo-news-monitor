"""Публікація на сторінці організації в LinkedIn через Posts API (/rest/posts).

Потрібен токен LINKEDIN_ACCESS_TOKEN з дозволом w_organization_social від імені
адміністратора сторінки і ID організації (linkedin.organization_id у config.yaml).
"""
from __future__ import annotations

import re

import httpx

# Символи, які LinkedIn у полі commentary трактує як розмітку («little text format»);
# без екранування частина тексту зникає або допис відхиляється.
_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


def escape_commentary(text: str) -> str:
    return _RESERVED.sub(r"\\\1", text)


class LinkedInError(Exception):
    def __init__(self, code: int | None, message: str, token_invalid: bool = False):
        super().__init__(f"LinkedIn {code}: {message}")
        self.code, self.token_invalid = code, token_invalid


class LinkedInAPI:
    def __init__(self, organization_id: str, token: str, version: str = "202510", client: httpx.Client | None = None):
        if not organization_id or not token:
            raise ValueError("Для LinkedIn потрібні linkedin.organization_id у config.yaml і LINKEDIN_ACCESS_TOKEN")
        self.author = f"urn:li:organization:{organization_id}"
        self._token, self.version = token, str(version)
        self._http = client or httpx.Client(timeout=30)

    def post(self, message: str, link: str) -> str:
        text = message if link in message else f"{message}\n\n{link}"
        body = {
            "author": self.author,
            "commentary": escape_commentary(text),
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "LinkedIn-Version": self.version,
            "X-Restli-Protocol-Version": "2.0.0",
        }
        try:
            r = self._http.post("https://api.linkedin.com/rest/posts", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LinkedInError(None, type(exc).__name__) from None
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "невідома помилка")
            except ValueError:
                msg = "невідома помилка"
            # 401 – недійсний або прострочений токен (живе 60 днів)
            raise LinkedInError(r.status_code, msg, token_invalid=r.status_code == 401)
        return r.headers.get("x-restli-id", "")
