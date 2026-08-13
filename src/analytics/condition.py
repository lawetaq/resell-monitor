from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ConditionAssessment:
    condition_class: str
    matched_warning_phrases: tuple[str, ...]
    item_condition: str = "unknown"


FAULT = (
    "неисправен", "неисправна", "не работает", "нерабочий", "нерабочая",
    "на запчасти", "артефакты", "нет изображения", "не включается",
    "требует ремонта", "под восстановление", "с дефектом", "битая", "битый",
    "не рабочая", "не рабочий", "неисправная", "неисправный", "на запчасти",
    "под ремонт", "не стартует",
)
RISK = (
    "после ремонта", "ремонтировалась", "ремонтировался", "майнинг",
    "после майнинга", "вскрывалась", "вскрывался", "без проверки",
    "не проверял", "не проверяла", "возможны проблемы",
)

NEGATED_PATTERNS = (
    r"не\s+ремонтировал(?:ся|ась)?", r"не\s+вскрывал(?:ся|ась)?",
    r"артефакт(?:ов|ы)?\s+(?:нет|не\s+имеется)",
    r"без\s+артефактов", r"не\s+был[ао]?\s+в\s+майнинг",
)


def assess_condition(title: str, description: str | None = None) -> ConditionAssessment:
    text = f"{title} {description or ''}".casefold().replace("ё", "е")
    safe = text
    for pattern in NEGATED_PATTERNS:
        safe = re.sub(pattern, " ", safe)
    safe = re.sub(r"не\s+стартует\s+xmp\s*\d*", " ", safe)
    faults = tuple(phrase for phrase in FAULT if phrase in safe)
    risks = tuple(phrase for phrase in RISK if phrase in safe)
    if faults:
        item = "parts_only" if any(x in faults for x in ("на запчасти", "под ремонт")) else "faulty"
        return ConditionAssessment("FAULT", faults, item)
    if risks:
        return ConditionAssessment("RISK", risks, "unknown")
    working = any(phrase in safe for phrase in ("работает", "рабочая", "рабочий", "без претензий"))
    new = any(phrase in safe for phrase in ("новая", "новый", "не использовалась", "не использовался"))
    return ConditionAssessment("OK", (), "new" if new else "working" if working else "unknown")
