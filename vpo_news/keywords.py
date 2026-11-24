"""Підказка стовпа за ключовими словами з config.yaml (без моделі).

Використовується для перевірки ключових слів в офлайн-тестах і як
резервна оцінка; остаточне рішення про стовп ухвалює модель.
"""
from __future__ import annotations

import re


def pillar_scores(text: str, cfg: dict) -> dict[str, int]:
    scores = {}
    for key, p in cfg["pillars"].items():
        scores[key] = sum(len(re.findall(k, text, re.IGNORECASE)) for k in p.get("keywords", []))
    return scores


def guess_pillar(text: str, cfg: dict) -> str | None:
    scores = pillar_scores(text, cfg)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None
