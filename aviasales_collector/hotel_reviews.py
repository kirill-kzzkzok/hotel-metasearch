from __future__ import annotations

import re
from typing import Any


_THEMES = {
    "location": ("локац", "располож", "рядом", "центр", "метро", "пляж", "location", "located", "beach"),
    "cleanliness": ("чист", "гряз", "clean", "dirty"),
    "breakfast": ("завтрак", "breakfast"),
    "staff": ("персонал", "сотрудник", "обслужив", "service", "staff", "helpful"),
    "room": ("номер", "комнат", "room", "spacious", "small", "тесн", "маленьк"),
    "noise": ("шум", "noisy", "noise", "quiet"),
    "bathroom": ("ванн", "душ", "bathroom", "shower"),
    "pool": ("бассейн", "pool", "swimming"),
    "value": ("соотношение цены", "цена и качество", "price and quality", "value for money"),
}
_POSITIVE = (
    "отличн", "прекрасн", "хорош", "удобн", "вкусн", "дружелюб", "внимательн", "чист",
    "excellent", "great", "good", "clean", "delicious", "friendly", "comfortable",
    "spacious", "quiet", "helpful",
)
_NEGATIVE = (
    "плох", "ужас", "гряз", "шумн", "тесн", "маленьк", "далеко", "груб", "медлен", "bad",
    "слом", "dirty", "noisy", "small", "poor", "broken", "rude", "far", "slow",
)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _has_negation_before(text: str, position: int) -> bool:
    prefix = text[max(0, position - 32):position]
    return re.search(r"(?:\bне|\bnot|\bno|\bnever)(?:\W+\w+){0,2}\W*$", prefix) is not None


def _sentiment_score(text: str) -> int:
    score = 0
    for terms, polarity in ((_POSITIVE, 1), (_NEGATIVE, -1)):
        for term in terms:
            position = text.find(term)
            if position >= 0:
                score += -polarity if _has_negation_before(text, position) else polarity
    return score


def analyze_review_quotes(
    quotes: list[str], source: str | None, retrieved_at: str | None,
) -> dict[str, Any]:
    phrases = list(dict.fromkeys(" ".join(str(quote).split()) for quote in quotes if str(quote).strip()))
    evidence: dict[str, list[str]] = {theme: [] for theme in _THEMES}
    positive: set[str] = set()
    negative: set[str] = set()

    for phrase in phrases:
        normalized = phrase.casefold()
        matched = [theme for theme, terms in _THEMES.items() if _contains_any(normalized, terms)]
        for theme in matched:
            evidence[theme].append(phrase)
        if not matched:
            continue

        polarity = _sentiment_score(normalized)
        if polarity > 0:
            positive.update(matched)
        elif polarity < 0:
            negative.update(matched)

    mentions = [theme for theme, examples in evidence.items() if examples]
    return {
        "phrases": phrases,
        "mentions": mentions,
        "positive": [theme for theme in _THEMES if theme in positive],
        "negative": [theme for theme in _THEMES if theme in negative],
        "evidence": {theme: examples for theme, examples in evidence.items() if examples},
        "source": source if phrases else None,
        "retrieved_at": retrieved_at if phrases else None,
        "sampled_quotes": len(phrases),
        "status": "quoted_mentions" if phrases else "not_available",
    }
