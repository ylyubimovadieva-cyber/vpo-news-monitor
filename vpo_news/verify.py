"""Правило «підтверджена новина» і перевірка джерел за білим/чорним списками.

Рішення про підтвердження ухвалює цей код, а не модель:
  * новина є на першоджерелі (домен із whitelist.primary_domains), або
  * її повідомили щонайменше два медіа з білого списку різних власників.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .util import domain_matches


@dataclass
class SourceInfo:
    kind: str          # primary | media
    owner: str         # для медіа – власник; для першоджерела – домен


def is_blocked(domain: str, cfg: dict) -> bool:
    return domain_matches(domain, cfg.get("blocked_domains", []))


def classify_domain(domain: str, cfg: dict) -> SourceInfo | None:
    """Повертає тип джерела або None, якщо домену немає в білому списку (чи він заборонений)."""
    if not domain or is_blocked(domain, cfg):
        return None
    wl = cfg.get("whitelist", {})
    # Медіа перевіряємо першими: у них точні домени.
    for m in wl.get("media", []):
        if domain_matches(domain, [m["domain"]]):
            return SourceInfo("media", m.get("owner") or m["domain"])
    for p in wl.get("primary_domains", []):
        if domain_matches(domain, [p]):
            return SourceInfo("primary", domain)
    return None


def has_blocked_attribution(text: str, cfg: dict) -> str | None:
    for pat in cfg.get("blocked_attribution_patterns", []):
        # Лише цілі слова: «ТАРС» не має спрацьовувати на «Шахтарську».
        if re.search(rf"(?<!\w)(?:{pat})(?!\w)", text or "", re.IGNORECASE):
            return pat
    return None


def verify_items(items) -> tuple[bool, dict]:
    """items – рядки з полями kind, owner, url, domain. Повертає (підтверджено, підстава)."""
    primaries = [i for i in items if i["kind"] == "primary"]
    if primaries:
        return True, {"type": "primary", "urls": [primaries[0]["url"]], "domains": [primaries[0]["domain"]]}
    by_owner: dict[str, dict] = {}
    for i in items:
        if i["kind"] == "media":
            by_owner.setdefault(i["owner"] or i["domain"], i)
    if len(by_owner) >= 2:
        chosen = list(by_owner.values())[:3]
        return True, {
            "type": "two_media",
            "urls": [c["url"] for c in chosen],
            "domains": [c["domain"] for c in chosen],
        }
    return False, {"type": "none", "urls": [i["url"] for i in items][:3], "domains": [i["domain"] for i in items][:3]}


def pick_source_url(items) -> str:
    """Посилання для допису: першоджерело, якщо є; інакше найраніша оригінальна стаття медіа."""
    primaries = [i for i in items if i["kind"] == "primary"]
    pool = primaries or list(items)
    pool = sorted(pool, key=lambda i: i["published_at"])
    return pool[0]["url"]


def describe_basis(b: dict) -> str:
    kind = b.get("type")
    domains = ", ".join(b.get("domains") or [])
    if kind == "primary":
        return f"першоджерело – {domains}"
    if kind == "two_media":
        return f"два незалежні медіа – {domains}"
    return "не підтверджено"
