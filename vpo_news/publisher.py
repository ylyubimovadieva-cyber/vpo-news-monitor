"""Публікація погоджених дописів з урахуванням денних лімітів.

Публікується ЛИШЕ те, що координаторка погодила кнопкою. Бойові канали
використовуються тільки коли DRY_RUN=false і LIVE_CONFIRMED=true; інакше –
тестова група або лише лог.
"""
from __future__ import annotations

import logging

from .config import Settings
from .db import DB
from .facebook import FacebookAPI, FacebookError
from .linkedin import LinkedInAPI, LinkedInError
from .telegram import TelegramAPI, TelegramError
from datetime import timedelta

from .util import iso, kyiv_date, parse_iso, utcnow

log = logging.getLogger(__name__)

PUBLISHED = ("published", "dry_published")


def _statuses(live: bool) -> str:
    # У бойовому режимі тестові публікації не рахуються ні в ліміти, ні в інтервал.
    return "('published')" if live else "('published','dry_published')"


def _count_today(db: DB, column_status: str, column_at: str, pillar: str | None = None, live: bool = False) -> int:
    today = kyiv_date()
    sql = f"SELECT {column_at} AS at, pillar FROM drafts WHERE {column_status} IN {_statuses(live)}"
    rows = db.query(sql)
    return sum(1 for r in rows if r["at"] and kyiv_date(parse_iso(r["at"])) == today and (pillar is None or r["pillar"] == pillar))


def _last_tg_publication(db: DB, live: bool):
    row = db.query(f"SELECT MAX(tg_published_at) AS at FROM drafts WHERE tg_status IN {_statuses(live)}")
    return parse_iso(row[0]["at"]) if row and row[0]["at"] else None


def _notify(s: Settings, notify_api: TelegramAPI | None, text: str) -> None:
    if notify_api and s.coordinator_chat_id:
        try:
            notify_api.send_message(s.coordinator_chat_id, text, disable_preview=True)
        except TelegramError:
            pass


def _publish_telegram(s: Settings, api: TelegramAPI | None, d) -> tuple[str, int | None]:
    tg = s.cfg.get("telegram", {})
    mode = s.publish_mode
    if mode == "live":
        chat, thread = tg.get("group_chat_id"), (tg.get("topics") or {}).get(d["pillar"])
        if not chat or not thread:
            raise TelegramError("sendMessage", None, "не задано group_chat_id або message_thread_id теми в config.yaml")
        msg = api.send_message(chat, d["text"], message_thread_id=thread)
        return "published", msg["message_id"]
    if mode == "test_group":
        thread = (tg.get("test_topics") or {}).get(d["pillar"]) or None
        msg = api.send_message(s.test_group_chat_id, "[ТЕСТ] " + d["text"], message_thread_id=thread)
        return "dry_published", msg["message_id"]
    log.info("[DRY_RUN] Допис №%s (стовп %s, %d симв.) було б опубліковано в Telegram", d["id"], d["pillar"], len(d["text"]))
    return "dry_published", None


def _publish_facebook(s: Settings, fb: FacebookAPI | None, d) -> tuple[str, str | None]:
    if s.publish_mode != "live":
        log.info("[DRY_RUN] Допис №%s було б опубліковано на Facebook", d["id"])
        return "dry_published", None
    return "published", fb.post(d["text"], d["source_url"])


def _publish_linkedin(s: Settings, li: LinkedInAPI | None, d) -> tuple[str, str | None]:
    if s.publish_mode != "live":
        log.info("[DRY_RUN] Допис №%s було б опубліковано в LinkedIn", d["id"])
        return "dry_published", None
    return "published", li.post(d["text"], d["source_url"])


def run_queue(s: Settings, db: DB, tg_api: TelegramAPI | None = None, fb_api: FacebookAPI | None = None,
              notify_api: TelegramAPI | None = None, li_api: LinkedInAPI | None = None) -> dict:
    stats = {"tg": 0, "fb": 0, "li": 0, "deferred": 0}
    tg_limit = int(s.cfg.get("telegram", {}).get("daily_limit_per_topic", 3))
    fb_limit = int(s.cfg.get("facebook", {}).get("daily_limit", 2))
    li_limit = int(s.cfg.get("linkedin", {}).get("daily_limit", 2))
    mode = s.publish_mode

    if tg_api is None and mode != "log" and s.publish_token:
        tg_api = TelegramAPI(s.publish_token)
    if notify_api is None and s.approval_token:
        notify_api = TelegramAPI(s.approval_token)
    if fb_api is None and mode == "live" and s.facebook_enabled and s.facebook_token:
        fb = s.cfg["facebook"]
        fb_api = FacebookAPI(str(fb.get("page_id") or ""), s.facebook_token, fb.get("graph_api_version", "v23.0"))
    li_cfg = s.cfg.get("linkedin", {})
    if li_api is None and mode == "live" and s.linkedin_enabled and s.linkedin_token and li_cfg.get("organization_id"):
        li_api = LinkedInAPI(str(li_cfg["organization_id"]), s.linkedin_token, li_cfg.get("api_version", "202510"))
    tag = "" if mode == "live" else "[DRY_RUN] "
    live = mode == "live"
    # Кілька погоджених підряд виходять не разом, а з інтервалом (за замовчуванням година).
    interval = timedelta(minutes=float(s.cfg.get("telegram", {}).get("min_interval_minutes", 60)))
    last_tg = _last_tg_publication(db, live)

    queue = sorted(db.drafts_by_status("approved"), key=lambda d: d["decided_at"] or "")
    for d in queue:
        # Можливість, у якої дедлайн минув, поки допис чекав у черзі, не публікуємо.
        if d["deadline"] and d["deadline"] < kyiv_date():
            db.update_draft(d["id"], status="expired", error="дедлайн можливості минув до публікації")
            _notify(s, notify_api, f"Допис №{d['id']} не опубліковано: дедлайн {d['deadline']} уже минув.")
            continue
        # ---- Telegram
        if d["tg_status"] == "queued":
            if _count_today(db, "tg_status", "tg_published_at", d["pillar"], live) >= tg_limit:
                stats["deferred"] += 1
            elif last_tg and interval and utcnow() - last_tg < interval:
                stats["deferred"] += 1  # чекає, поки мине інтервал від попереднього допису
            elif mode != "log" and tg_api is None:
                log.warning("Немає токена бота для публікації – допис №%s чекає", d["id"])
            else:
                try:
                    status, mid = _publish_telegram(s, tg_api, d)
                    db.update_draft(d["id"], tg_status=status, tg_message_id=mid, tg_published_at=iso(utcnow()))
                    last_tg = utcnow()
                    stats["tg"] += 1
                    _notify(s, notify_api, f"{tag}Допис №{d['id']} опубліковано в Telegram (тема {s.pillars[d['pillar']]['topic_name']}).")
                except TelegramError as exc:
                    log.warning("Telegram: допис №%s не опубліковано: %s", d["id"], exc.code)
                    db.update_draft(d["id"], error=f"Telegram: {exc.description}")
        d = db.get_draft(d["id"])

        # ---- Facebook (лише якщо координаторка обрала кнопку з Facebook)
        # Facebook і LinkedIn – разом із Telegram-дописом, а не раніше за нього (інтервал).
        tg_out = d["tg_status"] in PUBLISHED + ("failed",)
        if d["fb_status"] == "queued" and d["target"] in ("tgfb", "all") and tg_out:
            if not s.facebook_enabled:
                db.update_draft(d["id"], fb_status="none")
            elif _count_today(db, "fb_status", "fb_published_at", live=live) >= fb_limit:
                stats["deferred"] += 1
            elif mode == "live" and fb_api is None:
                log.warning("Не задано FACEBOOK_PAGE_TOKEN або facebook.page_id – допис №%s чекає", d["id"])
            else:
                try:
                    status, post_id = _publish_facebook(s, fb_api, d)
                    db.update_draft(d["id"], fb_status=status, fb_post_id=post_id, fb_published_at=iso(utcnow()))
                    stats["fb"] += 1
                    _notify(s, notify_api, f"{tag}Допис №{d['id']} опубліковано на Facebook.")
                except FacebookError as exc:
                    log.warning("Facebook: допис №%s не опубліковано, код %s", d["id"], exc.code)
                    if exc.token_invalid:
                        db.update_draft(d["id"], fb_status="failed", error="Facebook: недійсний токен")
                        _notify(s, notify_api, f"Допис №{d['id']} не вдалося опублікувати на Facebook: токен сторінки "
                                "недійсний або прострочений. Оновіть FACEBOOK_PAGE_TOKEN за інструкцією в README.")
                    else:
                        db.update_draft(d["id"], error=f"Facebook: {exc}")
        d = db.get_draft(d["id"])

        # ---- LinkedIn (лише якщо координаторка обрала кнопку з LinkedIn)
        if d["li_status"] == "queued" and d["target"] == "all" and tg_out:
            if not s.linkedin_enabled:
                db.update_draft(d["id"], li_status="none")
            elif _count_today(db, "li_status", "li_published_at", live=live) >= li_limit:
                stats["deferred"] += 1
            elif mode == "live" and li_api is None:
                log.warning("Не задано LINKEDIN_ACCESS_TOKEN або linkedin.organization_id – допис №%s чекає", d["id"])
            else:
                try:
                    status, post_id = _publish_linkedin(s, li_api, d)
                    db.update_draft(d["id"], li_status=status, li_post_id=post_id, li_published_at=iso(utcnow()))
                    stats["li"] += 1
                    _notify(s, notify_api, f"{tag}Допис №{d['id']} опубліковано в LinkedIn.")
                except LinkedInError as exc:
                    log.warning("LinkedIn: допис №%s не опубліковано, код %s", d["id"], exc.code)
                    if exc.token_invalid:
                        db.update_draft(d["id"], li_status="failed", error="LinkedIn: недійсний токен")
                        _notify(s, notify_api, f"Допис №{d['id']} не вдалося опублікувати в LinkedIn: токен "
                                "недійсний або прострочений (він діє 60 днів). Оновіть LINKEDIN_ACCESS_TOKEN за інструкцією в README.")
                    else:
                        db.update_draft(d["id"], error=f"LinkedIn: {exc}")
        d = db.get_draft(d["id"])

        tg_done = d["tg_status"] in PUBLISHED or d["tg_status"] == "failed"
        fb_done = d["target"] not in ("tgfb", "all") or d["fb_status"] in PUBLISHED + ("failed", "none")
        li_done = d["target"] != "all" or d["li_status"] in PUBLISHED + ("failed", "none")
        if tg_done and fb_done and li_done:
            db.update_draft(d["id"], status="published")
    return stats
