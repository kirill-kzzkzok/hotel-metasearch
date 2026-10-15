"""Empty-results detection for Trip.com list pages.

2026-10-15 Suzdal live session: the page legitimately said "Найдено 0
объектов размещения" / "Мы не нашли подходящие объекты размещения" /
"Подходящие отели не найдены." but the detector missed every phrasing, so
the source reported unavailable/incomplete_collection instead of the
honest no_results. These tests pin the live phrasings and guard against
false positives on non-empty pages.
"""
from aviasales_collector.hotels.sources.tripcom import (
    _tripcom_explicit_empty_results,
)

EMPTY_BODIES = {
    "suzdal_live_full": (
        "Найдено 0 объектов размещения\nРекомендовано Trip.com\n"
        "Мы не нашли подходящие объекты размещения\n"
        "Измените параметры поиска и попробуйте еще раз.\n"
        "Подходящие отели не найдены."
    ),
    "suzdal_found_zero": "Найдено 0 объектов размещения",
    "we_did_not_find": "Мы не нашли подходящие объекты размещения",
    "hotels_not_found": "Подходящие отели не найдены.",
    "legacy_ru": "не найдено отелей по вашему запросу",
    "legacy_ru_variants": "Подходящие варианты не найдены",
    "legacy_en": "No properties found for these dates",
    "legacy_en_no_hotels": "There are no rooms available for these dates",
}

NON_EMPTY_BODIES = {
    "dubai_live": (
        "Найдено 5328 объектов размещения\nСвободные номера\nДубай"
    ),
    "positive_found": "Подходящие отели найдены и забронированы",
    "positive_we_found": "Мы нашли подходящие объекты размещения",
}


def test_live_empty_phrasings_are_detected():
    for label, body in EMPTY_BODIES.items():
        assert _tripcom_explicit_empty_results(body), label


def test_non_empty_pages_do_not_match():
    for label, body in NON_EMPTY_BODIES.items():
        assert not _tripcom_explicit_empty_results(body), label
