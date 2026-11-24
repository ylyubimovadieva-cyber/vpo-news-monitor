"""Завантаження config.yaml і змінних оточення."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
# П'ять стовпів платформи і рубрика «МОЖЛИВОСТІ» (гранти і програми) зі своєю темою в групі.
PILLAR_KEYS = ("DIM", "HOLOS", "TURBOTA", "POVERNENNIA", "PAMIAT", "MOZHLYVOSTI")
OPPORTUNITY_KEY = "MOZHLYVOSTI"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "так", "on")


def _env_int(name: str) -> int | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("Змінна %s має бути числом, значення проігноровано", name)
        return None


@dataclass
class Settings:
    cfg: dict
    dry_run: bool = True
    live_confirmed: bool = False
    db_path: Path = field(default_factory=lambda: ROOT / "state" / "news.sqlite")
    approval_token: str | None = None
    publish_token: str | None = None
    facebook_token: str | None = None
    coordinator_chat_id: int = 0
    test_group_chat_id: int | None = None

    # ---- зручні геттери
    @property
    def pillars(self) -> dict:
        return self.cfg["pillars"]

    def pillar_title(self, key: str | None) -> str:
        if not key or key not in self.pillars:
            return ""
        return self.pillars[key]["title"]

    @property
    def live(self) -> bool:
        """Бойовий режим – лише коли DRY_RUN=false І LIVE_CONFIRMED=true."""
        return (not self.dry_run) and self.live_confirmed

    @property
    def publish_mode(self) -> str:
        if self.live:
            return "live"
        if self.test_group_chat_id:
            return "test_group"
        return "log"

    @property
    def facebook_enabled(self) -> bool:
        return bool(self.cfg.get("facebook", {}).get("enabled", False))


def load_config(path: Path | str | None = None) -> dict:
    path = Path(path or os.environ.get("VPO_CONFIG") or ROOT / "config.yaml")
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    missing = [k for k in PILLAR_KEYS if k not in cfg.get("pillars", {})]
    if missing:
        raise ValueError(f"У config.yaml бракує стовпів: {missing}")
    return cfg


def load_settings(config_path: Path | str | None = None) -> Settings:
    cfg = load_config(config_path)
    tg = cfg.get("telegram", {})
    approval_token = os.environ.get("TELEGRAM_APPROVAL_BOT_TOKEN") or None
    s = Settings(
        cfg=cfg,
        dry_run=_env_bool("DRY_RUN", True),
        live_confirmed=_env_bool("LIVE_CONFIRMED", False),
        db_path=Path(os.environ.get("VPO_DB_PATH") or ROOT / "state" / "news.sqlite"),
        approval_token=approval_token,
        publish_token=os.environ.get("TELEGRAM_PUBLISH_BOT_TOKEN") or approval_token,
        facebook_token=os.environ.get("FACEBOOK_PAGE_TOKEN") or None,
        coordinator_chat_id=_env_int("TELEGRAM_COORDINATOR_CHAT_ID") or int(tg.get("coordinator_chat_id") or 0),
        test_group_chat_id=_env_int("TELEGRAM_TEST_GROUP_CHAT_ID"),
    )
    if not s.dry_run and not s.live_confirmed:
        log.warning("DRY_RUN=false, але LIVE_CONFIRMED не встановлено – працюю в тестовому режимі")
    log.info("Режим публікації: %s", s.publish_mode)
    return s
