"""Стан сервісу в SQLite: знайдені новини, чернетки, статуси, службові значення."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

from .util import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL,
    url_norm      TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    title_key     TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    domain        TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- primary | media
    owner         TEXT,                       -- власник медіа (для «двох незалежних медіа»)
    published_at  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    summary       TEXT,
    body          TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    status_at     TEXT NOT NULL,
    reason        TEXT,
    cluster_id    INTEGER,
    image_url     TEXT                        -- og:image сторінки першоджерела
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id    INTEGER PRIMARY KEY,
    checked_sig   TEXT,                       -- склад кластера під час останньої перевірки моделлю
    attempts      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS drafts (
    id                  INTEGER PRIMARY KEY,
    cluster_id          INTEGER NOT NULL UNIQUE,
    item_id             INTEGER NOT NULL,
    pillar              TEXT NOT NULL,
    secondary_pillar    TEXT,
    text                TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    verification_basis  TEXT NOT NULL,        -- JSON {"type": ..., "urls": [...]}
    confidence          REAL NOT NULL,
    warnings            TEXT,                 -- JSON-список
    deadline            TEXT,                 -- для рубрики МОЖЛИВОСТІ: РРРР-ММ-ДД
    reason              TEXT,
    status              TEXT NOT NULL,        -- doubtful | to_send | awaiting | editing | approved | rejected | expired | published
    target              TEXT,                 -- tg | tgfb | all (Telegram, Facebook і LinkedIn)
    edited              INTEGER NOT NULL DEFAULT 0,
    approval_message_id INTEGER,
    created_at          TEXT NOT NULL,
    sent_at             TEXT,
    decided_at          TEXT,
    tg_status           TEXT NOT NULL DEFAULT 'none',   -- none | queued | published | dry_published | failed
    tg_published_at     TEXT,
    tg_message_id       INTEGER,
    fb_status           TEXT NOT NULL DEFAULT 'none',
    fb_published_at     TEXT,
    fb_post_id          TEXT,
    li_status           TEXT NOT NULL DEFAULT 'none',
    li_published_at     TEXT,
    li_post_id          TEXT,
    image_kind          TEXT,                 -- source (з першоджерела) | card (картка платформи) | NULL
    image_url           TEXT,
    image_note          TEXT,                 -- звідки зображення і результат перевірки
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

ITEM_FIELDS = (
    "url", "url_norm", "title", "title_key", "source_name", "domain", "kind", "owner",
    "published_at", "summary", "body", "status", "reason", "image_url",
)


class DB:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        # Один файл без WAL – зручно зберігати в git-гілці.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Додає нові колонки до бази, створеної попередньою версією."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(drafts)")}
        if "deadline" not in cols:
            self.conn.execute("ALTER TABLE drafts ADD COLUMN deadline TEXT")
        for col in ("image_kind", "image_url", "image_note"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE drafts ADD COLUMN {col} TEXT")
        icols = {r[1] for r in self.conn.execute("PRAGMA table_info(items)")}
        if "image_url" not in icols:
            self.conn.execute("ALTER TABLE items ADD COLUMN image_url TEXT")
        if "li_status" not in cols:
            self.conn.execute("ALTER TABLE drafts ADD COLUMN li_status TEXT NOT NULL DEFAULT 'none'")
            self.conn.execute("ALTER TABLE drafts ADD COLUMN li_published_at TEXT")
            self.conn.execute("ALTER TABLE drafts ADD COLUMN li_post_id TEXT")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str | None) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------ items
    def has_url(self, url_norm: str) -> bool:
        return self.conn.execute("SELECT 1 FROM items WHERE url_norm=?", (url_norm,)).fetchone() is not None

    def insert_item(self, **fields) -> int:
        now = iso(utcnow())
        data = {k: fields.get(k) for k in ITEM_FIELDS}
        data["status"] = data["status"] or "new"
        cols = ", ".join(list(data) + ["fetched_at", "status_at"])
        marks = ", ".join(["?"] * (len(data) + 2))
        cur = self.conn.execute(
            f"INSERT INTO items({cols}) VALUES({marks})", (*data.values(), now, now)
        )
        self.conn.commit()
        return cur.lastrowid

    def set_item_status(self, item_id: int, status: str, reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE items SET status=?, reason=COALESCE(?, reason), status_at=? WHERE id=?",
            (status, reason, iso(utcnow()), item_id),
        )
        self.conn.commit()

    def set_item_cluster(self, item_id: int, cluster_id: int) -> None:
        self.conn.execute("UPDATE items SET cluster_id=? WHERE id=?", (cluster_id, item_id))
        self.conn.commit()

    def items_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM items WHERE status IN ({marks}) ORDER BY published_at", statuses
        ).fetchall()

    def cluster_items(self, cluster_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE cluster_id=? ORDER BY published_at", (cluster_id,)
        ).fetchall()

    def recent_items(self, since_iso: str, exclude_statuses=("irrelevant_prefilter", "blocked")) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(exclude_statuses))
        return self.conn.execute(
            f"SELECT * FROM items WHERE published_at >= ? AND cluster_id IS NOT NULL AND status NOT IN ({marks})",
            (since_iso, *exclude_statuses),
        ).fetchall()

    # --------------------------------------------------------- clusters
    def cluster_state(self, cluster_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clusters WHERE cluster_id=?", (cluster_id,)).fetchone()

    def mark_cluster_checked(self, cluster_id: int, sig: str) -> None:
        self.conn.execute(
            "INSERT INTO clusters(cluster_id, checked_sig, attempts) VALUES(?, ?, 1) "
            "ON CONFLICT(cluster_id) DO UPDATE SET checked_sig=excluded.checked_sig, attempts=attempts+1",
            (cluster_id, sig),
        )
        self.conn.commit()

    # ----------------------------------------------------------- drafts
    def draft_for_cluster(self, cluster_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drafts WHERE cluster_id=?", (cluster_id,)).fetchone()

    def insert_draft(self, **fields) -> int:
        fields = dict(fields)
        fields.setdefault("created_at", iso(utcnow()))
        for key in ("verification_basis", "warnings"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        cols = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        cur = self.conn.execute(f"INSERT INTO drafts({cols}) VALUES({marks})", tuple(fields.values()))
        self.conn.commit()
        return cur.lastrowid

    def update_draft(self, draft_id: int, **fields) -> None:
        for key in ("verification_basis", "warnings"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE drafts SET {sets} WHERE id=?", (*fields.values(), draft_id))
        self.conn.commit()

    def get_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def drafts_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM drafts WHERE status IN ({marks}) ORDER BY id", statuses
        ).fetchall()

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------- очищення
    def prune(self, keep_items_days: int, keep_drafts_days: int) -> None:
        now = utcnow()
        item_cut = iso(now - timedelta(days=keep_items_days))
        draft_cut = iso(now - timedelta(days=keep_drafts_days))
        self.conn.execute(
            "DELETE FROM drafts WHERE created_at < ? AND status IN "
            "('published','rejected','expired','doubtful')",
            (draft_cut,),
        )
        self.conn.execute(
            "DELETE FROM items WHERE fetched_at < ? AND id NOT IN (SELECT item_id FROM drafts)",
            (item_cut,),
        )
        self.conn.execute("DELETE FROM clusters WHERE cluster_id NOT IN (SELECT DISTINCT cluster_id FROM items WHERE cluster_id IS NOT NULL)")
        self.conn.commit()
        self.conn.execute("VACUUM")


def basis(row) -> dict:
    try:
        return json.loads(row["verification_basis"] or "{}")
    except (TypeError, ValueError):
        return {}


def warnings_of(row) -> list[str]:
    try:
        return json.loads(row["warnings"] or "[]")
    except (TypeError, ValueError):
        return []
