"""Щоденний підсумок для координаторки о 9:00 за Києвом."""
from __future__ import annotations

import logging
from datetime import timedelta

from .config import Settings
from .db import DB
from .telegram import TelegramAPI
from .util import KYIV, iso, kyiv_date, utcnow

log = logging.getLogger(__name__)

LIST_LIMIT = 8


def _short(text: str, n: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build(s: Settings, db: DB) -> str:
    since = iso(utcnow() - timedelta(hours=24))
    one = lambda sql, p=(): db.query(sql, p)[0][0]  # noqa: E731

    found = one("SELECT COUNT(*) FROM items WHERE fetched_at >= ?", (since,))
    relevant_kw = one("SELECT COUNT(*) FROM items WHERE fetched_at >= ? AND status NOT IN ('irrelevant_prefilter','blocked')", (since,))
    drafts_new = one("SELECT COUNT(*) FROM drafts WHERE created_at >= ? AND status != 'doubtful'", (since,))
    awaiting = one("SELECT COUNT(*) FROM drafts WHERE status IN ('to_send','awaiting','editing')")
    queued = one("SELECT COUNT(*) FROM drafts WHERE status = 'approved'")
    tg_pub = one("SELECT COUNT(*) FROM drafts WHERE tg_published_at >= ? AND tg_status IN ('published','dry_published')", (since,))
    fb_pub = one("SELECT COUNT(*) FROM drafts WHERE fb_published_at >= ? AND fb_status IN ('published','dry_published')", (since,))
    li_pub = one("SELECT COUNT(*) FROM drafts WHERE li_published_at >= ? AND li_status IN ('published','dry_published')", (since,))
    rejected = one("SELECT COUNT(*) FROM drafts WHERE decided_at >= ? AND status = 'rejected'", (since,))
    expired = one("SELECT COUNT(*) FROM drafts WHERE decided_at >= ? AND status = 'expired'", (since,))
    waiting = one("SELECT COUNT(*) FROM items WHERE status = 'waiting'")

    unverified = db.query("SELECT title, domain, url FROM items WHERE status = 'unverified' AND status_at >= ? ORDER BY status_at DESC", (since,))
    blocked = db.query("SELECT title, domain, reason FROM items WHERE status = 'blocked' AND status_at >= ?", (since,))
    doubtful = db.query("SELECT id, pillar, confidence, source_url, reason FROM drafts WHERE status = 'doubtful' AND created_at >= ? ORDER BY id", (since,))
    doubtful_items = db.query("SELECT title, url, reason FROM items WHERE status = 'doubtful' AND status_at >= ? "
                              "AND cluster_id NOT IN (SELECT cluster_id FROM drafts)", (since,))

    mode = {"live": "бойовий", "test_group": "тестовий (тестова група)", "log": "тестовий (лише лог)"}[s.publish_mode]
    lines = [
        f"Підсумок за добу на {utcnow().astimezone(KYIV).strftime('%d.%m.%Y %H:%M')}",
        f"Режим: {mode}",
        "",
        f"Знайдено новин: {found}, з них про ВПО за ключовими словами: {relevant_kw}",
        f"Нових чернеток на погодження: {drafts_new}",
        f"Зараз чекають вашого рішення: {awaiting}",
        f"Погоджено і чекають на публікацію (ліміти): {queued}",
        f"Опубліковано: Telegram – {tg_pub}, Facebook – {fb_pub}, LinkedIn – {li_pub}",
        f"Відхилено вами: {rejected}; прострочено: {expired}",
        f"Чекають на друге джерело або на обробку моделлю: {waiting}",
    ]

    lines += ["", f"Відкинуто як непідтверджені ({len(unverified)}):"]
    lines += [f"- {_short(r['title'])} ({r['domain']})" for r in unverified[:LIST_LIMIT]] or ["- немає"]
    if len(unverified) > LIST_LIMIT:
        lines.append(f"- і ще {len(unverified) - LIST_LIMIT}")

    if blocked:
        lines += ["", f"Відкинуто через заборонені джерела ({len(blocked)}):"]
        lines += [f"- {_short(r['title'])} ({r['domain']})" for r in blocked[:LIST_LIMIT]]

    total_doubtful = len(doubtful) + len(doubtful_items)
    lines += ["", f"Сумнівні – не надіслані на погодження ({total_doubtful}):"]
    for r in doubtful[:LIST_LIMIT]:
        lines.append(f"- №{r['id']}, {s.pillars[r['pillar']]['topic_name']}, впевненість {r['confidence']:.2f}: "
                     f"{_short(r['reason'], 140)}\n  {r['source_url']}")
    for r in doubtful_items[: max(0, LIST_LIMIT - len(doubtful))]:
        lines.append(f"- {_short(r['title'])}: {_short(r['reason'], 120)}\n  {r['url']}")
    if total_doubtful == 0:
        lines.append("- немає")
    return "\n".join(lines)[:4000]


def send(s: Settings, db: DB, force: bool = False, api: TelegramAPI | None = None) -> bool:
    now_kyiv = utcnow().astimezone(KYIV)
    hour = int(s.cfg.get("digest", {}).get("hour_kyiv", 9))
    today = kyiv_date()
    if not force:
        if now_kyiv.hour not in (hour, hour + 1):
            log.info("Зараз %02d:00 за Києвом – підсумок надсилається о %02d:00", now_kyiv.hour, hour)
            return False
        if db.get_meta("last_digest") == today:
            log.info("Підсумок за сьогодні вже надіслано")
            return False
    text = build(s, db)
    if api is None:
        if not s.approval_token or not s.coordinator_chat_id:
            log.warning("Немає токена бота або chat_id координаторки – підсумок лише в лог")
            print(text)
            return False
        api = TelegramAPI(s.approval_token)
    api.send_message(s.coordinator_chat_id, text, disable_preview=True)
    db.set_meta("last_digest", today)
    return True
