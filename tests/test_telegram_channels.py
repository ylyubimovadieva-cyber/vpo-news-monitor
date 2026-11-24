"""Офіційні іменні Telegram-канали: лише зі списку, анонімні канали заборонені."""
from vpo_news import collector

PREVIEW = """
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="dmytro_lubinetzs/101">
  <div class="tgme_widget_message_text js-message_text" dir="auto"><b>Звернувся до уряду щодо виплат ВПО</b><br/>Деталі на сайті
    <a href="https://ombudsman.gov.ua/news_details/test-vyplaty">ombudsman.gov.ua</a></div>
  <div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/dmytro_lubinetzs/101"><time datetime="2026-11-18T10:00:00+00:00">10:00</time></a></div>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="dmytro_lubinetzs/102">
  <div class="tgme_widget_message_text js-message_text" dir="auto">Зустрівся з переселенцями в Харкові.<br/>Обговорили житло.</div>
  <a class="tgme_widget_message_date" href="https://t.me/dmytro_lubinetzs/102"><time datetime="2026-11-18T12:00:00+00:00">12:00</time></a>
</div></div>
<div class="tgme_widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="dmytro_lubinetzs/103">
  <div class="tgme_widget_message_photo_wrap"></div>
</div></div>
"""


class FakeFetcher:
    def __init__(self, text):
        self.text, self.urls = text, []

    def get(self, url):
        self.urls.append(url)
        return type("R", (), {"text": self.text})()


def test_preview_parsed(cfg):
    f = FakeFetcher(PREVIEW)
    cands = collector.candidates_from_telegram(f, cfg, {"name": "Лубінець", "type": "telegram", "channel": "dmytro_lubinetzs"})
    assert f.urls == ["https://t.me/s/dmytro_lubinetzs"]
    assert len(cands) == 2  # допис без тексту (лише фото) пропущено
    first, second = cands
    assert first["url"] == "https://t.me/dmytro_lubinetzs/101"
    assert first["title"] == "Звернувся до уряду щодо виплат ВПО"
    assert first["link"] == "https://ombudsman.gov.ua/news_details/test-vyplaty"
    assert "Деталі на сайті" in first["body"]
    assert second["link"] is None and second["published"] is not None
    assert second["body"].split("\n") == ["Зустрівся з переселенцями в Харкові.", "Обговорили житло."]


def test_channel_must_be_allowlisted(cfg):
    f = FakeFetcher(PREVIEW)
    assert collector.candidates_from_telegram(f, cfg, {"name": "X", "type": "telegram", "channel": "anon_insider"}) == []
    assert f.urls == []  # навіть не звертаємося до каналу поза списком


def test_telegram_domain_only_for_allowlisted_channel(cfg):
    ok = collector._source_info({"name": "Л", "type": "telegram", "channel": "dmytro_lubinetzs"}, "t.me", cfg)
    assert ok and ok.kind == "primary" and ok.owner == "ombudsman"
    assert collector._source_info({"name": "X", "type": "telegram", "channel": "anon_insider"}, "t.me", cfg) is None
    # посилання на t.me з RSS медіа – як і раніше, не джерело
    assert collector._source_info({"name": "Медіа", "type": "rss"}, "t.me", cfg) is None
