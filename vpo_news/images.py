"""Зображення до дописів: з першоджерела (після перевірки) або власна картка платформи.

Правила (вони ж – інструкція моделі, див. IMAGE_RULES):
- зображення з джерела беремо лише з офіційного першоджерела (органи влади,
  міжнародні й громадські організації, про які новина) – їхні матеріали
  публікуються для поширення; фото медіа та агентств захищені авторським правом
  і не використовуються;
- модель перевіряє зображення: немає впізнаваних облич (особливо дітей,
  постраждалих, військових, ВПО), немає персональних даних (документи, номери
  авто, адреси, телефони, імена на табличках), немає водяних знаків чи підписів
  фотоагентств і фотостоків, немає сцен насильства й тіл загиблих;
- якщо придатного зображення немає – власна картка платформи: назва стовпа і
  заголовок, без людей і чужих зображень;
- зображення надсилається координаторці разом із текстом, і публікується лише
  після її погодження.
"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

IMAGE_RULES = """Ти перевіряєш зображення, яке громадська організація хоче додати до новини для внутрішньо переміщених осіб.
Дотримуйся міжнародних стандартів захисту персональних даних (GDPR, Конвенція Ради Європи 108+, Закон України «Про захист персональних даних») і гуманітарних стандартів щодо зображень людей (гідність, поінформована згода, особливий захист дітей і вразливих груп).
Зображення НЕ можна використовувати (usable=false), якщо на ньому:
- впізнавані обличчя людей (будь-хто, особливо діти, ВПО, постраждалі, військові, пацієнти) – ми не маємо їхньої згоди. Офіційний портрет посадовця на офіційному заході допустимий лише якщо це публічна особа при виконанні обов'язків;
- персональні дані: документи, номери автомобілів, адреси, телефони, імена на табличках чи бейджах, екрани з особистою інформацією;
- водяні знаки, логотипи чи підписи фотоагентств і фотостоків (Reuters, AP, AFP, Getty, Shutterstock, EPA, Укрінформ, УНІАН тощо) або позначка «©» стороннього автора;
- сцени насильства, тіла загиблих, поранені, кров;
- реклама або зображення, не пов'язане зі змістом новини.
Допустимі: інфографіка й банери установи, логотипи, документи без персональних даних, будівлі, техніка, загальні плани без впізнаваних облич.
Поверни коротке пояснення українською в reason."""

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "usable": {"type": "boolean"},
        "identifiable_faces": {"type": "boolean"},
        "personal_data": {"type": "boolean"},
        "third_party_copyright": {"type": "boolean"},
        "graphic_content": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["usable", "identifiable_faces", "personal_data", "third_party_copyright", "graphic_content", "reason"],
    "additionalProperties": False,
}

ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}

FONT_DIRS = [
    "/usr/share/fonts/truetype/dejavu",            # GitHub Actions (Ubuntu)
    "C:/Windows/Fonts",
    "/Library/Fonts", "/System/Library/Fonts/Supplemental",
]
FONT_FILES = {"regular": ["DejaVuSans.ttf", "segoeui.ttf", "Arial.ttf", "arial.ttf"],
              "bold": ["DejaVuSans-Bold.ttf", "segoeuib.ttf", "Arial Bold.ttf", "arialbd.ttf"]}


def _font(kind: str, size: int):
    from PIL import ImageFont
    for d in FONT_DIRS:
        for name in FONT_FILES[kind]:
            p = Path(d) / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    raise RuntimeError("не знайдено шрифту з кирилицею для картки")


def download(url: str, max_bytes: int = 5_000_000, client: httpx.Client | None = None) -> tuple[bytes, str] | None:
    """Завантажує зображення. None – якщо це не зображення, воно завелике або недоступне."""
    http = client or httpx.Client(timeout=20, follow_redirects=True,
                                  headers={"User-Agent": "VPO-News-Monitor/1.0 (non-commercial)"})
    try:
        r = http.get(url)
    except httpx.HTTPError:
        return None
    mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if r.status_code != 200 or mime not in ALLOWED_MIME or not r.content or len(r.content) > max_bytes:
        return None
    return r.content, mime


def make_card(pillar_title: str, headline: str, footer: str = "Національна платформа · Конгрес ВПО України") -> bytes:
    """Власна картка платформи 1200×630 без людей і чужих зображень."""
    from PIL import Image, ImageDraw
    W, H, M = 1200, 630, 70
    ink, acc, muted = (24, 36, 58), (0, 59, 112), (90, 100, 115)
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 16], fill=acc)
    d.text((M, 55), pillar_title, font=_font("bold", 40), fill=acc)
    font = _font("bold", 52)
    # переносимо за шириною
    words, cur, out = headline.split(), "", []
    for w in words:
        t = f"{cur} {w}".strip()
        if d.textlength(t, font=font) <= W - 2 * M:
            cur = t
        else:
            out.append(cur)
            cur = w
    out.append(cur)
    if len(out) > 5:
        out = out[:5]
        out[-1] = out[-1].rstrip(".,:;") + "…"
    y = 150
    for ln in out:
        d.text((M, y), ln, font=font, fill=ink)
        y += 66
    d.line([M, H - 95, W - M, H - 95], fill=(220, 225, 232), width=3)
    d.text((M, H - 75), footer, font=_font("regular", 30), fill=muted)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def headline_of(text: str, header: str) -> str:
    """Перше речення основного тексту допису – заголовок для картки."""
    body = text[len(header):] if text.startswith(header) else text
    body = body.strip().split("\n\n")[0]
    first = body.split(". ")[0].strip().rstrip(".")
    return first[:220]


def choose(cfg: dict, classifier, source_item, pillar: str, text: str, http: httpx.Client | None = None) -> dict:
    """Повертає поля чернетки: image_kind, image_url, image_note."""
    icfg = cfg.get("images", {})
    if not icfg.get("enabled", True):
        return {"image_kind": None, "image_url": None, "image_note": None}
    reason = "у першоджерелі немає зображення"
    url = source_item["image_url"] if "image_url" in source_item.keys() else None
    if url and source_item["kind"] != "primary":
        reason = "зображення медіа захищене авторським правом, не використовуємо"
    elif url and classifier is not None:
        got = download(url, int(icfg.get("max_bytes", 5_000_000)), http)
        if got is None:
            reason = "зображення першоджерела недоступне або завелике"
        else:
            try:
                res = classifier.review_image(got[0], got[1], source_item["title"], source_item["domain"])
            except Exception as exc:  # noqa: BLE001 – перевірка не вдалася → безпечний варіант
                log.warning("Перевірка зображення не вдалася: %s", type(exc).__name__)
                res = None
            if res and res.get("usable") and not (res.get("identifiable_faces") or res.get("personal_data")
                                                 or res.get("third_party_copyright") or res.get("graphic_content")):
                return {"image_kind": "source", "image_url": url,
                        "image_note": f"з першоджерела {source_item['domain']}; перевірка: {res.get('reason', '')}"[:300]}
            reason = "зображення першоджерела не пройшло перевірку: " + ((res or {}).get("reason") or "помилка перевірки")
    return {"image_kind": "card", "image_url": None,
            "image_note": f"власна картка платформи ({reason})"[:300]}


def image_bytes(d, cfg: dict, http: httpx.Client | None = None) -> bytes | None:
    """Байти зображення чернетки для надсилання й публікації."""
    kind = d["image_kind"] if "image_kind" in d.keys() else None
    if kind == "source" and d["image_url"]:
        got = download(d["image_url"], int(cfg.get("images", {}).get("max_bytes", 5_000_000)), http)
        if got:
            return got[0]
        kind = "card"  # джерело прибрало зображення – безпечний запасний варіант
    if kind == "card":
        title = cfg["pillars"][d["pillar"]]["title"]
        try:
            return make_card(title, headline_of(d["text"], title))
        except Exception as exc:  # noqa: BLE001 – без зображення допис усе одно публікується
            log.warning("Не вдалося створити картку: %s", type(exc).__name__)
    return None


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()
