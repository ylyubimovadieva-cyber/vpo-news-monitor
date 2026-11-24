"""Джерела: сторінки Facebook через Graph API і рубрика МОЖЛИВОСТІ."""
import httpx
import pytest

from vpo_news import collector, publisher
from vpo_news.util import iso, utcnow

from .conftest import FakeFacebook, FakeTelegram, make_settings

POSTS = {
    "data": [
        {"message": "Обласна військова адміністрація: для ВПО відкрито новий хаб допомоги\nПодробиці на сайті.",
         "created_time": "2026-11-18T08:00:00+0000",
         "permalink_url": "https://www.facebook.com/oda/posts/1",
         "attachments": {"data": [{"unshimmed_url": "https://adm.dp.gov.ua/news/test-hub"}]}},
        {"message": "Зустріч з переселенцями у громаді.",
         "created_time": "2026-11-18T09:00:00+0000",
         "permalink_url": "https://www.facebook.com/oda/posts/2"},
        {"created_time": "2026-11-18T09:00:00+0000", "permalink_url": "https://www.facebook.com/oda/posts/3"},
    ]
}


def _client(payload, status=200):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload)))


def test_facebook_requires_read_token(cfg, monkeypatch):
    monkeypatch.delenv("FACEBOOK_READ_TOKEN", raising=False)
    with pytest.raises(collector.FacebookReadError):
        collector.candidates_from_facebook(cfg, {"name": "ОВА", "type": "facebook", "page": "oda"})


def test_facebook_posts_parsed(cfg, monkeypatch):
    monkeypatch.setenv("FACEBOOK_READ_TOKEN", "x")
    cands = collector.candidates_from_facebook(cfg, {"name": "ОВА", "type": "facebook", "page": "oda"}, _client(POSTS))
    assert len(cands) == 2  # допис без тексту пропущено
    assert cands[0]["link"] == "https://adm.dp.gov.ua/news/test-hub"
    assert cands[0]["title"].startswith("Обласна військова адміністрація")
    assert cands[0]["published"] is not None


def test_facebook_api_error(cfg, monkeypatch):
    monkeypatch.setenv("FACEBOOK_READ_TOKEN", "x")
    with pytest.raises(collector.FacebookReadError):
        collector.candidates_from_facebook(cfg, {"name": "ОВА", "type": "facebook", "page": "oda"},
                                           _client({"error": {"code": 10, "message": "no permission"}}, 400))


def test_facebook_source_kind(cfg):
    fb_source = {"name": "ОВА", "type": "facebook", "page": "oda", "kind": "primary"}
    info = collector._source_info(fb_source, "facebook.com", cfg)
    assert info.kind == "primary"
    # посилання на facebook.com з RSS медіа не вважається джерелом
    assert collector._source_info({"name": "Медіа", "type": "rss"}, "facebook.com", cfg) is None


def test_expired_opportunity_not_published(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    did = db.insert_draft(cluster_id=1, item_id=1, pillar="MOZHLYVOSTI", text="t", source_url="https://irf.ua/x",
                          verification_basis={"type": "primary", "urls": []}, confidence=0.9, warnings=[],
                          status="approved", target="tg", tg_status="queued", decided_at=iso(utcnow()),
                          deadline="2020-01-01")
    pub = FakeTelegram()
    publisher.run_queue(s, db, tg_api=pub, fb_api=FakeFacebook(), notify_api=FakeTelegram())
    assert pub.sent == [] and db.get_draft(did)["status"] == "expired"
