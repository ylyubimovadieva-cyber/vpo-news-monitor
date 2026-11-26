"""Зображення до дописів: картка платформи, перевірка зображень першоджерела, надсилання разом із текстом."""
import httpx

from vpo_news import approval, images, publisher
from vpo_news.util import iso, utcnow

from .conftest import FakeTelegram, make_settings

PNG = images.make_card("ДІМ – право на стабільність", "Перевірка")


class Item(dict):
    def keys(self):
        return super().keys()


def _item(kind="primary", image="https://www.pfu.gov.ua/img/a.png"):
    return Item(kind=kind, image_url=image, title="Виплати ВПО", domain="pfu.gov.ua")


class FakeReviewer:
    def __init__(self, **res):
        self.res = {"usable": True, "identifiable_faces": False, "personal_data": False,
                    "third_party_copyright": False, "graphic_content": False, "reason": "інфографіка"} | res
        self.calls = 0

    def review_image(self, data, mime, title, domain):
        self.calls += 1
        return self.res


def _http(status=200, mime="image/png", body=PNG):
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, headers={"content-type": mime}, content=body)))


def test_card_is_png_without_people():
    data = images.make_card("ПАМ'ЯТЬ – право на правду", "Дуже довгий заголовок " * 20)
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 5000


def test_primary_source_image_used_after_review(cfg):
    rv = FakeReviewer()
    res = images.choose(cfg, rv, _item(), "DIM", "текст", http=_http())
    assert res["image_kind"] == "source" and res["image_url"].startswith("https://www.pfu.gov.ua")
    assert rv.calls == 1


def test_faces_or_copyright_fall_back_to_card(cfg):
    for bad in ({"identifiable_faces": True}, {"third_party_copyright": True}, {"personal_data": True},
                {"graphic_content": True}, {"usable": False}):
        res = images.choose(cfg, FakeReviewer(**bad), _item(), "DIM", "текст", http=_http())
        assert res["image_kind"] == "card", bad
        assert "не пройшло перевірку" in res["image_note"]


def test_media_images_never_used(cfg):
    rv = FakeReviewer()
    res = images.choose(cfg, rv, _item(kind="media"), "DIM", "текст", http=_http())
    assert res["image_kind"] == "card" and "авторським правом" in res["image_note"] and rv.calls == 0


def test_no_image_or_not_an_image_gives_card(cfg):
    assert images.choose(cfg, FakeReviewer(), _item(image=None), "DIM", "т")["image_kind"] == "card"
    res = images.choose(cfg, FakeReviewer(), _item(), "DIM", "т", http=_http(mime="text/html", body=b"<html>"))
    assert res["image_kind"] == "card" and "недоступне" in res["image_note"]


def test_image_sent_with_draft_and_published_as_photo(cfg, db):
    s = make_settings(cfg, test_group_chat_id=-200)
    text = "ДІМ – право на стабільність\n\n" + "Текст допису. " * 30 + "\n\nДжерело: https://www.pfu.gov.ua/1/"
    did = db.insert_draft(cluster_id=1, item_id=1, pillar="DIM", text=text, source_url="https://www.pfu.gov.ua/1/",
                          verification_basis={"type": "primary", "urls": [], "domains": ["pfu.gov.ua"]},
                          confidence=0.9, warnings=[], status="to_send",
                          image_kind="card", image_note="власна картка платформи (у першоджерелі немає зображення)")
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    assert api.photos and api.photos[0]["chat_id"] == 111  # зображення координаторці разом із чернеткою
    assert "Зображення (надіслано вище): власна картка" in api.sent[-1]["text"]

    db.update_draft(did, status="approved", target="tg", tg_status="queued", decided_at=iso(utcnow()))
    pub = FakeTelegram()
    publisher.run_queue(s, db, tg_api=pub, notify_api=FakeTelegram())
    assert pub.photos[0]["chat_id"] == -200 and pub.photos[0]["caption"].startswith("[ТЕСТ] ДІМ")
    assert pub.sent == []  # текст іде підписом до фото
