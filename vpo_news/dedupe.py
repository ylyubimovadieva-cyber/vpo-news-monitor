"""Пошук дублікатів і новин про ту саму подію за схожістю заголовків і лідів."""
from __future__ import annotations

import re

_STOP = {
    "або", "але", "без", "біля", "був", "була", "були", "було", "вже", "від", "для", "його", "її", "їх",
    "так", "також", "там", "тим", "того", "той", "тому", "треба", "через", "щодо", "що", "які", "який",
    "яка", "яке", "якщо", "при", "про", "під", "після", "над", "між", "між", "них", "ним", "нас", "вас",
    "році", "року", "рік", "the", "and", "for", "with", "from", "this", "that", "will", "has", "have",
    "україні", "україна", "україни", "українців", "заява", "новини",
}

_WORD = re.compile(r"[0-9a-zа-яіїєґ']+", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    text = (text or "").lower().replace("’", "'").replace("ʼ", "'")
    return [w for w in _WORD.findall(text) if len(w) >= 3 and w not in _STOP]


def _stems(text: str) -> set[str]:
    # Груба «основа» слова: перші 5 літер. Для української цього досить, щоб
    # «переселенців» і «переселенцям» збігалися.
    return {w[:5] if not w.isdigit() else w for w in _tokens(text)}


# Основи слів, які є майже в кожній новині про ВПО, – вони не допомагають
# відрізнити одну подію від іншої.
_COMMON_STEMS = {"внутр", "перем", "перес", "осіб", "особа", "особи", "впо", "украї", "idp", "idps",
                 "displ", "inter", "peopl", "війни", "війна", "через", "можут", "отрим"}


def _event_stems(text: str) -> set[str]:
    return _stems(text) - _COMMON_STEMS


def title_key(title: str) -> str:
    return " ".join(sorted(_stems(title)))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_similarity(a: str, b: str) -> float:
    return _jaccard(_event_stems(a), _event_stems(b))


def event_similarity(title_a: str, lead_a: str, title_b: str, lead_b: str) -> float:
    """Схожість двох новин: заголовки важать більше, ліди – допоміжно."""
    t = title_similarity(title_a, title_b)
    lead = _jaccard(_event_stems(f"{title_a} {(lead_a or '')[:400]}"), _event_stems(f"{title_b} {(lead_b or '')[:400]}"))
    return max(t, 0.5 * t + 0.5 * lead)
