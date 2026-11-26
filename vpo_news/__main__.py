"""Точка входу: python -m vpo_news <команда>.

Команди:
  tick           запуск за розкладом: approvals щоразу, collect раз на 3 год, digest о 9:00
  collect        зібрати новини, обробити, надіслати чернетки на погодження
  approvals      обробити кнопки й відповіді координаторки, прострочити старі, опублікувати погоджене
  digest         надіслати щоденний підсумок (о 9:00 за Києвом; --force – будь-коли)
  samples        тестовий прогін: 5 чернеток, по одній на стовп (див. README)
  check-sources  перевірити, чи відповідають джерела з config.yaml
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import approval, collector, digest, pipeline, publisher
from .config import load_settings
from .db import DB
from datetime import timedelta

from .util import iso, kyiv_date, parse_iso, setup_logging, utcnow

log = logging.getLogger("vpo_news")
STARTED = utcnow()


def _classifier(cfg):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("Не задано ANTHROPIC_API_KEY – новини буде зібрано, але не класифіковано")
        return None
    from .llm import Classifier
    return Classifier(cfg)


def cmd_collect(args) -> int:
    s = load_settings(args.config)
    db = DB(s.db_path)
    stats = collector.collect(s.cfg, db)
    log.info("Збір: %s", stats)
    pstats = pipeline.run(s.cfg, db, _classifier(s.cfg))
    log.info("Обробка: %s", pstats)
    sent = approval.send_pending(s, db)
    log.info("Надіслано на погодження: %d", sent)
    # Раз на добу прибираємо старі записи, щоб база лишалася малою.
    if db.get_meta("last_prune") != kyiv_date():
        st = s.cfg.get("storage", {})
        db.prune(int(st.get("keep_items_days", 30)), int(st.get("keep_drafts_days", 120)))
        db.set_meta("last_prune", kyiv_date())
    return 0


def cmd_approvals(args) -> int:
    s = load_settings(args.config)
    db = DB(s.db_path)
    n = approval.process_updates(s, db)
    log.info("Оновлень від Telegram: %d", n)
    log.info("Прострочено чернеток: %d", approval.expire_drafts(s, db))
    log.info("Надіслано на погодження: %d", approval.send_pending(s, db))
    log.info("Публікація: %s", publisher.run_queue(s, db))
    return 0


def cmd_tick(args) -> int:
    """Один запуск за розкладом (кожні 10 хв): кнопки, публікація; збір – раз на N годин; підсумок – о 9:00."""
    cmd_approvals(args)
    s = load_settings(args.config)
    db = DB(s.db_path)
    every = float(s.cfg.get("schedule", {}).get("collect_every_hours", 3))
    last = parse_iso(db.get_meta("last_collect"))
    # невеликий запас, щоб затримки розкладу GitHub не зсували збір на цілий цикл
    due = last is None or utcnow() - last >= timedelta(hours=every) - timedelta(minutes=15)
    if due or getattr(args, "collect_now", False):
        db.set_meta("last_collect", iso(utcnow()))
        db.close()
        cmd_collect(args)
        db = DB(s.db_path)
    digest.send(s, db)
    db.close()
    # --listen-minutes – загальний час роботи процесу; збір новин теж його займає.
    total = float(getattr(args, "listen_minutes", 0) or 0)
    minutes = total - (utcnow() - STARTED).total_seconds() / 60
    if minutes > 1:
        listen(s, minutes)
    return 0


def listen(s, minutes: float) -> None:
    """Слухає бота заданий час: кнопки й відповіді координаторки обробляються за секунди.

    GitHub запускає процес за розкладом нерегулярно (буває раз на годину й рідше),
    тож без цього координаторка чекала б на реакцію бота до години.
    """
    deadline = utcnow() + timedelta(minutes=minutes)
    log.info("Слухаю бота %.0f хв", minutes)
    handled = 0
    while utcnow() < deadline:
        db = DB(s.db_path)
        try:
            wait = int(max(1, min(25, (deadline - utcnow()).total_seconds())))
            n = approval.process_updates(s, db, wait=wait)
            if n:
                handled += n
                approval.send_pending(s, db)  # нова версія після редагування
                publisher.run_queue(s, db)
        except Exception:  # noqa: BLE001 – збій мережі не має зупиняти прослуховування
            log.exception("Помилка під час прослуховування бота")
        finally:
            db.close()
    log.info("Прослуховування завершено, оновлень: %d", handled)


def cmd_digest(args) -> int:
    s = load_settings(args.config)
    db = DB(s.db_path)
    if args.print:
        print(digest.build(s, db))
        return 0
    digest.send(s, db, force=args.force)
    return 0


def cmd_samples(args) -> int:
    from .samples import run_samples
    return run_samples(args)


def cmd_check_sources(args) -> int:
    s = load_settings(args.config)
    for row in collector.check_sources(s.cfg):
        mark = "OK " if row.get("ok") else "ERR"
        extra = f"записів: {row.get('items', 0)}, з білого списку: {row.get('whitelisted', 0)}"
        if row.get("error"):
            extra = f"помилка: {row['error']}"
        if not row.get("enabled", True):
            mark, extra = "OFF", "вимкнено в config.yaml; " + extra
        print(f"[{mark}] {row['name']}: {extra}")
    return 0


def main(argv=None) -> int:
    setup_logging()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="vpo_news", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="шлях до config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect").set_defaults(fn=cmd_collect)
    sub.add_parser("approvals").set_defaults(fn=cmd_approvals)
    t = sub.add_parser("tick")
    t.add_argument("--collect-now", action="store_true", help="зібрати новини зараз, не чекаючи розкладу")
    t.add_argument("--listen-minutes", type=float, default=0,
                   help="працювати стільки хвилин загалом: після збору слухати бота й реагувати на кнопки одразу")
    t.set_defaults(fn=cmd_tick)
    d = sub.add_parser("digest")
    d.add_argument("--force", action="store_true", help="надіслати зараз, незалежно від часу")
    d.add_argument("--print", action="store_true", help="лише показати текст, нічого не надсилати")
    d.set_defaults(fn=cmd_digest)
    sm = sub.add_parser("samples")
    sm.add_argument("--fixtures-only", action="store_true", help="не збирати живі новини, взяти лише тестові приклади")
    sm.add_argument("--send", action="store_true", help="надіслати зразки координаторці в Telegram (без кнопок)")
    sm.add_argument("--out", default="samples/dry_run_samples.md")
    sm.set_defaults(fn=cmd_samples)
    sub.add_parser("check-sources").set_defaults(fn=cmd_check_sources)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
