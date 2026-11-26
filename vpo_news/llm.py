"""Класифікація новин і підготовка тексту допису через Claude API."""
from __future__ import annotations

import json
import logging

import anthropic

from .config import PILLAR_KEYS

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


PILLAR_ENUM = list(PILLAR_KEYS) + ["NONE"]

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "pillar": {"type": "string", "enum": PILLAR_ENUM},
        "secondary_pillar": {"type": "string", "enum": PILLAR_ENUM},
        "same_event_urls": {"type": "array", "items": {"type": "string"}},
        "is_rumor_or_unconfirmed": {"type": "boolean"},
        "verified": {"type": "boolean"},
        "verification_basis": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["primary", "two_media", "none"]},
                "urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["type", "urls"],
            "additionalProperties": False,
        },
        "deadline": {"type": "string"},
        "summary_uk": {"type": "string"},
        "international": {"type": "boolean"},
        "summary_en": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": [
        "relevant", "pillar", "secondary_pillar", "same_event_urls", "is_rumor_or_unconfirmed",
        "verified", "verification_basis", "deadline", "summary_uk", "international", "summary_en",
        "confidence", "reason",
    ],
    "additionalProperties": False,
}

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"summary_uk": {"type": "string"}},
    "required": ["summary_uk"],
    "additionalProperties": False,
}

STYLE_RULES = """Вимоги до тексту допису (поле summary_uk):
- українською, людяно і стримано; речення різної довжини; без канцеляризмів («здійснювати», «з метою», «у рамках», «даний») і без повторів;
- зміст: що сталося, кого це стосується, що це означає для людей; конкретні цифри й дати – лише ті, що є в матеріалах;
- без хештегів, без емодзі, без знаків оклику; тире лише коротке «–», ніколи довге «—»;
- без оцінок на зразок «нарешті», «ганебно», «довгоочікуваний», якщо їх немає в першоджерелі;
- жодних висновків, узагальнень чи оцінок від себе. Кожна оцінка, порада, заклик, прогноз чи твердження про те, що «важливо», «потрібно», «має бути», – лише з автором: «у фонді наголошують, що…», «за словами Уповноваженого…», «автори дослідження радять…». Особливо не завершуй текст власним підсумковим реченням: останнє речення – факт або слова джерела;
- точно перекладай англомовні матеріали: displacement – «переміщення» (не «виселення»), returnees – «ті, хто повернувся», livelihoods – «засоби до існування»; пиши грамотно («а тому що», а не «а бо»);
- діапазони чисел і днів – через коротке тире без пробілів: «5–11 класи», «пн–сб»;
- не приписуй Конгресу ВПО України чи Національній платформі того, чого вони не робили; ніколи не пиши, що Конгрес чи платформа «виконує», «впроваджує» або «реалізує» Стратегію;
- не згадуй імен приватних осіб і не наводь персональних даних (посадовців органів влади і міжнародних організацій можна називати за посадою);
- НЕ пиши назву стовпа, посилання чи слово «Джерело» – їх додасть система; пиши лише основний текст."""

INTERNATIONAL_RULES = """Поля international і summary_en (для сторінки в LinkedIn, де читає міжнародна аудиторія: донори, міжнародні організації, дослідники, дипломати, партнери в ЄС):
- international=true лише якщо новина важлива саме для міжнародної аудиторії: рішення й програми міжнародних організацій і донорів (ООН, УВКБ ООН, МОМ, ЄС, Рада Європи, Світовий банк), міжнародне право і правосуддя (реєстр збитків, Міжнародна комісія з розгляду претензій, документування воєнних злочинів), дослідження й дані про переміщення, зміни державної політики щодо ВПО, що стосуються міжнародних партнерів, тимчасовий захист українців за кордоном, повернення депортованих дітей, міжнародні гранти й програми, відкриті для організацій;
- international=false для суто локальної практичної інформації: телефони гарячих ліній, порядок отримання виплат чи послуг у конкретній громаді, оголошення ОВА про прийом документів, локальні заходи;
- якщо international=true – summary_en: той самий зміст англійською (British English), за тими самими вимогами (без оцінок від себе, без знаків оклику, емодзі й хештегів, тире лише «–»), але з коротким контекстом для іноземного читача (наприклад, хто такі ВПО – internally displaced persons, IDPs; що таке ОВА – regional military administration). Обсяг – як у summary_uk. Без назви стовпа, посилання і слова Source;
- якщо international=false – summary_en порожній рядок."""


def build_system_prompt(cfg: dict) -> str:
    pillars = "\n".join(
        f"- {key}: «{p['title']}» – {p['about']}" for key, p in cfg["pillars"].items()
    )
    return f"""Ти – редактор новин громадської організації «Конгрес ВПО України». Ти відбираєш новини для Telegram-групи «Національна платформа» національної платформи «Національний конгрес стійкості» і готуєш короткі дописи.

П'ять стовпів платформи і рубрика MOZHLYVOSTI (службовий ключ: назва – тематика):
{pillars}

Про рубрику MOZHLYVOSTI: якщо матеріал оголошує грант, конкурс, стипендію, навчальну чи іншу програму, на яку ВПО або організації, що з ними працюють, можуть подати заявку, – основний pillar = MOZHLYVOSTI, а тематичний стовп (наприклад, DIM для житлового гранту) – у secondary_pillar. У полі deadline вкажи кінцеву дату подання заявок у форматі РРРР-ММ-ДД, лише якщо вона прямо вказана в матеріалі; інакше порожній рядок. У тексті допису обов'язково: хто може подати заявку, що можна отримати, дедлайн і як подати. Для решти матеріалів deadline – порожній рядок.

Звідки бувають матеріали: сайти й офіційні сторінки органів влади (зокрема обласних військових адміністрацій), міжнародних і громадських організацій, офіційні сторінки народних депутатів і Уповноваженого з прав людини, медіа. Заяви політиків без ухвалених рішень, обіцянки й наміри («плануємо», «пропоную») – не новина для платформи: relevant=false або is_rumor_or_unconfirmed=true. Звіти про власну роботу (зустрічі, консультації з ВПО) релевантні, лише якщо з них випливає щось конкретне для людей.

Завдання для кожної новини:
1. relevant – чи стосується новина внутрішньо переміщених осіб (ВПО) або людей, постраждалих від війни, і чи вона корисна людям (рішення, програми, виплати, права, послуги, дані). Загальні новини фронту, політичні заяви без наслідків для людей, анонси без змісту – не релевантні. Так само не релевантні матеріали без новини по суті: «оприлюднили довідку/звіт», якщо в матеріалі немає ні нових цифр, ні того, хто і що може отримати чи зробити.
2. pillar – основний стовп; secondary_pillar – другий, якщо новина справді стосується двох, інакше NONE. Якщо relevant=false, обидва NONE.
3. same_event_urls – з переліку «інших матеріалів» лише ті URL, що описують ТУ САМУ подію (те саме рішення, ту саму програму, ті самі цифри). Не вигадуй URL, бери лише з переліку.
4. is_rumor_or_unconfirmed – true, якщо це чутки, припущення («можливо», «можуть», «розглядають»), неофіційні цифри або посилання на анонімні джерела чи Telegram-канали.
5. verified і verification_basis: verified=true, якщо основний матеріал опублікований на першоджерелі (сайт органу влади, міжнародної організації чи організації, про яку йдеться) – тоді type=primary. Якщо організація пише про подію, що сталася з іншими (наприклад, фонд повідомляє про атаку на авто іншого фонду), для цієї події вона не першоджерело: потрібне підтвердження ще одним незалежним джерелом, інакше verified=false; або якщо щонайменше два різні медіа з переліку повідомляють ту саму подію – тоді type=two_media. urls – відповідні посилання з переліку.
6. summary_uk – текст допису за вимогами нижче. Якщо relevant=false, залиш порожнім.
7. confidence – від 0 до 1: наскільки ти впевнений, що новина релевантна, правильно віднесена до стовпа, підтверджена, а текст точно передає першоджерело.
8. reason – одне коротке речення, чому ти так вирішив.

{STYLE_RULES}

{INTERNATIONAL_RULES}"""


def _item_block(item, label: str, limit: int) -> str:
    text = (item["body"] or item["summary"] or "").strip()[:limit]
    return (
        f"[{label}]\nURL: {item['url']}\nДжерело: {item['source_name']} ({item['domain']}, "
        f"{'першоджерело' if item['kind'] == 'primary' else 'медіа'})\n"
        f"Опубліковано: {item['published_at']}\nЗаголовок: {item['title']}\nТекст: {text}\n"
    )


def build_user_prompt(rep, others, body_range: tuple[int, int]) -> str:
    parts = [_item_block(rep, "ОСНОВНИЙ МАТЕРІАЛ", 5000)]
    if others:
        parts.append("Інші матеріали, які можуть описувати ту саму подію:")
        for n, o in enumerate(others, 1):
            parts.append(_item_block(o, f"ІНШИЙ МАТЕРІАЛ {n}", 2000))
    lo, hi = body_range
    parts.append(f"Довжина summary_uk: від {lo} до {hi} символів разом із пробілами (суворо).")
    return "\n".join(parts)


class Classifier:
    def __init__(self, cfg: dict, client: anthropic.Anthropic | None = None):
        llm = cfg.get("llm", {})
        self.cfg = cfg
        self.model = llm.get("model", "claude-sonnet-5-5")
        self.effort = llm.get("effort", "medium")
        self.max_tokens = int(llm.get("max_tokens", 8000))
        self.fallbacks = bool(llm.get("server_fallbacks", True))
        self.client = client or anthropic.Anthropic()  # ключ – зі змінної ANTHROPIC_API_KEY
        self.system = build_system_prompt(cfg)

    def _call(self, user, schema: dict) -> dict:
        """user – текст або список блоків (наприклад, зображення + текст)."""
        params = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            if self.fallbacks:
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
                )
            else:
                resp = self.client.messages.create(**params)
        except anthropic.RateLimitError as exc:
            raise LLMError("ліміт запитів Anthropic API") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"помилка Anthropic API {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("немає з'єднання з Anthropic API") from exc

        if resp.stop_reason == "refusal":
            raise LLMError("модель відмовилася обробляти матеріал")
        if resp.stop_reason == "max_tokens":
            raise LLMError("відповідь моделі обрізано (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            raise LLMError("порожня відповідь моделі")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise LLMError("модель повернула некоректний JSON") from exc

    def classify(self, rep, others, body_range: tuple[int, int]) -> dict:
        return self._call(build_user_prompt(rep, others, body_range), CLASSIFY_SCHEMA)

    def review_image(self, data: bytes, mime: str, title: str, domain: str) -> dict:
        """Перевірка зображення з першоджерела: обличчя, персональні дані, авторське право."""
        from .images import IMAGE_RULES, VISION_SCHEMA, b64
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64(data)}},
            {"type": "text", "text": f"{IMAGE_RULES}\n\nНовина: «{title}» (джерело: {domain}). Чи можна використати це зображення?"},
        ]
        return self._call(content, VISION_SCHEMA)

    def rewrite(self, rep, body: str, errors: list[str], body_range: tuple[int, int]) -> str:
        lo, hi = body_range
        user = (
            _item_block(rep, "ПЕРШОДЖЕРЕЛО", 5000)
            + "\nЧернетка допису:\n" + body
            + "\n\nЧернетка порушує правила:\n- " + "\n- ".join(errors)
            + f"\n\nПерепиши текст так, щоб виправити ці порушення і дотриматися всіх вимог. "
              f"Довжина: від {lo} до {hi} символів. Поверни лише основний текст у summary_uk."
        )
        return self._call(user, REWRITE_SCHEMA)["summary_uk"]
