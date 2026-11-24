"""Правила тексту допису: очищення і перевірка.

Помилки (errors) блокують допис; попередження (warnings) лише показуються
координаторці під час погодження.
"""
from __future__ import annotations

import re
from collections import Counter

URL_RE = re.compile(r"https?://[^\s<>\"']+")
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # більшість емодзі і піктограм
    "\U00002600-\U000027BF"   # різні символи і дінгбати
    "\U0001F1E6-\U0001F1FF"   # прапори
    "\U00002B00-\U00002BFF"   # стрілки, зірки
    "\U0000FE0F\U0000200D\U000020E3"
    "\U00002300-\U000023FF"
    "]"
)
LONG_DASHES = "—―"           # довге тире і горизонтальна риска
HASHTAG_RE = re.compile(r"(?<![\w&/])#\w+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+?380|\b0)\s?\(?\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b")

CLAIM_PATTERNS = [
    re.compile(r"(конгрес|платформ)\w*[^.\n]{0,80}(викону|впроваджу|реалізу)\w*[^.\n]{0,40}стратегі", re.IGNORECASE),
    re.compile(r"стратегі\w*[^.\n]{0,60}(викону|впроваджу|реалізу)\w*[^.\n]{0,40}(конгрес|платформ)", re.IGNORECASE),
]


def _outside_urls(text: str, fn) -> str:
    """Застосовує fn лише до частин тексту поза URL."""
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def _strip_urls(text: str) -> str:
    return URL_RE.sub(" ", text)


def sanitize(text: str) -> str:
    """Безпечні автоматичні виправлення: довге тире → коротке, без емодзі, хештегів і знаків оклику."""

    def fix(part: str) -> str:
        for d in LONG_DASHES:
            part = part.replace(d, "–")
        part = EMOJI_RE.sub("", part)
        part = HASHTAG_RE.sub(lambda m: m.group(0)[1:], part)
        part = part.replace("?!", "?").replace("!?", "?")
        part = re.sub(r"!+", ".", part)
        part = re.sub(r"\.{2,}(?!\.)", ".", part) if "..." not in part else part
        return part

    text = _outside_urls(text.replace("\r\n", "\n"), fix)
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compose(header: str, body: str, source_url: str, prefix: str = "Джерело: ") -> str:
    return f"{header}\n\n{body.strip()}\n\n{prefix}{source_url}"


def body_length_range(header: str, source_url: str, cfg: dict) -> tuple[int, int]:
    rules = cfg.get("text_rules", {})
    prefix = rules.get("source_prefix", "Джерело: ")
    overhead = len(header) + 4 + len(prefix) + len(source_url)
    lo = int(rules.get("min_length", 400)) - overhead
    hi = int(rules.get("max_length", 700)) - overhead
    return max(lo, 150), max(hi, 250)


def ensure_frame(text: str, header: str, source_url: str, prefix: str = "Джерело: ") -> str:
    """Для тексту координаторки: додає назву стовпа і посилання, якщо їх немає."""
    text = text.strip()
    if text.split("\n", 1)[0].strip() != header:
        text = f"{header}\n\n{text}"
    if source_url not in text:
        text = f"{text}\n\n{prefix}{source_url}"
    return text


def validate(text: str, header: str, source_url: str, cfg: dict, source_text: str = "") -> tuple[list[str], list[str]]:
    rules = cfg.get("text_rules", {})
    errors: list[str] = []
    warnings: list[str] = []
    plain = _strip_urls(text)

    first_line = text.split("\n", 1)[0].strip()
    if first_line != header:
        errors.append(f"Перший рядок має бути назвою стовпа: «{header}»")

    lo, hi = int(rules.get("min_length", 400)), int(rules.get("max_length", 700))
    if not lo <= len(text) <= hi:
        errors.append(f"Довжина {len(text)} символів, а має бути від {lo} до {hi}")

    if not URL_RE.search(text):
        errors.append("Немає посилання на джерело")
    elif source_url and source_url not in text:
        errors.append("Посилання в тексті не збігається з посиланням на першоджерело")

    if HASHTAG_RE.search(plain):
        errors.append("Є хештеги")
    if EMOJI_RE.search(plain):
        errors.append("Є емодзі")
    if any(d in plain for d in LONG_DASHES):
        errors.append("Є довге тире (дозволене лише коротке «–»)")
    if "!" in plain:
        errors.append("Є знак оклику")

    low, src_low = plain.lower(), (source_text or "").lower()
    for w in rules.get("evaluative_words", []):
        if w.lower() in low and w.lower() not in src_low:
            errors.append(f"Оцінне слово «{w}…», якого немає в першоджерелі")

    for pat in CLAIM_PATTERNS:
        if pat.search(plain):
            errors.append("Не можна писати, що Конгрес чи платформа виконує або впроваджує Стратегію")
            break

    if EMAIL_RE.search(plain):
        warnings.append("Є адреса e-mail – перевірте, що це не персональні дані")
    if PHONE_RE.search(plain):
        warnings.append("Є номер телефону – перевірте, що це офіційна гаряча лінія, а не особистий номер")

    for w in rules.get("bureaucratese", []):
        if w.lower() in low:
            warnings.append(f"Канцеляризм: «{w.strip()}»")

    words = [w for w in re.findall(r"[а-яіїєґa-z']{6,}", low)]
    for word, n in Counter(words).most_common(3):
        if n >= 4:
            warnings.append(f"Слово «{word}» повторюється {n} рази")

    sentences = [s.strip() for s in re.split(r"(?<=[.?])\s+", plain) if len(s.strip()) > 20]
    if len(sentences) != len(set(sentences)):
        warnings.append("Є повтор речення")

    return errors, warnings
