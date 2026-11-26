"""Збір новин: RSS там, де він є; інакше – акуратний парсинг сторінки новин.

Дотримуємося robots.txt, робимо паузи між запитами до одного сайту і
приймаємо лише посилання з доменів білого списку.
"""
from __future__ import annotations

import json
import os
import logging
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import feedparser
import httpx

from . import dedupe, verify
from .db import DB
from .util import domain_of, iso, normalize_url, older_than, parse_date

log = logging.getLogger(__name__)

BODY_LIMIT = 6000


# ------------------------------------------------------------ HTML-парсер

class PageParser(HTMLParser):
    """Мінімальний розбір сторінки без сторонніх бібліотек."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.times: list[str] = []
        self.jsonld: list[str] = []
        self.paragraphs: list[str] = []
        self.links: list[str] = []
        self._in_title = False
        self._in_p = 0
        self._buf: list[str] = []
        self._skip = 0
        self._in_jsonld = False
        self._jbuf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "title":
            self._in_title = True
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        elif tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._in_jsonld, self._jbuf = True, []
            else:
                self._skip += 1
        elif tag in ("style", "noscript", "nav", "footer"):
            self._skip += 1
        elif tag == "p":
            self._in_p += 1
            self._buf = []
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "br" and self._in_p:
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            if self._in_jsonld:
                self.jsonld.append("".join(self._jbuf))
                self._in_jsonld = False
            else:
                self._skip = max(0, self._skip - 1)
        elif tag in ("style", "noscript", "nav", "footer"):
            self._skip = max(0, self._skip - 1)
        elif tag == "p" and self._in_p:
            self._in_p -= 1
            text = " ".join("".join(self._buf).split())
            if len(text) >= 40:
                self.paragraphs.append(text)
            self._buf = []

    def handle_data(self, data):
        if self._in_jsonld:
            self._jbuf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._in_p:
            self._buf.append(data)

    # ---- результати
    def published(self) -> datetime | None:
        for key in ("article:published_time", "og:published_time", "datepublished", "pubdate",
                    "publishdate", "date", "dc.date", "dcterms.created"):
            if key in self.meta:
                dt = parse_date(self.meta[key])
                if dt:
                    return dt
        for blob in self.jsonld:
            m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', blob)
            if m:
                dt = parse_date(m.group(1))
                if dt:
                    return dt
        for t in self.times:
            dt = parse_date(t)
            if dt:
                return dt
        return None

    def headline(self) -> str:
        return (self.meta.get("og:title") or self.title or "").strip()

    def text(self) -> str:
        return "\n".join(self.paragraphs)[:BODY_LIMIT]


def strip_html(html: str) -> str:
    p = PageParser()
    p.feed(f"<p>{html or ''}</p>")
    text = " ".join(p.paragraphs) or re.sub(r"<[^>]+>", " ", html or "")
    return " ".join(text.split())


# ---------------------------------------------------------------- HTTP

class Fetcher:
    def __init__(self, cfg: dict, client: httpx.Client | None = None):
        c = cfg.get("collector", {})
        self.delay = float(c.get("request_delay_seconds", 3))
        self.ua = c.get("user_agent", "vpo-news-monitor/1.0")
        self.client = client or httpx.Client(
            timeout=float(c.get("timeout_seconds", 20)),
            follow_redirects=True,
            headers={"User-Agent": self.ua, "Accept-Language": "uk,en;q=0.7"},
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def _pause(self, domain: str) -> None:
        last = self._last.get(domain)
        if last is not None:
            wait = self.delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[domain] = time.monotonic()

    def allowed(self, url: str) -> bool:
        domain = domain_of(url)
        if domain not in self._robots:
            rp = RobotFileParser()
            robots_url = urljoin(url, "/robots.txt")
            try:
                self._pause(domain)
                r = self.client.get(robots_url)
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                elif r.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                log.warning("robots.txt недоступний для %s – сайт пропущено в цьому запуску", domain)
                rp = None
            self._robots[domain] = rp
        rp = self._robots[domain]
        return bool(rp and rp.can_fetch(self.ua, url))

    def get(self, url: str) -> httpx.Response | None:
        if not self.allowed(url):
            log.info("robots.txt забороняє %s", domain_of(url))
            return None
        self._pause(domain_of(url))
        try:
            r = self.client.get(url)
        except httpx.HTTPError as exc:
            log.warning("Помилка запиту до %s: %s", domain_of(url), type(exc).__name__)
            return None
        if r.status_code >= 400:
            log.warning("%s повернув %s", domain_of(url), r.status_code)
            return None
        return r


# --------------------------------------------------------------- джерела

def _entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            return datetime(*tm[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        dt = parse_date(entry.get(key))
        if dt:
            return dt
    return None


def candidates_from_rss(fetcher: Fetcher, source: dict) -> list[dict]:
    r = fetcher.get(source["url"])
    if r is None:
        return []
    feed = feedparser.parse(r.content)
    out = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        summary = strip_html(e.get("summary") or e.get("description") or "")
        out.append({"url": link, "title": strip_html(e.get("title", "")), "published": _entry_date(e), "summary": summary})
    return out


def candidates_from_html(fetcher: Fetcher, source: dict) -> list[dict]:
    r = fetcher.get(source["url"])
    if r is None:
        return []
    p = PageParser()
    p.feed(r.text)
    pattern = re.compile(source["link_regex"])
    seen, out = set(), []
    for href in p.links:
        full = urljoin(str(r.url), href)
        if pattern.search(full) and full not in seen:
            seen.add(full)
            out.append({"url": full, "title": "", "published": None, "summary": ""})
    return out


FACEBOOK_DOMAINS = ("facebook.com", "fb.com", "fb.me")


class FacebookReadError(Exception):
    pass


def candidates_from_facebook(cfg: dict, source: dict, client: httpx.Client | None = None) -> list[dict]:
    """Дописи офіційної сторінки Facebook через Graph API.

    Читання чужих сторінок потребує токена застосунку Meta з доступом
    «Page Public Content Access» (див. README). Без токена джерело пропускається.
    Сторінки facebook.com не парсимо: robots.txt і правила Meta це забороняють.
    """
    token = os.environ.get("FACEBOOK_READ_TOKEN")
    if not token:
        raise FacebookReadError("не задано FACEBOOK_READ_TOKEN")
    version = cfg.get("facebook", {}).get("graph_api_version", "v23.0")
    client = client or httpx.Client(timeout=20)
    try:
        r = client.get(
            f"https://graph.facebook.com/{version}/{source['page']}/posts",
            params={"fields": "message,created_time,permalink_url,attachments{unshimmed_url,url,title}",
                    "limit": 25, "access_token": token},
        )
    except httpx.HTTPError as exc:
        raise FacebookReadError(type(exc).__name__) from None
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        raise FacebookReadError(f"{err.get('code')} {err.get('message', '')[:120]}")
    out = []
    for post in data.get("data", []):
        message = (post.get("message") or "").strip()
        if not message or not post.get("permalink_url"):
            continue
        first_line = message.split("\n", 1)[0]
        link = None
        for att in (post.get("attachments") or {}).get("data", []):
            link = att.get("unshimmed_url") or att.get("url")
            if link:
                break
        out.append({
            "url": post["permalink_url"],
            "link": link,  # посилання з допису, наприклад на новину на сайті установи
            "title": first_line[:160],
            "published": parse_date(post.get("created_time")),
            "summary": message[:1000],
            "body": message[:BODY_LIMIT],
            "no_fetch": True,
        })
    return out


TELEGRAM_DOMAINS = ("t.me", "telegram.me")


class TelegramPreviewParser(HTMLParser):
    """Розбір публічної вебсторінки каналу t.me/s/<канал>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts: list[dict] = []
        self._cur: dict | None = None
        self._text_depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "")
        if tag == "div" and a.get("data-post") and "tgme_widget_message" in cls:
            self._cur = {"post": a["data-post"], "text": "", "time": None, "links": []}
            self.posts.append(self._cur)
            return
        if self._cur is None:
            return
        if tag == "div":
            if self._text_depth:
                self._text_depth += 1
            elif "tgme_widget_message_text" in cls:
                self._text_depth, self._buf = 1, []
        elif tag == "br" and self._text_depth:
            self._buf.append("\n")
        elif tag == "time" and a.get("datetime") and not self._cur["time"]:
            self._cur["time"] = a["datetime"]
        elif tag == "a" and a.get("href", "").startswith("http"):
            if self._text_depth or "tgme_widget_message_link_preview" in cls:
                self._cur["links"].append(a["href"])

    def handle_endtag(self, tag):
        if tag == "div" and self._text_depth:
            self._text_depth -= 1
            if self._text_depth == 0 and self._cur is not None and not self._cur["text"]:
                self._cur["text"] = "".join(self._buf).strip()

    def handle_data(self, data):
        if self._text_depth:
            self._buf.append(data)


def allowed_telegram_channels(cfg: dict) -> dict[str, dict]:
    return {c["channel"].lower(): c for c in cfg.get("telegram_channels", [])}


def candidates_from_telegram(fetcher: Fetcher, cfg: dict, source: dict) -> list[dict]:
    """Дописи офіційного іменного каналу з білого списку telegram_channels."""
    channel = source["channel"].lstrip("@")
    if channel.lower() not in allowed_telegram_channels(cfg):
        log.warning("Канал %s не в списку telegram_channels – пропущено", channel)
        return []
    r = fetcher.get(f"https://t.me/s/{channel}")
    if r is None:
        return []
    p = TelegramPreviewParser()
    p.feed(r.text)
    out = []
    for post in p.posts:
        text = post["text"]
        if not text:
            continue
        external = [u for u in post["links"] if domain_of(u) not in TELEGRAM_DOMAINS]
        out.append({
            "url": f"https://t.me/{post['post']}",
            "link": external[0] if external else None,
            "title": text.split("\n", 1)[0][:160],
            "published": parse_date(post["time"]),
            "summary": text[:1000],
            "body": text[:BODY_LIMIT],
            "no_fetch": True,
        })
    return out


def _source_info(source: dict, domain: str, cfg: dict) -> verify.SourceInfo | None:
    """Тип джерела: для дописів Facebook і дозволених Telegram-каналів – з налаштувань, інакше – за доменом."""
    if domain in TELEGRAM_DOMAINS:
        # Telegram загалом заборонений; виняток – офіційні іменні канали зі списку.
        ch = allowed_telegram_channels(cfg).get(str(source.get("channel", "")).lstrip("@").lower())
        if source.get("type") == "telegram" and ch:
            return verify.SourceInfo(ch.get("kind", "primary"), ch.get("owner") or ch["channel"])
        return None
    if verify.is_blocked(domain, cfg):
        return None
    if any(domain == d or domain.endswith("." + d) for d in FACEBOOK_DOMAINS):
        if source.get("type") == "facebook" or source.get("kind"):
            return verify.SourceInfo(source.get("kind", "primary"), source.get("owner") or source.get("page") or source["name"])
        return None
    return verify.classify_domain(domain, cfg)


def fetch_article(fetcher: Fetcher, url: str) -> tuple[str, datetime | None, str, str, str | None] | None:
    """Повертає (заголовок, дата, текст, кінцевий URL, зображення og:image) або None."""
    r = fetcher.get(url)
    if r is None or "html" not in r.headers.get("content-type", "html"):
        return None
    p = PageParser()
    p.feed(r.text)
    image = p.meta.get("og:image") or p.meta.get("twitter:image")
    if image:
        image = urljoin(str(r.url), image.strip())
        if not image.startswith("https://"):
            image = None
    return p.headline(), p.published(), p.text(), str(r.url), image


def _matches_any(text: str, patterns) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def is_relevant_candidate(text: str, cfg: dict) -> bool:
    return _matches_any(text, cfg.get("relevance_keywords", []))


def collect(cfg: dict, db: DB, fetcher: Fetcher | None = None) -> dict:
    """Збирає нові новини з усіх увімкнених джерел і записує їх у базу."""
    fetcher = fetcher or Fetcher(cfg)
    ccfg = cfg.get("collector", {})
    max_age = float(ccfg.get("max_age_hours", 72))
    limit = int(ccfg.get("max_items_per_source", 30))
    want_body = bool(ccfg.get("fetch_article_text", True))
    stats = {"seen": 0, "new": 0, "candidates": 0, "skipped_old": 0, "skipped_domain": 0}
    fb_warned = False

    for source in cfg.get("sources", []):
        if not source.get("enabled", True):
            continue
        stype = source.get("type", "rss")
        try:
            if stype == "html":
                cands = candidates_from_html(fetcher, source)
            elif stype == "facebook":
                cands = candidates_from_facebook(cfg, source)
            elif stype == "telegram":
                cands = candidates_from_telegram(fetcher, cfg, source)
            else:
                cands = candidates_from_rss(fetcher, source)
        except FacebookReadError as exc:
            if not fb_warned:
                log.warning("Сторінки Facebook пропущено: %s", exc)
                fb_warned = True
            continue
        except Exception:  # одне зламане джерело не має зупиняти решту
            log.exception("Не вдалося обробити джерело %s", source.get("name"))
            continue
        log.info("%s: %d записів", source.get("name"), len(cands))

        for c in cands[:limit]:
            stats["seen"] += 1
            url = c["url"]
            # Якщо допис у Facebook посилається на новину на сайті з білого списку –
            # беремо саму новину: це першоджерело і так простіше знайти дублікати.
            link = c.get("link")
            if link and verify.classify_domain(domain_of(link), cfg) is not None:
                url = link
                c = {**c, "no_fetch": False}
            url_norm = normalize_url(url)
            if db.has_url(url_norm):
                continue
            domain = domain_of(url)
            info = _source_info(source, domain, cfg)
            if info is None:
                stats["skipped_domain"] += 1
                continue

            title, published, summary, body = c["title"], c["published"], c["summary"], c.get("body") or ""
            # Для загальних стрічок міжнародних організацій беремо лише записи про Україну.
            req = source.get("require_keywords")
            if req and title and not _matches_any(f"{title}\n{summary}\n{url}", req):
                continue
            need_page = published is None or not title
            if published and older_than(published, max_age):
                stats["skipped_old"] += 1
                continue
            if not need_page and not is_relevant_candidate(f"{title}\n{summary}", cfg):
                db.insert_item(url=url, url_norm=url_norm, title=title, title_key=dedupe.title_key(title),
                               source_name=source["name"], domain=domain, kind=info.kind, owner=info.owner,
                               published_at=iso(published), summary=summary[:1000], status="irrelevant_prefilter")
                stats["new"] += 1
                continue

            image_url = None
            if (need_page or want_body) and not c.get("no_fetch"):
                art = fetch_article(fetcher, url)
                if art:
                    a_title, a_pub, a_text, final_url, a_image = art
                    # Після переадресації домен має лишатися в білому списку.
                    if domain_of(final_url) != domain and verify.classify_domain(domain_of(final_url), cfg) is None:
                        stats["skipped_domain"] += 1
                        continue
                    title = title or a_title
                    published = published or a_pub
                    body = a_text
                    image_url = a_image
            if not title or published is None:
                continue  # без дати не можемо перевірити свіжість – пропускаємо
            if older_than(published, max_age):
                stats["skipped_old"] += 1
                continue

            full_text = f"{title}\n{summary}\n{body}"
            status, reason = "new", None
            blocked = verify.has_blocked_attribution(full_text, cfg)
            if blocked:
                status, reason = "blocked", f"посилання на заборонене джерело ({blocked})"
            elif not is_relevant_candidate(full_text, cfg):
                status = "irrelevant_prefilter"
            db.insert_item(url=url, url_norm=url_norm, title=title, title_key=dedupe.title_key(title),
                           source_name=source["name"], domain=domain, kind=info.kind, owner=info.owner,
                           published_at=iso(published), summary=summary[:1000], body=body or None,
                           status=status, reason=reason, image_url=image_url)
            stats["new"] += 1
            if status == "new":
                stats["candidates"] += 1
    return stats


def check_sources(cfg: dict) -> list[dict]:
    """Діагностика: чи відповідає кожне джерело і скільки записів повертає."""
    fetcher = Fetcher(cfg)
    report = []
    for s in cfg.get("sources", []):
        row = {"name": s.get("name"), "url": s.get("url"), "enabled": s.get("enabled", True)}
        if not row["enabled"]:
            report.append(row)
            continue
        try:
            if s.get("type") == "html":
                cands = candidates_from_html(fetcher, s)
            elif s.get("type") == "facebook":
                cands = candidates_from_facebook(cfg, s)
            elif s.get("type") == "telegram":
                cands = candidates_from_telegram(fetcher, cfg, s)
            else:
                cands = candidates_from_rss(fetcher, s)
            row["items"] = len(cands)
            row["whitelisted"] = sum(1 for c in cands if _source_info(s, domain_of(c["url"]), cfg))
            row["ok"] = len(cands) > 0
        except FacebookReadError as exc:
            row["ok"], row["error"] = False, f"Facebook: {exc}"
        except Exception as exc:  # noqa: BLE001
            row["ok"], row["error"] = False, type(exc).__name__
        report.append(row)
    return report


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
