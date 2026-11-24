"""Дрібні допоміжні функції: час, URL, логування без секретів."""
from __future__ import annotations

import email.utils
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")

_TRACKING_EXACT = {"fbclid", "gclid", "yclid", "igshid", "ref", "ref_src", "mc_cid", "mc_eid", "_ga"}


# --------------------------------------------------------------------- час

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def kyiv_date(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(KYIV).date().isoformat()


def fmt_kyiv(dt: datetime) -> str:
    return dt.astimezone(KYIV).strftime("%d.%m %H:%M")


_DMY = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ ,]+(\d{1,2}):(\d{2}))?")


def parse_date(value: str | None) -> datetime | None:
    """Розбирає дату з RSS/HTML. Дата без часового поясу вважається київською."""
    if not value:
        return None
    value = value.strip()
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            m = _DMY.search(value)
            if m:
                d, mo, y, h, mi = m.groups()
                try:
                    dt = datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0))
                except ValueError:
                    dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KYIV)
    return dt.astimezone(timezone.utc)


def older_than(dt: datetime, hours: float, now: datetime | None = None) -> bool:
    return (now or utcnow()) - dt > timedelta(hours=hours)


# --------------------------------------------------------------------- URL

def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def domain_matches(domain: str, patterns) -> bool:
    """Чи збігається домен із будь-яким шаблоном (точно або як субдомен)."""
    domain = domain.lower()
    for p in patterns:
        p = p.lower().strip(".")
        if domain == p or domain.endswith("." + p):
            return True
    return False


def normalize_url(url: str) -> str:
    """Нормалізований URL для пошуку дублікатів: без www, utm-міток, фрагмента і кінцевого '/'."""
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_EXACT
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, urlencode(sorted(query)), ""))


# --------------------------------------------------------------- логування

_SECRET_PATTERNS = [
    re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}"),           # токен Telegram у URL
    re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"),           # токен Telegram окремо
    re.compile(r"(access_token=)[^&\s]+"),               # токен Facebook
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),                # ключ Anthropic
]


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in _SECRET_PATTERNS:
            msg = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "***", msg)
        record.msg, record.args = msg, None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx на рівні INFO пише повні URL запитів, а URL Telegram містить токен.
    for noisy in ("httpx", "httpcore", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
