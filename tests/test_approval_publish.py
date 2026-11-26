from datetime import timedelta

from vpo_news import approval, publisher
from vpo_news.util import iso, utcnow

from .conftest import FakeFacebook, FakeTelegram, make_settings

TEXT = "ТУРБОТА – право на турботу\n\n" + "Текст допису. " * 30 + "\n\nДжерело: https://www.pfu.gov.ua/1-test/"


def _draft(db, pillar="TURBOTA", cluster=1, status="to_send", **kw):
    return db.insert_draft(cluster_id=cluster, item_id=cluster, pillar=pillar, text=TEXT,
                           source_url="https://www.pfu.gov.ua/1-test/",
                           verification_basis={"type": "primary", "urls": ["https://www.pfu.gov.ua/1-test/"], "domains": ["pfu.gov.ua"]},
                           confidence=0.9, warnings=[], status=status, **kw)


def _cb(update_id, data, message_id, user=111):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "from": {"id": user}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": user}}}}


def _msg(update_id, text, user=111):
    return {"update_id": update_id, "message": {"message_id": 5, "from": {"id": user}, "chat": {"id": user}, "text": text}}


def test_send_pending_has_four_buttons(cfg, db):
    cfg["linkedin"]["enabled"] = False
    s = make_settings(cfg)
    _draft(db)
    api = FakeTelegram()
    assert approval.send_pending(s, db, api) == 1
    m = api.sent[0]
    assert m["chat_id"] == 111
    labels = [b["text"] for row in m["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == ["Опублікувати в Telegram", "Опублікувати в Telegram і Facebook", "Редагувати", "Відхилити"]
    assert "Підстава підтвердження: першоджерело – pfu.gov.ua" in m["text"]


def test_linkedin_button_when_enabled(cfg, db):
    s = make_settings(cfg)
    _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    labels = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == ["Опублікувати в Telegram", "Опублікувати в Telegram і Facebook",
                      "Опублікувати в Telegram, Facebook і LinkedIn", "Редагувати", "Відхилити"]


class FakeLinkedIn:
    def __init__(self):
        self.posts = []

    def post(self, message, link):
        self.posts.append((message, link))
        return f"urn:li:share:{len(self.posts)}"


def test_approve_all_posts_everywhere_live(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True, facebook_token="fb", linkedin_token="li")
    did = _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    api.updates = [_cb(1, f"all:{did}", db.get_draft(did)["approval_message_id"])]
    approval.process_updates(s, db, api)
    d = db.get_draft(did)
    assert d["target"] == "all" and d["fb_status"] == "queued" and d["li_status"] == "queued"
    pub, fb, li = FakeTelegram(), FakeFacebook(), FakeLinkedIn()
    publisher.run_queue(s, db, tg_api=pub, fb_api=fb, notify_api=FakeTelegram(), li_api=li)
    assert pub.sent and fb.posts and li.posts[0][1] == "https://www.pfu.gov.ua/1-test/"
    d = db.get_draft(did)
    assert d["li_status"] == "published" and d["status"] == "published"


def test_linkedin_only_after_its_button_and_never_in_dry_run(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    _draft(db, status="approved", target="tgfb", tg_status="queued", fb_status="queued", decided_at=iso(utcnow()))
    li = FakeLinkedIn()
    publisher.run_queue(s, db, tg_api=FakeTelegram(), fb_api=FakeFacebook(), notify_api=FakeTelegram(), li_api=li)
    assert li.posts == []

    s2 = make_settings(cfg, dry_run=True, test_group_chat_id=-200)
    did = _draft(db, cluster=2, status="approved", target="all", tg_status="queued", fb_status="queued",
                 li_status="queued", decided_at=iso(utcnow()))
    li2 = FakeLinkedIn()
    publisher.run_queue(s2, db, tg_api=FakeTelegram(), fb_api=FakeFacebook(), notify_api=FakeTelegram(), li_api=li2)
    assert li2.posts == [] and db.get_draft(did)["li_status"] == "dry_published"


def test_linkedin_escapes_reserved_characters():
    import httpx

    from vpo_news.linkedin import LinkedInAPI, escape_commentary
    assert escape_commentary("Програма (ВПО) #1 @x") == "Програма \\(ВПО\\) \\#1 \\@x"
    seen = {}

    def handler(req):
        import json
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7"})

    api = LinkedInAPI("123", "tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.post("Текст (тест)", "https://x.gov.ua/a") == "urn:li:share:7"
    assert seen["body"]["author"] == "urn:li:organization:123"
    assert seen["body"]["commentary"].endswith("https://x.gov.ua/a") and "\\(тест\\)" in seen["body"]["commentary"]


def test_facebook_button_hidden_when_disabled(cfg, db):
    cfg["facebook"]["enabled"] = False
    s = make_settings(cfg)
    _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    labels = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert "Опублікувати в Telegram і Facebook" not in labels


def test_nothing_published_without_approval(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    pub = FakeTelegram()
    stats = publisher.run_queue(s, db, tg_api=pub, fb_api=FakeFacebook(), notify_api=FakeTelegram())
    assert stats["tg"] == 0 and pub.sent == []


def test_strangers_are_ignored(cfg, db):
    s = make_settings(cfg)
    did = _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    mid = db.get_draft(did)["approval_message_id"]
    api.updates = [_cb(1, f"tg:{did}", mid, user=999), _msg(2, "Опублікуй", user=999)]
    approval.process_updates(s, db, api)
    assert db.get_draft(did)["status"] == "awaiting"
    assert len(api.sent) == 1  # чужим бот не відповідає


def test_approve_tg_only_and_publish_to_topic_live(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    did = _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    api.updates = [_cb(1, f"tg:{did}", db.get_draft(did)["approval_message_id"])]
    approval.process_updates(s, db, api)
    d = db.get_draft(did)
    assert d["status"] == "approved" and d["target"] == "tg" and d["fb_status"] == "none"

    pub, fb = FakeTelegram(), FakeFacebook()
    publisher.run_queue(s, db, tg_api=pub, fb_api=fb, notify_api=FakeTelegram())
    assert pub.sent[0]["chat_id"] == -100500 and pub.sent[0]["thread"] == 4
    assert fb.posts == []  # Facebook – лише після кнопки з Facebook
    assert db.get_draft(did)["status"] == "published"


def test_approve_tgfb_posts_facebook_with_link(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True, facebook_token="fb")
    did = _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    api.updates = [_cb(1, f"tgfb:{did}", db.get_draft(did)["approval_message_id"])]
    approval.process_updates(s, db, api)
    fb = FakeFacebook()
    publisher.run_queue(s, db, tg_api=FakeTelegram(), fb_api=fb, notify_api=FakeTelegram())
    assert fb.posts and fb.posts[0][1] == "https://www.pfu.gov.ua/1-test/"


def test_dry_run_never_touches_live_channels(cfg, db):
    s = make_settings(cfg, dry_run=True, live_confirmed=True)  # LIVE_CONFIRMED без DRY_RUN=false не діє
    assert s.publish_mode == "log"
    did = _draft(db, status="approved", target="tgfb", tg_status="queued", fb_status="queued", decided_at=iso(utcnow()))
    pub, fb = FakeTelegram(), FakeFacebook()
    publisher.run_queue(s, db, tg_api=pub, fb_api=fb, notify_api=FakeTelegram())
    assert pub.sent == [] and fb.posts == []
    assert db.get_draft(did)["tg_status"] == "dry_published"

    s2 = make_settings(cfg, dry_run=True, test_group_chat_id=-200)
    did2 = _draft(db, cluster=2, status="approved", target="tg", tg_status="queued", decided_at=iso(utcnow()))
    pub2 = FakeTelegram()
    publisher.run_queue(s2, db, tg_api=pub2, fb_api=FakeFacebook(), notify_api=FakeTelegram())
    assert pub2.sent[0]["chat_id"] == -200 and pub2.sent[0]["text"].startswith("[ТЕСТ]")
    assert db.get_draft(did2)["tg_status"] == "dry_published"


def test_edit_flow_publishes_edited_text(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    did = _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    old_mid = db.get_draft(did)["approval_message_id"]
    new_body = ("Пенсійний фонд почне виплату допомоги на проживання за листопад з 20 числа. Гроші надійдуть на "
                "банківські рахунки тих, кому допомогу продовжили після оцінювання потреб. Якщо виплата не "
                "надійшла, варто звернутися до сервісного центру фонду або написати через вебпортал. "
                "Окремої заяви для цього подавати не потрібно – усе відбувається автоматично.")
    api.updates = [_cb(1, f"edit:{did}", old_mid), _msg(2, "Закоротко!"), _msg(3, new_body)]
    approval.process_updates(s, db, api)
    d = db.get_draft(did)
    assert d["status"] == "awaiting" and d["edited"] == 1
    assert d["text"].startswith("ТУРБОТА – право на турботу\n\n") and d["text"].endswith("https://www.pfu.gov.ua/1-test/")
    assert any("не відповідає правилам" in m["text"] for m in api.sent)  # перша спроба відхилена

    # стара кнопка більше не діє, нова – публікує саме виправлений текст
    api.updates = [_cb(4, f"tg:{did}", old_mid)]
    approval.process_updates(s, db, api)
    assert db.get_draft(did)["status"] == "awaiting"
    api.updates = [_cb(5, f"tg:{did}", db.get_draft(did)["approval_message_id"])]
    approval.process_updates(s, db, api)
    pub = FakeTelegram()
    publisher.run_queue(s, db, tg_api=pub, fb_api=FakeFacebook(), notify_api=FakeTelegram())
    assert pub.sent[0]["text"] == d["text"]


def test_reject(cfg, db):
    s = make_settings(cfg)
    did = _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    api.updates = [_cb(1, f"rej:{did}", db.get_draft(did)["approval_message_id"])]
    approval.process_updates(s, db, api)
    assert db.get_draft(did)["status"] == "rejected"


def test_expire_after_48h(cfg, db):
    s = make_settings(cfg)
    did = _draft(db, status="awaiting", sent_at=iso(utcnow() - timedelta(hours=49)), approval_message_id=7)
    keep = _draft(db, cluster=2, status="awaiting", sent_at=iso(utcnow() - timedelta(hours=47)), approval_message_id=8)
    api = FakeTelegram()
    assert approval.expire_drafts(s, db, api) == 1
    assert db.get_draft(did)["status"] == "expired" and db.get_draft(keep)["status"] == "awaiting"


def test_daily_limits(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    now = iso(utcnow())
    for n in range(5):
        _draft(db, cluster=10 + n, status="approved", target="tgfb", tg_status="queued", fb_status="queued", decided_at=now)
    pub, fb = FakeTelegram(), FakeFacebook()
    stats = publisher.run_queue(s, db, tg_api=pub, fb_api=fb, notify_api=FakeTelegram())
    assert stats["tg"] == 3 and len(pub.sent) == 3       # не більше 3 на тему на день
    assert stats["fb"] == 2 and len(fb.posts) == 2       # не більше 2 на день у Facebook
    queued = db.query("SELECT COUNT(*) FROM drafts WHERE status='approved'")[0][0]
    assert queued == 3                                    # надлишок чекає наступного дня


def test_listen_reacts_to_button_immediately(cfg, db, monkeypatch, tmp_path):
    """Режим прослуховування: натискання обробляється й публікується в межах одного запуску."""
    from vpo_news import __main__ as cli
    s = make_settings(cfg, test_group_chat_id=-200, db_path=db.path if hasattr(db, "path") else None)
    s.db_path = s.db_path or tmp_path / "x.sqlite"
    did = _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    api.updates = [_cb(1, f"tg:{did}", db.get_draft(did)["approval_message_id"])]
    pub = FakeTelegram()
    monkeypatch.setattr(approval, "_tg", lambda s_, a=None: api)
    monkeypatch.setattr(cli, "DB", lambda path: db)
    monkeypatch.setattr(db, "close", lambda: None)
    real_run = publisher.run_queue
    monkeypatch.setattr(cli.publisher, "run_queue", lambda s_, db_: real_run(s_, db_, tg_api=pub, notify_api=FakeTelegram()))
    cli.listen(s, minutes=0.02)
    assert db.get_draft(did)["tg_status"] == "dry_published"
    assert pub.sent and pub.sent[0]["chat_id"] == -200


def test_consecutive_approvals_published_an_hour_apart(cfg, db):
    cfg["telegram"]["min_interval_minutes"] = 60
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    now = iso(utcnow())
    a = _draft(db, cluster=1, status="approved", target="tgfb", tg_status="queued", fb_status="queued", decided_at=now)
    b = _draft(db, cluster=2, pillar="DIM", status="approved", target="tgfb", tg_status="queued", fb_status="queued", decided_at=now)
    pub, fb = FakeTelegram(), FakeFacebook()
    stats = publisher.run_queue(s, db, tg_api=pub, fb_api=fb, notify_api=FakeTelegram())
    assert stats["tg"] == 1 and len(pub.sent) == 1 and len(fb.posts) == 1  # другий чекає разом із Facebook
    assert db.get_draft(b)["tg_status"] == "queued" and db.get_draft(b)["fb_status"] == "queued"
    # минула година – виходить другий
    db.update_draft(a, tg_published_at=iso(utcnow() - timedelta(minutes=61)))
    publisher.run_queue(s, db, tg_api=pub, fb_api=fb, notify_api=FakeTelegram())
    assert len(pub.sent) == 2 and db.get_draft(b)["tg_status"] == "published"


def test_test_posts_do_not_count_after_going_live(cfg, db):
    cfg["telegram"]["min_interval_minutes"] = 60
    now = iso(utcnow())
    for n in range(3):  # три тестові публікації сьогодні в одній темі
        _draft(db, cluster=10 + n, status="published", tg_status="dry_published", tg_published_at=now, decided_at=now)
    s = make_settings(cfg, dry_run=False, live_confirmed=True)
    d = _draft(db, cluster=20, status="approved", target="tg", tg_status="queued", decided_at=now)
    pub = FakeTelegram()
    publisher.run_queue(s, db, tg_api=pub, notify_api=FakeTelegram())
    assert db.get_draft(d)["tg_status"] == "published"  # ні ліміт, ні інтервал тест не з'їли


def test_live_hides_buttons_without_tokens(cfg, db):
    s = make_settings(cfg, dry_run=False, live_confirmed=True)  # токенів Facebook і LinkedIn немає
    _draft(db)
    api = FakeTelegram()
    approval.send_pending(s, db, api)
    labels = [b["text"] for row in api.sent[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert labels == ["Опублікувати в Telegram", "Редагувати", "Відхилити"]
    s2 = make_settings(cfg, dry_run=False, live_confirmed=True, facebook_token="x")
    assert s2.facebook_button and not s2.linkedin_button
