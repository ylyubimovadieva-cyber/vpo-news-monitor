"""Тестовий прогін у режимі DRY_RUN: 5 чернеток, по одній на кожен стовп.

Спершу береться свіжа реальна новина для кожного стовпа. Якщо для якогось
стовпа за останні 72 години підтвердженої новини немає, береться синтетичний
приклад із tests/fixtures (це чітко позначається у звіті).
Нічого не публікується: результат – файл samples/dry_run_samples.md і, за
бажання (--send), повідомлення координаторці без кнопок.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path

from . import collector, dedupe, pipeline, text_rules, verify
from .config import PILLAR_KEYS, ROOT, load_settings
from .db import DB, basis, warnings_of
from .telegram import TelegramAPI
from .util import domain_of, iso, normalize_url, utcnow

log = logging.getLogger(__name__)

FIXTURES = ROOT / "tests" / "fixtures" / "news_examples.json"


def _insert_fixture(db: DB, cfg: dict, example: dict) -> list[int]:
    ids = []
    for it in example["items"]:
        domain = domain_of(it["url"])
        info = verify.classify_domain(domain, cfg)
        if info is None:
            continue
        ids.append(db.insert_item(
            url=it["url"], url_norm=normalize_url(it["url"]), title=it["title"],
            title_key=dedupe.title_key(it["title"]), source_name=it["source_name"] + " (синтетичний приклад)",
            domain=domain, kind=info.kind, owner=info.owner,
            published_at=iso(utcnow() - timedelta(hours=min(it["hours_ago"], 48))),
            summary=it["body"][:300], body=it["body"], status="new",
        ))
    return ids


def _best_per_pillar(db: DB) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for d in db.query("SELECT * FROM drafts ORDER BY (status = 'to_send') DESC, confidence DESC"):
        best.setdefault(d["pillar"], d)
    return best


def _section(s, d, origin: str) -> str:
    errors, _ = text_rules.validate(d["text"], s.pillar_title(d["pillar"]), d["source_url"], s.cfg)
    second = s.pillar_title(d["secondary_pillar"]) if d["secondary_pillar"] else "–"
    lines = [
        f"## {s.pillar_title(d['pillar'])}",
        "",
        f"- Походження: {origin}",
        f"- Статус: {'готово до погодження' if d['status'] == 'to_send' else 'сумнівне – ' + (d['reason'] or '')}",
        f"- Другий стовп: {second}",
        f"- Підстава підтвердження: {verify.describe_basis(basis(d))}",
        f"- Впевненість моделі: {d['confidence']:.2f}",
        f"- Довжина: {len(d['text'])} символів; порушень правил: {len(errors)}",
    ]
    if warnings_of(d):
        lines.append(f"- Попередження: {'; '.join(warnings_of(d))}")
    lines += ["", "```text", d["text"], "```", ""]
    return "\n".join(lines)


def run_samples(args) -> int:
    s = load_settings(args.config)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Для тестового прогону потрібен ANTHROPIC_API_KEY у змінних оточення (див. README).")
        return 2
    from .llm import Classifier

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    db_path = out.parent / "samples.sqlite"
    if db_path.exists():
        db_path.unlink()
    db = DB(db_path)
    clf = Classifier(s.cfg)

    origins: dict[int, str] = {}
    if not args.fixtures_only:
        log.info("Збираю реальні новини (це займе кілька хвилин через паузи між запитами)")
        collector.collect(s.cfg, db)
        pipeline.run(s.cfg, db, clf)
    for d in db.query("SELECT id FROM drafts"):
        origins[d["id"]] = "реальна новина за останні 72 години"

    best = _best_per_pillar(db)
    missing = [p for p in PILLAR_KEYS if p not in best or best[p]["status"] != "to_send"]
    if missing:
        examples = json.loads(FIXTURES.read_text(encoding="utf-8"))["examples"]
        for p in missing:
            for ex in examples:
                exp = ex["expected"]
                if exp["pillar"] == p and exp["relevant"] and exp["verified"] and not exp.get("too_old"):
                    before = {r["id"] for r in db.query("SELECT id FROM drafts")}
                    _insert_fixture(db, s.cfg, ex)
                    pipeline.run(s.cfg, db, clf)
                    for r in db.query("SELECT id FROM drafts"):
                        if r["id"] not in before:
                            origins[r["id"]] = f"СИНТЕТИЧНИЙ приклад {ex['id']} (вигадана новина лише для оцінки стилю)"
                    nb = _best_per_pillar(db)
                    if p in nb and nb[p]["status"] == "to_send":
                        break
        best = _best_per_pillar(db)

    parts = [
        "# Тестовий прогін DRY_RUN: зразки дописів",
        "",
        f"Створено: {utcnow().strftime('%Y-%m-%d %H:%M')} UTC. Модель: {s.cfg['llm']['model']}. Нічого не опубліковано.",
        "Дописи, позначені як синтетичні, створено з вигаданих тестових новин – їх не можна публікувати.",
        "",
    ]
    for p in PILLAR_KEYS:
        if p in best:
            parts.append(_section(s, best[p], origins.get(best[p]["id"], "")))
        else:
            parts.append(f"## {s.pillar_title(p)}\n\nНе вдалося підготувати зразок.\n")
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"Зразки збережено: {out.resolve()}")

    if args.send:
        if not (s.approval_token and s.coordinator_chat_id):
            print("Для --send потрібні TELEGRAM_APPROVAL_BOT_TOKEN і chat_id координаторки")
        else:
            api = TelegramAPI(s.approval_token)
            for p in PILLAR_KEYS:
                if p in best:
                    d = best[p]
                    api.send_message(s.coordinator_chat_id,
                                     f"ЗРАЗОК ДЛЯ ОЦІНКИ СТИЛЮ (не буде опубліковано)\n{origins.get(d['id'], '')}\n\n{d['text']}",
                                     disable_preview=True)
    db.close()
    return 0
