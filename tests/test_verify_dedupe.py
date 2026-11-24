from vpo_news import dedupe, verify
from vpo_news.util import normalize_url


def test_primary_domains(cfg):
    for d in ("kmu.gov.ua", "pfu.gov.ua", "adm.dp.gov.ua", "zakon.rada.gov.ua", "reliefweb.int", "ukraine.iom.int", "irf.ua"):
        info = verify.classify_domain(d, cfg)
        assert info and info.kind == "primary", d


def test_media_domains_and_owners(cfg):
    up = verify.classify_domain("pravda.com.ua", cfg)
    euro = verify.classify_domain("eurointegration.com.ua", cfg)
    assert up.kind == "media" and up.owner == euro.owner == "up"


def test_blocked_and_unknown_domains(cfg):
    for d in ("ria.ru", "news.ria.ru", "t.me", "news.google.com", "example.com", "lenta.ru"):
        assert verify.classify_domain(d, cfg) is None, d


def _row(url, kind, owner, domain, published="2026-11-18T10:00:00+00:00"):
    return {"url": url, "kind": kind, "owner": owner, "domain": domain, "published_at": published}


def test_verified_by_primary():
    ok, b = verify.verify_items([_row("https://kmu.gov.ua/n", "primary", "kmu.gov.ua", "kmu.gov.ua")])
    assert ok and b["type"] == "primary"


def test_one_media_not_enough():
    ok, _ = verify.verify_items([_row("https://lb.ua/a", "media", "lb", "lb.ua")])
    assert not ok


def test_two_media_same_owner_not_enough():
    ok, _ = verify.verify_items([
        _row("https://pravda.com.ua/a", "media", "up", "pravda.com.ua"),
        _row("https://eurointegration.com.ua/b", "media", "up", "eurointegration.com.ua"),
    ])
    assert not ok


def test_two_independent_media():
    ok, b = verify.verify_items([
        _row("https://lb.ua/a", "media", "lb", "lb.ua"),
        _row("https://suspilne.media/b", "media", "suspilne", "suspilne.media"),
    ])
    assert ok and b["type"] == "two_media" and len(b["urls"]) == 2


def test_source_url_prefers_primary():
    items = [
        _row("https://lb.ua/a", "media", "lb", "lb.ua", "2026-11-18T08:00:00+00:00"),
        _row("https://kmu.gov.ua/n", "primary", "kmu.gov.ua", "kmu.gov.ua", "2026-11-18T09:00:00+00:00"),
    ]
    assert verify.pick_source_url(items) == "https://kmu.gov.ua/n"


def test_blocked_attribution(cfg):
    assert verify.has_blocked_attribution("Як повідомляє ТАСС, ...", cfg)
    assert not verify.has_blocked_attribution("Як повідомляє Укрінформ, ...", cfg)


def test_normalize_url_strips_tracking():
    a = normalize_url("https://www.Suspilne.media/news/123/?utm_source=fb&utm_medium=x#top")
    b = normalize_url("http://suspilne.media/news/123")
    assert a == b


def test_similar_titles_cluster():
    a = "У Дніпрі відкрили нові місця тимчасового проживання для переселенців"
    b = "Дніпро: для ВПО облаштували гуртожиток на 120 місць тимчасового проживання"
    c = "Збірна України з футболу зіграє товариський матч у березні"
    assert dedupe.title_similarity(a, b) > dedupe.title_similarity(a, c)
    assert dedupe.event_similarity(a, "", b, "") >= 0.25
    assert dedupe.event_similarity(a, "", c, "") < 0.25
