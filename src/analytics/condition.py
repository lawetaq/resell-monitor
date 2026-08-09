from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ConditionAssessment:
    condition_class: str
    matched_warning_phrases: tuple[str, ...]


FAULT = (
    "неисправен", "неисправна", "не работает", "нерабочий", "нерабочая",
    "на запчасти", "артефакты", "нет изображения", "не включается",
    "требует ремонта", "под восстановление", "с дефектом", "битая", "битый",
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
    faults = tuple(phrase for phrase in FAULT if phrase in safe)
    risks = tuple(phrase for phrase in RISK if phrase in safe)
    if faults:
        return ConditionAssessment("FAULT", faults)
    if risks:
        return ConditionAssessment("RISK", risks)
    return ConditionAssessment("OK", ())
