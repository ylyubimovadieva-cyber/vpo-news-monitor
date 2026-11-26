"""Погодження дописів координаторкою в приватному чаті з ботом."""
from __future__ import annotations

import logging
from datetime import timedelta

from . import text_rules
from .config import Settings
from .db import DB, basis, warnings_of
from .telegram import TelegramAPI, TelegramError
from .util import fmt_kyiv, iso, older_than, parse_iso, utcnow
from .verify import describe_basis

log = logging.getLogger(__name__)

ACTIONS = {"tg": "Опублікувати в Telegram", "tgfb": "Опублікувати в Telegram і Facebook",
           "all": "Опублікувати в Telegram, Facebook і LinkedIn", "edit": "Редагувати", "rej": "Відхилити"}
LABELS = {"tg": "Telegram", "tgfb": "Telegram і Facebook", "all": "Telegram, Facebook і LinkedIn"}


def keyboard(draft_id: int, facebook: bool, linkedin: bool = False) -> dict:
    rows = [[{"text": ACTIONS["tg"], "callback_data": f"tg:{draft_id}"}]]
    if facebook:
        rows.append([{"text": ACTIONS["tgfb"], "callback_data": f"tgfb:{draft_id}"}])
    if facebook and linkedin:
        rows.append([{"text": ACTIONS["all"], "callback_data": f"all:{draft_id}"}])
    rows.append([
        {"text": ACTIONS["edit"], "callback_data": f"edit:{draft_id}"},
        {"text": ACTIONS["rej"], "callback_data": f"rej:{draft_id}"},
    ])
    return {"inline_keyboard": rows}


def render(s: Settings, d, footer: str | None = None) -> str:
    title = s.pillar_title(d["pillar"])
    second = f" (другий стовп: {s.pillar_title(d['secondary_pillar'])})" if d["secondary_pillar"] else ""
    lines = [
        f"Чернетка №{d['id']}" + (" (виправлена)" if d["edited"] else ""),
        f"Стовп: {title}{second}",
        f"Підстава підтвердження: {describe_basis(basis(d))}",
    ]
    for u in basis(d).get("urls", []):
        lines.append(f"  {u}")
    lines.append(f"Джерело в дописі: {d['source_url']}")
    if d["deadline"]:
        lines.append(f"Дедлайн можливості: {d['deadline'][8:10]}.{d['deadline'][5:7]}.{d['deadline'][:4]}")
    lines.append(f"Впевненість моделі: {d['confidence']:.2f}".replace(".", ","))
    if d["sent_at"]:
        deadline = parse_iso(d["sent_at"]) + timedelta(hours=float(s.cfg.get("approval", {}).get("expire_hours", 48)))
        lines.append(f"Погодити до: {fmt_kyiv(deadline)} (Київ)")
    w = warnings_of(d)
    if w:
        lines.append("Зверніть увагу: " + "; ".join(w))
    lines += ["", "Текст допису:", "", d["text"]]
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


def _tg(s: Settings, api: TelegramAPI | None) -> TelegramAPI | None:
    if api:
        return api
    if not s.approval_token:
        log.warning("Не задано TELEGRAM_APPROVAL_BOT_TOKEN – чернетки не надсилаються")
        return None
    return TelegramAPI(s.approval_token)


def send_pending(s: Settings, db: DB, api: TelegramAPI | None = None) -> int:
    """Надсилає координаторці нові чернетки, кожну окремим повідомленням."""
    api = _tg(s, api)
    if api is None or not s.coordinator_chat_id:
        if not s.coordinator_chat_id:
            log.warning("Не задано coordinator_chat_id – чернетки не надсилаються")
        return 0
    n = 0
    for d in db.drafts_by_status("to_send"):
        db.update_draft(d["id"], sent_at=iso(utcnow()))
        d = db.get_draft(d["id"])
        try:
            msg = api.send_message(s.coordinator_chat_id, render(s, d),
                                   reply_markup=keyboard(d["id"], s.facebook_enabled, s.linkedin_enabled),
                                   disable_preview=True)
        except TelegramError as exc:
            log.warning("Не вдалося надіслати чернетку %s: %s", d["id"], exc.code)
            db.update_draft(d["id"], sent_at=None)
            continue
        db.update_draft(d["id"], status="awaiting", approval_message_id=msg["message_id"])
        n += 1
    return n


def _close_message(s: Settings, api: TelegramAPI, d, footer: str) -> None:
    if not d["approval_message_id"]:
        return
    try:
        api.edit_message_text(s.coordinator_chat_id, d["approval_message_id"], render(s, d, footer))
    except TelegramError as exc:
        log.info("Не вдалося оновити повідомлення чернетки %s: %s", d["id"], exc.code)


def _handle_callback(s: Settings, db: DB, api: TelegramAPI, cq: dict) -> None:
    from_id = (cq.get("from") or {}).get("id")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
    if from_id != s.coordinator_chat_id or chat_id != s.coordinator_chat_id:
        log.info("Натискання не від координаторки – проігноровано")
        return
    action, _, raw_id = (cq.get("data") or "").partition(":")
    d = db.get_draft(int(raw_id)) if raw_id.isdigit() else None
    if d is None or d["status"] not in ("awaiting", "editing"):
        api.answer_callback(cq["id"], "Ця чернетка вже неактуальна")
        return
    # Натискання на старе повідомлення (до редагування) не враховуємо.
    if cq["message"]["message_id"] != d["approval_message_id"]:
        api.answer_callback(cq["id"], "Це стара версія. Скористайтеся кнопками під новою")
        return

    now = iso(utcnow())
    if action in ("tg", "tgfb", "all"):
        if action in ("tgfb", "all") and not s.facebook_enabled:
            api.answer_callback(cq["id"], "Публікацію на Facebook вимкнено")
            return
        if action == "all" and not s.linkedin_enabled:
            api.answer_callback(cq["id"], "Публікацію в LinkedIn вимкнено")
            return
        db.update_draft(d["id"], status="approved", target=action, decided_at=now, tg_status="queued",
                        fb_status="queued" if action in ("tgfb", "all") else "none",
                        li_status="queued" if action == "all" else "none")
        if db.get_meta("editing_draft") == str(d["id"]):
            db.set_meta("editing_draft", None)
        label = LABELS[action]
        api.answer_callback(cq["id"], f"Погоджено: {label}")
        _close_message(s, api, db.get_draft(d["id"]), f"Рішення: погоджено для публікації в {label}. "
                       "Допис вийде найближчим запуском з урахуванням денних лімітів.")
    elif action == "rej":
        db.update_draft(d["id"], status="rejected", decided_at=now)
        if db.get_meta("editing_draft") == str(d["id"]):
            db.set_meta("editing_draft", None)
        api.answer_callback(cq["id"], "Відхилено")
        _close_message(s, api, db.get_draft(d["id"]), "Рішення: відхилено.")
    elif action == "edit":
        db.update_draft(d["id"], status="editing")
        db.set_meta("editing_draft", str(d["id"]))
        api.answer_callback(cq["id"], "Надішліть виправлений текст")
        api.send_message(
            s.coordinator_chat_id,
            f"Надішліть виправлений текст чернетки №{d['id']} одним повідомленням. "
            "Назву стовпа і посилання на джерело можна не писати – я додам їх сам. "
            "Після перевірки надішлю нову версію з кнопками.",
        )
    else:
        api.answer_callback(cq["id"])


def _handle_text(s: Settings, db: DB, api: TelegramAPI, msg: dict) -> None:
    chat_id = (msg.get("chat") or {}).get("id")
    from_id = (msg.get("from") or {}).get("id")
    if chat_id != s.coordinator_chat_id or from_id != s.coordinator_chat_id:
        return  # бот пише лише координаторці й нікому не відповідає
    text = (msg.get("text") or "").strip()
    editing = db.get_meta("editing_draft")
    d = db.get_draft(int(editing)) if editing and editing.isdigit() else None
    if d is None or d["status"] != "editing":
        if text.startswith("/start") or text:
            api.send_message(s.coordinator_chat_id, "Чернетки надходять сюди автоматично. "
                             "Щоб змінити текст, натисніть «Редагувати» під потрібною чернеткою.")
        return

    header = s.pillar_title(d["pillar"])
    prefix = s.cfg.get("text_rules", {}).get("source_prefix", "Джерело: ")
    new_text = text_rules.ensure_frame(text_rules.sanitize(text), header, d["source_url"], prefix)
    errors, warnings = text_rules.validate(new_text, header, d["source_url"], s.cfg, source_text=new_text)
    if errors:
        api.send_message(s.coordinator_chat_id, "Текст ще не відповідає правилам:\n- " + "\n- ".join(errors)
                         + f"\n\nЗараз {len(new_text)} символів. Надішліть, будь ласка, виправлену версію.")
        return
    old_message = d["approval_message_id"]
    db.update_draft(d["id"], text=new_text, warnings=warnings, edited=1, status="awaiting")
    db.set_meta("editing_draft", None)
    d = db.get_draft(d["id"])
    sent = api.send_message(s.coordinator_chat_id, render(s, d),
                            reply_markup=keyboard(d["id"], s.facebook_enabled, s.linkedin_enabled),
                            disable_preview=True)
    db.update_draft(d["id"], approval_message_id=sent["message_id"])
    if old_message:
        try:
            api.edit_message_text(s.coordinator_chat_id, old_message, render(s, d, "Замінено новою версією нижче."))
        except TelegramError:
            pass


def process_updates(s: Settings, db: DB, api: TelegramAPI | None = None, wait: int = 0) -> int:
    """Обробляє натискання кнопок і повідомлення координаторки (getUpdates).

    wait – скільки секунд чекати на нові оновлення (long polling), 0 – не чекати.
    """
    api = _tg(s, api)
    if api is None:
        return 0
    offset = db.get_meta("tg_offset")
    updates = api.get_updates(offset=int(offset) if offset else None, timeout=wait,
                              allowed_updates=["message", "callback_query"])
    for upd in updates:
        try:
            if "callback_query" in upd:
                _handle_callback(s, db, api, upd["callback_query"])
            elif "message" in upd:
                _handle_text(s, db, api, upd["message"])
        except TelegramError as exc:
            log.warning("Помилка Telegram під час обробки оновлення: %s", exc.code)
        db.set_meta("tg_offset", str(upd["update_id"] + 1))
    return len(updates)


def expire_drafts(s: Settings, db: DB, api: TelegramAPI | None = None) -> int:
    hours = float(s.cfg.get("approval", {}).get("expire_hours", 48))
    api = _tg(s, api)
    n = 0
    for d in db.drafts_by_status("awaiting", "editing"):
        if d["sent_at"] and older_than(parse_iso(d["sent_at"]), hours):
            db.update_draft(d["id"], status="expired", decided_at=iso(utcnow()))
            if db.get_meta("editing_draft") == str(d["id"]):
                db.set_meta("editing_draft", None)
            if api:
                _close_message(s, api, db.get_draft(d["id"]), "Прострочено: відповіді не було 48 годин, допис не публікується.")
            n += 1
    return n
