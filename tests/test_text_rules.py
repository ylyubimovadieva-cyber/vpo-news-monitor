from vpo_news import text_rules

HEADER = "ДІМ – право на стабільність"
URL = "https://www.kmu.gov.ua/news/test-yevidnovlennia-rozshyreno"

GOOD_BODY = (
    "Власники зруйнованого житла в громадах, які були під окупацією, тепер можуть отримати компенсацію "
    "за програмою єВідновлення. Уряд ухвалив відповідні зміни. Заяву подають через Дію або в ЦНАП. "
    "Житловий сертифікат дозволяє купити нове житло, зокрема в іншій громаді. Це стосується і внутрішньо "
    "переміщених осіб, чиї домівки знищені. Для багатьох родин це шанс не відкладати рішення про те, де жити далі."
)


def good_text():
    return text_rules.compose(HEADER, GOOD_BODY, URL)


def test_good_text_passes(cfg):
    errors, _ = text_rules.validate(good_text(), HEADER, URL, cfg)
    assert errors == [], errors
    assert 400 <= len(good_text()) <= 700


def test_first_line_is_pillar(cfg):
    errors, _ = text_rules.validate(good_text().replace(HEADER, "ДІМ"), HEADER, URL, cfg)
    assert any("Перший рядок" in e for e in errors)


def test_no_hashtags(cfg):
    errors, _ = text_rules.validate(good_text().replace("Дію", "#Дія"), HEADER, URL, cfg)
    assert "Є хештеги" in errors


def test_url_fragment_is_not_hashtag(cfg):
    url = URL + "#section"
    text = text_rules.compose(HEADER, GOOD_BODY, url)
    errors, _ = text_rules.validate(text, HEADER, url, cfg)
    assert "Є хештеги" not in errors


def test_no_emoji(cfg):
    errors, _ = text_rules.validate(good_text().replace("ЦНАП.", "ЦНАП 🏠."), HEADER, URL, cfg)
    assert "Є емодзі" in errors


def test_no_long_dash(cfg):
    errors, _ = text_rules.validate(good_text().replace("Уряд ухвалив", "Уряд — ухвалив"), HEADER, URL, cfg)
    assert any("довге тире" in e for e in errors)


def test_short_dash_allowed(cfg):
    errors, _ = text_rules.validate(good_text(), HEADER, URL, cfg)
    assert "–" in good_text() and not any("тире" in e for e in errors)


def test_no_exclamation(cfg):
    errors, _ = text_rules.validate(good_text().replace("ЦНАП.", "ЦНАП!"), HEADER, URL, cfg)
    assert "Є знак оклику" in errors


def test_link_required(cfg):
    text = f"{HEADER}\n\n{GOOD_BODY}\n\nДжерело: сайт уряду"
    errors, _ = text_rules.validate(text, HEADER, URL, cfg)
    assert "Немає посилання на джерело" in errors


def test_link_must_be_primary_source(cfg):
    text = text_rules.compose(HEADER, GOOD_BODY, "https://example.com/aggregator")
    errors, _ = text_rules.validate(text, HEADER, URL, cfg)
    assert any("не збігається" in e for e in errors)


def test_length_limits(cfg):
    errors, _ = text_rules.validate(text_rules.compose(HEADER, "Коротко.", URL), HEADER, URL, cfg)
    assert any("Довжина" in e for e in errors)
    errors, _ = text_rules.validate(text_rules.compose(HEADER, GOOD_BODY * 2, URL), HEADER, URL, cfg)
    assert any("Довжина" in e for e in errors)


def test_evaluative_words_only_if_in_source(cfg):
    text = good_text().replace("Уряд ухвалив", "Уряд нарешті ухвалив")
    errors, _ = text_rules.validate(text, HEADER, URL, cfg, source_text="Уряд ухвалив зміни")
    assert any("нарешті" in e for e in errors)
    errors, _ = text_rules.validate(text, HEADER, URL, cfg, source_text="Уряд нарешті ухвалив зміни")
    assert not any("нарешті" in e for e in errors)


def test_no_claim_congress_implements_strategy(cfg):
    text = good_text().replace("Уряд ухвалив відповідні зміни.", "Конгрес ВПО впроваджує Стратегію.")
    errors, _ = text_rules.validate(text, HEADER, URL, cfg)
    assert any("Стратегію" in e for e in errors)


def test_sanitize_fixes_dashes_emoji_hashtags_exclamations():
    raw = "Важлива новина — #ВПО отримають допомогу! 🙂 Деталі: https://site.gov.ua/a#b!"
    clean = text_rules.sanitize(raw)
    assert "—" not in clean and "–" in clean
    assert "#ВПО" not in clean and "ВПО" in clean
    assert "🙂" not in clean
    assert "допомогу." in clean
    assert "https://site.gov.ua/a#b!" in clean  # URL не змінюється


def test_ensure_frame_adds_header_and_link():
    framed = text_rules.ensure_frame("Текст координаторки.", HEADER, URL)
    assert framed.startswith(HEADER + "\n\n")
    assert framed.endswith(URL)


def test_body_length_range_fits_total(cfg):
    lo, hi = text_rules.body_length_range(HEADER, URL, cfg)
    overhead = len(text_rules.compose(HEADER, "", URL))
    assert lo + overhead >= 400 - 2 and hi + overhead <= 700 + 2


def test_unattributed_judgement_warning(cfg):
    from vpo_news.text_rules import unattributed_judgement
    assert unattributed_judgement("Кластер оновив рекомендації. Допомога важлива саме до початку сезону.")
    assert unattributed_judgement("Кластер оновив рекомендації. У кластері наголошують, що допомога важлива.") is None
    assert unattributed_judgement("Програма для вчителів. Заповнити форму потрібно до 11 грудня 2026 року.") is None
    assert unattributed_judgement("Фонд евакуює людей щодня.") is None


def test_number_ranges_get_short_dash():
    from vpo_news.text_rules import sanitize
    assert sanitize("учнів 5-11 класів") == "учнів 5–11 класів"
    assert sanitize("тел. 067-123-45-67") == "тел. 067-123-45-67"
    assert sanitize("https://x.ua/a-1-2") == "https://x.ua/a-1-2"
