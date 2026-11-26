"""Обробка зібраних новин: групування за подіями, перевірка, класифікація, чернетки."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from . import dedupe, text_rules, verify, images
from .config import OPPORTUNITY_KEY
from .db import DB
from .llm import Classifier, LLMError
from .util import KYIV, iso, older_than, parse_iso, utcnow

log = logging.getLogger(__name__)

OPEN_STATUSES = ("new", "waiting")
# Можливість з дедлайном менш ніж за стільки днів не має сенсу публікувати.
OPPORTUNITY_MIN_DAYS_LEFT = 2


def kyiv_today() -> date:
    return utcnow().astimezone(KYIV).date()


def parse_deadline(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None


def expire_waiting(cfg: dict, db: DB) -> int:
    """Новини, що так і не отримали підтвердження за 72 години, позначаються «unverified»."""
    max_age = float(cfg.get("collector", {}).get("max_age_hours", 72))
    n = 0
    for item in db.items_by_status("new", "waiting"):
        if older_than(parse_iso(item["published_at"]), max_age):
            db.set_item_status(item["id"], "unverified", "не знайдено першоджерела або другого незалежного медіа за 72 години")
            n += 1
    return n


def assign_clusters(cfg: dict, db: DB) -> None:
    """Кожна нова новина приєднується до найсхожішої події за 72 години або починає нову."""
    dcfg = cfg.get("dedupe", {})
    threshold = float(dcfg.get("cluster_threshold", 0.25))
    max_age = float(cfg.get("collector", {}).get("max_age_hours", 72))
    since = iso(utcnow() - timedelta(hours=max_age))
    for item in db.items_by_status("new"):
        best, best_score = None, 0.0
        # Порівнюємо лише з першою новиною кожної події, щоб події не «злипалися» ланцюжком.
        for other in db.recent_items(since):
            if other["id"] == item["id"] or other["id"] != other["cluster_id"]:
                continue
            score = dedupe.event_similarity(
                item["title"], item["summary"] or item["body"] or "",
                other["title"], other["summary"] or other["body"] or "",
            )
            if score > best_score:
                best, best_score = other, score
        if best is not None and best_score >= threshold:
            cluster_id = best["cluster_id"]
        else:
            cluster_id = item["id"]
        db.set_item_cluster(item["id"], cluster_id)
        if db.draft_for_cluster(cluster_id):
            db.set_item_status(item["id"], "duplicate", f"та сама подія вже має чернетку (кластер {cluster_id})")
        else:
            db.set_item_status(item["id"], "waiting")


def _signature(items) -> str:
    return ",".join(str(i["id"]) for i in sorted(items, key=lambda i: i["id"]))


def _pick_rep(items):
    primaries = [i for i in items if i["kind"] == "primary"]
    pool = primaries or list(items)
    return sorted(pool, key=lambda i: i["published_at"])[0]


def process_clusters(cfg: dict, db: DB, classifier: Classifier | None) -> dict:
    stats = {"checked": 0, "drafts": 0, "doubtful": 0, "irrelevant": 0, "waiting": 0, "errors": 0}
    llm_cfg = cfg.get("llm", {})
    min_conf = float(llm_cfg.get("min_confidence", 0.7))
    max_attempts = int(llm_cfg.get("max_attempts_per_cluster", 3))
    prefix = cfg.get("text_rules", {}).get("source_prefix", "Джерело: ")

    cluster_ids = sorted({i["cluster_id"] for i in db.items_by_status("waiting") if i["cluster_id"]})
    for cid in cluster_ids:
        if db.draft_for_cluster(cid):
            continue
        items = [i for i in db.cluster_items(cid) if i["status"] == "waiting"]
        if not items:
            continue
        ok, _ = verify.verify_items(items)
        if not ok:
            stats["waiting"] += 1
            continue  # чекаємо першоджерела або другого медіа
        sig = _signature(items)
        state = db.cluster_state(cid)
        if state and (state["checked_sig"] == sig or state["attempts"] >= max_attempts):
            continue  # склад події не змінився з минулої перевірки
        if classifier is None:
            log.info("Немає ключа Anthropic – кластер %s чекає на класифікацію", cid)
            continue

        rep = _pick_rep(items)
        others = [i for i in items if i["id"] != rep["id"]][:6]
        source_url = verify.pick_source_url(items)
        pillar_header_guess = max((p["title"] for p in cfg["pillars"].values()), key=len)
        body_range = text_rules.body_length_range(pillar_header_guess, source_url, cfg)

        stats["checked"] += 1
        db.mark_cluster_checked(cid, sig)
        try:
            res = classifier.classify(rep, others, body_range)
        except LLMError as exc:
            log.warning("Кластер %s: %s", cid, exc)
            stats["errors"] += 1
            continue

        if not res["relevant"] or res["pillar"] == "NONE":
            for i in items:
                db.set_item_status(i["id"], "irrelevant", res.get("reason"))
            stats["irrelevant"] += 1
            continue

        # Залишаємо в події лише ті матеріали, які модель визнала тією самою подією.
        same = set(res.get("same_event_urls") or [])
        confirmed = [rep] + [o for o in others if o["url"] in same]
        confirmed_ids = {c["id"] for c in confirmed}
        for o in items:
            if o["id"] not in confirmed_ids:
                db.set_item_cluster(o["id"], o["id"])  # окрема подія, чекає на власне підтвердження
        ok, basis = verify.verify_items(confirmed)
        if not ok:
            stats["waiting"] += 1
            continue
        source_url = verify.pick_source_url(confirmed)
        source_item = next(i for i in confirmed if i["url"] == source_url)

        pillar = res["pillar"]
        secondary = res["secondary_pillar"] if res["secondary_pillar"] not in ("NONE", pillar) else None
        header = cfg["pillars"][pillar]["title"]
        body_range = text_rules.body_length_range(header, source_url, cfg)
        source_text = "\n".join(f"{i['title']}\n{i['body'] or i['summary'] or ''}" for i in confirmed)

        body = text_rules.sanitize(res.get("summary_uk") or "")
        text = text_rules.compose(header, body, source_url, prefix)
        errors, warnings = text_rules.validate(text, header, source_url, cfg, source_text)
        if errors and body:
            try:
                body = text_rules.sanitize(classifier.rewrite(source_item, body, errors, body_range))
                text = text_rules.compose(header, body, source_url, prefix)
                errors, warnings = text_rules.validate(text, header, source_url, cfg, source_text)
            except LLMError as exc:
                log.warning("Кластер %s: не вдалося переписати текст: %s", cid, exc)

        reasons = []
        if res["is_rumor_or_unconfirmed"]:
            reasons.append("модель позначила як чутки або непідтверджені дані")
        if not res["verified"]:
            reasons.append("модель не підтвердила, що джерела описують саме цю подію")
        if float(res["confidence"]) < min_conf:
            reasons.append(f"низька впевненість {float(res['confidence']):.2f}")
        if errors:
            reasons.append("текст не відповідає правилам: " + "; ".join(errors))
        deadline = parse_deadline(res.get("deadline"))
        if pillar == OPPORTUNITY_KEY:
            if deadline is None:
                reasons.append("у матеріалі про можливість не вказано дедлайн")
            elif deadline <= kyiv_today() + timedelta(days=OPPORTUNITY_MIN_DAYS_LEFT):
                reasons.append(f"дедлайн {deadline:%d.%m.%Y} уже минув або надто близько")
        status = "doubtful" if reasons else "to_send"
        # Зображення – лише для чернеток, що підуть координаторці (перевірка коштує запиту до моделі).
        image = {}
        if status == "to_send":
            try:
                image = images.choose(cfg, classifier, source_item, pillar, text)
            except Exception as exc:  # noqa: BLE001 – без зображення чернетка все одно йде на погодження
                log.warning("Кластер %s: не вдалося підібрати зображення: %s", cid, type(exc).__name__)

        db.insert_draft(
            cluster_id=cid, item_id=source_item["id"], pillar=pillar, secondary_pillar=secondary,
            text=text, source_url=source_url, verification_basis=basis,
            confidence=float(res["confidence"]), warnings=warnings,
            reason="; ".join(reasons) or res.get("reason"), status=status,
            deadline=deadline.isoformat() if deadline else None,
            **image,
        )
        for i in items:
            if i["id"] in confirmed_ids:
                db.set_item_status(i["id"], "drafted" if status == "to_send" else "doubtful", res.get("reason"))
        stats["drafts" if status == "to_send" else "doubtful"] += 1
    return stats


def run(cfg: dict, db: DB, classifier: Classifier | None) -> dict:
    expired = expire_waiting(cfg, db)
    assign_clusters(cfg, db)
    stats = process_clusters(cfg, db, classifier)
    stats["unverified"] = expired
    return stats
