"""Тести класифікації на 20 синтетичних прикладах (tests/fixtures/news_examples.json).

* Офлайн-тести (запускаються завжди): попередній відбір, підказка стовпа за
  ключовими словами з config.yaml і повний конвеєр з імітацією моделі –
  перевіряють правило «підтверджена новина», заборонені джерела і 72 години.
* Живий тест моделі (RUN_LIVE_TESTS=1 і ANTHROPIC_API_KEY): кожен приклад
  надсилається в Claude, перевіряються релевантність, стовп і правила тексту.
  Коштує кілька центів.
"""
import os

import pytest

from vpo_news import collector, keywords, pipeline, text_rules
from vpo_news.config import PILLAR_KEYS

from .conftest import insert_example_items, load_examples

EXAMPLES = load_examples()
RELEVANT = [e for e in EXAMPLES if e["expected"]["relevant"] and not e["expected"].get("blocked")]


def _text(e):
    return "\n".join(f"{i['title']}\n{i['body']}" for i in e["items"])


def test_fixture_set_size():
    assert 15 <= len(EXAMPLES) <= 22
    assert {e["expected"]["pillar"] for e in RELEVANT} == set(PILLAR_KEYS)


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["id"] for e in EXAMPLES])
def test_prefilter(cfg, example):
    expected = example["expected"].get("prefilter", True)
    assert collector.is_relevant_candidate(_text(example), cfg) is expected


@pytest.mark.parametrize("example", RELEVANT, ids=[e["id"] for e in RELEVANT])
def test_keyword_pillar_hint(cfg, example):
    assert keywords.guess_pillar(_text(example), cfg) == example["expected"]["pillar"]


class FakeClassifier:
    """Повертає «правильну» відповідь з фікстури – перевіряємо код навколо моделі."""

    def __init__(self):
        self.by_url = {i["url"]: e for e in EXAMPLES for i in e["items"]}
        self.calls = 0

    def classify(self, rep, others, body_range):
        self.calls += 1
        e = self.by_url[rep["url"]]
        lo, hi = body_range
        body = ""
        while len(body) < lo + 20:
            body += "Тестовий текст допису про подію з першоджерела, без оцінок і зайвих слів. "
        return {
            "relevant": e["expected"]["relevant"],
            "pillar": e["expected"]["pillar"] or "NONE",
            "secondary_pillar": "NONE",
            # як і справжня модель, визнаємо «тією самою подією» лише матеріали з того ж прикладу
            "same_event_urls": [o["url"] for o in others if o["url"] in {i["url"] for i in e["items"]}],
            "is_rumor_or_unconfirmed": False,
            "verified": True,
            "verification_basis": {"type": "primary", "urls": [rep["url"]]},
            "deadline": e["expected"].get("deadline", ""),
            "summary_uk": body.strip()[:hi],
            "confidence": 0.9,
            "reason": "тест",
        }

    def rewrite(self, rep, body, errors, body_range):
        raise AssertionError(f"rewrite не мав викликатися: {errors}")


def test_pipeline_with_fake_model(cfg, db):
    ids = {}
    for e in EXAMPLES:
        ids[e["id"]] = insert_example_items(db, cfg, e)
    # заборонені домени взагалі не потрапляють у базу
    assert ids["f18"] == [None] and ids["f19"] == [None]

    fake = FakeClassifier()
    pipeline.run(cfg, db, fake)

    drafts = {d["item_id"]: d for d in db.query("SELECT * FROM drafts")}
    for e in EXAMPLES:
        exp = e["expected"]
        item_ids = [i for i in ids[e["id"]] if i]
        if not item_ids:
            continue
        has_draft = any(i in drafts for i in item_ids)
        if exp.get("too_old"):
            assert not has_draft and db.query("SELECT status FROM items WHERE id=?", (item_ids[0],))[0][0] == "unverified"
        elif not exp["verified"]:
            assert not has_draft, e["id"]  # одне медіа – чекає на друге джерело
        elif exp.get("deadline_passed"):
            d = next(drafts[i] for i in item_ids if i in drafts)
            assert d["status"] == "doubtful" and "дедлайн" in d["reason"], e["id"]
        elif exp["relevant"]:
            assert has_draft, e["id"]
            d = next(drafts[i] for i in item_ids if i in drafts)
            assert d["pillar"] == exp["pillar"] and d["status"] == "to_send"
            assert (d["deadline"] or "") == exp.get("deadline", "")
            assert d["text"].startswith(cfg["pillars"][exp["pillar"]]["title"])
        else:
            assert not has_draft, e["id"]

    # Дві новини про ту саму подію (f02, f06, f11, f14) дають одну чернетку
    for eid in ("f02", "f06", "f11", "f14"):
        assert sum(1 for i in ids[eid] if i in drafts) == 1, eid
    assert len([d for d in drafts.values() if d["pillar"] == "DIM"]) == 3  # f01, f02, f03
    calls = fake.calls
    pipeline.run(cfg, db, fake)
    assert fake.calls == calls  # повторний запуск не викликає модель вдруге


# ---------------------------------------------------------------- живий тест

live = pytest.mark.skipif(
    not (os.environ.get("RUN_LIVE_TESTS") == "1" and os.environ.get("ANTHROPIC_API_KEY")),
    reason="живий тест моделі: потрібні RUN_LIVE_TESTS=1 і ANTHROPIC_API_KEY",
)

LIVE_CASES = [e for e in EXAMPLES if not e["expected"].get("blocked") and not e["expected"].get("too_old")]


@live
@pytest.mark.parametrize("example", LIVE_CASES, ids=[e["id"] for e in LIVE_CASES])
def test_live_model(cfg, db, example):
    from vpo_news.llm import Classifier

    ids = [i for i in insert_example_items(db, cfg, example) if i]
    rows = [db.query("SELECT * FROM items WHERE id=?", (i,))[0] for i in ids]
    rep, others = rows[0], rows[1:]
    header = cfg["pillars"]["POVERNENNIA"]["title"]
    res = Classifier(cfg).classify(rep, others, text_rules.body_length_range(header, rep["url"], cfg))
    exp = example["expected"]
    assert res["relevant"] is exp["relevant"], res["reason"]
    if exp["relevant"]:
        assert res["pillar"] == exp["pillar"], res["reason"]
        if exp.get("deadline"):
            assert res["deadline"] == exp["deadline"]
        if exp["verified"]:
            pheader = cfg["pillars"][res["pillar"]]["title"]
            text = text_rules.compose(pheader, text_rules.sanitize(res["summary_uk"]), rep["url"])
            errors, _ = text_rules.validate(text, pheader, rep["url"], cfg, _text(example))
            assert not errors, errors
        else:
            assert res["is_rumor_or_unconfirmed"] or res["confidence"] < 0.7
