"""Виводить chat_id групи і message_thread_id усіх тем, які бачить бот.

Як користуватися (докладно – у README, розділ «ID групи і тем»):
  1. Додайте бота-публікатора в групу «Національна платформа» адміністратором.
  2. Напишіть по одному короткому повідомленню в КОЖНУ тему (ДІМ, ГОЛОС, ...).
     Повідомлення потім можна видалити.
  3. Запустіть:  python scripts/get_topic_ids.py
  4. Перенесіть числа в config.yaml (telegram.group_chat_id і telegram.topics).

Також скрипт показує chat_id людей, які написали боту в приватний чат, –
так координаторка може дізнатися свій chat_id.

Telegram зберігає оновлення 24 години, тож запускайте скрипт невдовзі після кроку 2.
Поки скрипт працює, вимкніть робочий процес approvals у GitHub Actions, якщо
публікує той самий бот, що й погоджує: інакше він «забере» ці оновлення.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpo_news.telegram import TelegramAPI, TelegramError  # noqa: E402

KEY_BY_NAME = {"ДІМ": "DIM", "ГОЛОС": "HOLOS", "ТУРБОТА": "TURBOTA", "ПОВЕРНЕННЯ": "POVERNENNIA", "ПАМ'ЯТЬ": "PAMIAT", "ПАМ’ЯТЬ": "PAMIAT", "МОЖЛИВОСТІ": "MOZHLYVOSTI"}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    token = os.environ.get("TELEGRAM_PUBLISH_BOT_TOKEN") or os.environ.get("TELEGRAM_APPROVAL_BOT_TOKEN")
    if not token:
        print("Задайте TELEGRAM_PUBLISH_BOT_TOKEN (або TELEGRAM_APPROVAL_BOT_TOKEN) у змінних оточення.")
        return 2
    api = TelegramAPI(token)
    try:
        me = api.call("getMe")
        updates = api.get_updates(timeout=0, allowed_updates=["message"])
    except TelegramError as exc:
        print(f"Помилка Telegram: {exc.code} {exc.description}")
        return 1
    print(f"Бот: @{me.get('username')}")

    chats: dict[int, dict] = {}
    private: dict[int, str] = {}
    for u in updates:
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private":
            private[chat["id"]] = chat.get("first_name") or ""
            continue
        if chat.get("type") not in ("group", "supergroup"):
            continue
        info = chats.setdefault(chat["id"], {"title": chat.get("title"), "username": chat.get("username"), "is_forum": chat.get("is_forum"), "topics": {}})
        thread = msg.get("message_thread_id")
        if not thread or not msg.get("is_topic_message"):
            continue
        name = None
        created = msg.get("forum_topic_created") or (msg.get("reply_to_message") or {}).get("forum_topic_created")
        if created:
            name = created.get("name")
        info["topics"].setdefault(thread, name)

    for pid, name in private.items():
        print(f"Приватний чат з ботом: {name} – chat_id {pid} (для TELEGRAM_COORDINATOR_CHAT_ID)")
    if not chats:
        print("Бот не бачить повідомлень із груп. Перевірте, що бот – адміністратор групи і що в темах "
              "з'явилися нові повідомлення за останні 24 години.")
        return 1

    for cid, info in chats.items():
        uname = f" (@{info['username']})" if info["username"] else ""
        print(f"\nГрупа: {info['title']}{uname}\n  group_chat_id: {cid}\n  теми (forum): {'так' if info['is_forum'] else 'ні'}")
        for thread, name in sorted(info["topics"].items()):
            print(f"  message_thread_id {thread}: {name or '(назву не видно – подивіться, в яку тему писали)'}")
        guessed = {KEY_BY_NAME.get((n or "").strip().upper()): t for t, n in info["topics"].items() if n}
        guessed.pop(None, None)
        if guessed:
            print("\n  Фрагмент для config.yaml:")
            print(f"  group_chat_id: {cid}")
            print("  topics:")
            for key in ("DIM", "HOLOS", "TURBOTA", "POVERNENNIA", "PAMIAT", "MOZHLYVOSTI"):
                print(f"    {key}: {guessed.get(key, 0)}")
    print("\nЗагальна тема (General) не має message_thread_id – вона для публікацій не використовується.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
