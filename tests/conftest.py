import json
from datetime import timedelta
from pathlib import Path

import pytest

from vpo_news import dedupe, verify
from vpo_news.config import Settings, load_config
from vpo_news.db import DB
from vpo_news.util import domain_of, iso, normalize_url, utcnow

FIXTURES = Path(__file__).parent / "fixtures" / "news_examples.json"


def load_examples():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["examples"]


@pytest.fixture
def cfg():
    c = load_config()
    c["telegram"]["coordinator_chat_id"] = 111
    c["telegram"]["group_chat_id"] = -100500
    c["telegram"]["topics"] = {"DIM": 2, "HOLOS": 3, "TURBOTA": 4, "POVERNENNIA": 5, "PAMIAT": 6}
    c["facebook"]["page_id"] = "123"
    c["telegram"]["min_interval_minutes"] = 0  # інтервал перевіряє окремий тест
    c["collector"]["request_delay_seconds"] = 0
    return c


@pytest.fixture
def db():
    d = DB(":memory:")
    yield d
    d.close()


def make_settings(cfg, **kw) -> Settings:
    s = Settings(cfg=cfg, coordinator_chat_id=111, approval_token="TEST", publish_token="TEST")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def insert_example_items(db, cfg, example, status="new"):
    """Записує матеріали прикладу в базу так, як це зробив би збирач. Повертає id або None (заблоковано)."""
    ids = []
    for it in example["items"]:
        domain = domain_of(it["url"])
        info = verify.classify_domain(domain, cfg)
        if info is None:
            ids.append(None)
            continue
        ids.append(db.insert_item(
            url=it["url"], url_norm=normalize_url(it["url"]), title=it["title"],
            title_key=dedupe.title_key(it["title"]), source_name=it["source_name"], domain=domain,
            kind=info.kind, owner=info.owner, published_at=iso(utcnow() - timedelta(hours=it["hours_ago"])),
            summary=it["body"][:300], body=it["body"], status=status,
        ))
    return ids


class FakeTelegram:
    """Імітація Telegram API: запам'ятовує виклики, нічого не надсилає."""

    def __init__(self, updates=None):
        self.sent, self.edited, self.answers = [], [], []
        self.updates = list(updates or [])
        self._mid = 1000

    def send_message(self, chat_id, text, reply_markup=None, message_thread_id=None, disable_preview=False):
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup,
                          "thread": message_thread_id, "message_id": self._mid})
        return {"message_id": self._mid}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {}

    def answer_callback(self, callback_id, text=None):
        self.answers.append(text)
        return True

    def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        out = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        self.updates = []
        return out


class FakeFacebook:
    def __init__(self):
        self.posts = []

    def post(self, message, link):
        self.posts.append((message, link))
        return f"123_{len(self.posts)}"
