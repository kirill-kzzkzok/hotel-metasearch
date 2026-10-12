from datetime import date

from aviasales_collector.parser import (
    extract_airports,
    extract_date_texts,
    extract_transfer_durations,
    extract_transfers,
    normalize_card_data,
    parse_price,
    parse_stops,
    route_from_url,
)


def test_parse_ruble_price_with_narrow_spaces():
    assert parse_price("20\u202f806\u00a0₽") == (20806, "RUB")


def test_parse_unknown_currency_without_inventing_one():
    assert parse_price("1 250") == (1250, None)


def test_parse_stops():
    assert parse_stops("4 ч в пути / Прямой") == 0
    assert parse_stops("8 ч в пути / 1 пересадка") == 1
    assert parse_stops("маршрут не распознан") is None


def test_destination_route_is_preserved_as_opaque_route_token():
    assert route_from_url("https://www.aviasales.ru/search/MOW2710AYT1?expected_price=42") == "MOW2710AYT1"
    assert route_from_url(None) is None


def test_normalized_card_rejects_untrusted_ticket_urls():
    card = {
        "card_text": "Анталья\n5 окт\nПрямой\n4 ч в пути",
        "destination": "Анталья",
        "source_url": "https://aviasales.ru@attacker.example/ticket",
        "price_texts": ["12 345 ₽"],
        "text_items": ["5 окт", "4 ч в пути / Прямой"],
        "date_items": ["5 окт"],
        "airlines": [],
    }

    offer = normalize_card_data(card, "MOW", "OW", date(2026, 9, 22))

    assert offer["source_url"] is None
    assert offer["destination_route"] is None
    assert "ticket link missing" in offer["warnings"]


def test_dates_can_be_split_into_month_and_day_dom_nodes():
    assert extract_date_texts(["Победа", "СЕН", "23", "14:50", "SVO Шереметьево"]) == ["23 сен"]
    assert extract_date_texts(["ОКТ 29", "13:45"]) == ["29 окт"]
    assert extract_date_texts(["СЕН", "23", "17 ч в пути / 1 пересадка"]) == ["23 сен"]


def test_airports_are_taken_only_from_visible_code_and_name_lines():
    assert extract_airports("14:50\nSVO Шереметьево\n16:15\nKGD Храброво") == [
        {"code": "SVO", "name": "Шереметьево"},
        {"code": "KGD", "name": "Храброво"},
    ]


def test_airport_codes_are_extracted_when_separate_or_after_the_name():
    assert extract_airports("08:10\nSVO\nШереметьево\n14:20\nАнталья (AYT)") == [
        {"code": "SVO", "name": "Шереметьево"},
        {"code": "AYT", "name": "Анталья"},
    ]
    assert extract_airports("08:10\nШереметьево\nSVO\n14:20\nАнталья (AYT)") == [
        {"code": "SVO", "name": "Шереметьево"},
        {"code": "AYT", "name": "Анталья"},
    ]


def test_airport_and_transfer_sequences_keep_repeated_points_for_return_legs():
    assert extract_airports("SVO Шереметьево\nAYT Анталья\nAYT Анталья\nSVO Шереметьево") == [
        {"code": "SVO", "name": "Шереметьево"},
        {"code": "AYT", "name": "Анталья"},
        {"code": "AYT", "name": "Анталья"},
        {"code": "SVO", "name": "Шереметьево"},
    ]
    card = "10 ч в пути / 1 пересадка\n3 ч Шанхай\n9 ч в пути / 1 пересадка\n2 ч Шанхай"
    assert extract_transfers(card) == ["Шанхай", "Шанхай"]
    assert extract_transfer_durations(card) == ["3 ч", "2 ч"]


def test_transfer_city_is_extracted_from_rendered_duration_line():
    card = """26\u202f120 ₽
Sichuan Airlines
LED Пулково
PVG Пудун
14 ⁠ч в пути / 1 пересадка
4 ⁠ч Чэнду"""

    assert extract_transfers(card) == ["Чэнду"]
    assert extract_transfer_durations(card) == ["4 ч"]
    assert extract_airports("26\u202f120 ₽\n122 927 ₽\nLED Пулково\nPVG Пудун") == [
        {"code": "LED", "name": "Пулково"},
        {"code": "PVG", "name": "Пудун"},
    ]


def test_normalized_one_way_card_preserves_comparison_price_and_does_not_infer_fields():
    offer = normalize_card_data(
        {
            "card_text": "Анталья\n5 окт\nПрямой\n4 ч в пути",
            "destination": "Анталья",
            "source_url": "https://www.aviasales.ru/search/MOW0510AYT1?expected_price=42",
            "price_texts": ["12 345 ₽", "18 000 ₽"],
            "text_items": ["5 окт", "4 ч в пути / Прямой"],
            "date_items": ["5 окт"],
            "airlines": ["Pobeda"],
        },
        "MOW",
        "OW",
        date(2026, 9, 22),
    )

    assert offer["origin"] == "MOW"
    assert offer["destination"] == "Анталья"
    assert offer["destination_route"] == "MOW0510AYT1"
    assert offer["price"] == 12345
    assert offer["old_price"] == 18000
    assert offer["currency"] == "RUB"
    assert offer["departure_date"] == "2026-10-05"
    assert offer["return_date"] is None
    assert offer["trip_duration_days"] is None
    assert offer["source_url"].endswith("expected_price=42")
    assert "country" not in offer
    assert "destination_iata" not in offer
    assert offer["warnings"] == []


def test_normalized_card_keeps_transfer_city():
    offer = normalize_card_data(
        {
            "card_text": "Шанхай\nLED Пулково\nPVG Пудун\n14\u200a⁠ч в пути / 1 пересадка\n4\u200a⁠ч Чэнду",
            "destination": "Шанхай",
            "source_url": "https://www.aviasales.ru/search/LED0109PVG1",
            "price_texts": ["26 120 ₽"],
            "text_items": ["Sichuan Airlines", "LED Пулково", "PVG Пудун", "14\u200a⁠ч в пути / 1 пересадка", "4\u200a⁠ч Чэнду"],
            "date_items": ["30 сен"],
            "airlines": ["Sichuan Airlines"],
        },
        "LED",
        "OW",
        date(2026, 9, 22),
    )

    assert offer["transfers"] == ["Чэнду"]
    assert offer["transfer_durations"] == ["4 ч"]


def test_round_trip_extracts_segment_times_and_existing_airline_image_without_extra_lookup():
    offer = normalize_card_data(
        {
            "card_text": "Шанхай\n30 сен\n08:00\n10:00\n12:00\n14:00\n10:00\n16:00\n10 ч в пути / 1 пересадка\n9 ч в пути / Прямой\n4 ч Чэнду",
            "destination": "Шанхай",
            "source_url": "https://www.aviasales.ru/search/LED3009PVG1",
            "price_texts": ["26 120 ₽"],
            "text_items": ["10 ч в пути / 1 пересадка", "9 ч в пути / Прямой"],
            "date_items": ["30 сен", "8 окт"],
            "airlines": ["Sichuan Airlines", "China Eastern"],
            "airline_by_leg": [["Sichuan Airlines"], ["China Eastern"]],
            "airline_logos": [{"name": "Sichuan Airlines", "url": "https://pics.avs.io/200/200/3U.png"}],
        },
        "LED",
        "RT",
        date(2026, 9, 1),
    )

    assert offer["flight_times"] == ["08:00", "10:00", "12:00", "14:00", "10:00", "16:00"]
    assert offer["flight_duration"] == ["10 ч", "9 ч"]
    assert offer["airline_by_leg"] == [["Sichuan Airlines"], ["China Eastern"]]
    assert offer["transfer_durations"] == ["4 ч"]
    assert offer["airline_logos"] == [{"name": "Sichuan Airlines", "url": "https://pics.avs.io/200/200/3U.png"}]


def test_leg_airlines_and_logos_are_kept_when_global_airline_list_is_missing():
    offer = normalize_card_data(
        {
            "card_text": "Санья\n29 сен\n10:00\n18:00\n10 ч в пути / 1 пересадка\n",
            "destination": "Санья",
            "source_url": "https://www.aviasales.ru/search/LED2909SYX1",
            "price_texts": ["56 382 ₽"],
            "text_items": ["10 ч в пути / 1 пересадка"],
            "date_items": ["29 сен"],
            "airlines": [],
            "airline_by_leg": [["Air China"]],
            "airline_logos": [{"name": "Air China", "url": "https://pics.avs.io/200/200/CA.png"}],
        },
        "LED",
        "OW",
        date(2026, 9, 22),
    )

    assert offer["airline"] == ["Air China"]
    assert offer["airline_by_leg"] == [["Air China"]]
    assert offer["airline_logos"] == [{"name": "Air China", "url": "https://pics.avs.io/200/200/CA.png"}]


def test_repeat_registration_notice_is_assigned_to_each_round_trip_leg():
    offer = normalize_card_data(
        {
            "card_text": (
                "Дубай\n30 сен\n19:30\n07:00\nDWC Аль‑Мактум\n"
                "11 ч в пути / 1 пересадка\n3 ч Москва\nповторная регистрация\n"
                "4 окт\n14:30\n23:10\n10 ч в пути / 1 пересадка\n"
                "2 ч Москва\nповторная регистрация"
            ),
            "destination": "Дубай",
            "source_url": "https://www.aviasales.ru/search/LED3009DXB1",
            "price_texts": ["34 747 ₽"],
            "text_items": [
                "11 ч в пути / 1 пересадка",
                "3 ч Москва",
                "повторная регистрация",
                "10 ч в пути / 1 пересадка",
                "2 ч Москва",
                "повторная регистрация",
            ],
            "date_items": ["30 сен", "4 окт"],
        },
        "LED",
        "RT",
        date(2026, 9, 1),
    )

    assert offer["transfers"] == ["Москва", "Москва"]
    assert offer["transfer_durations"] == ["3 ч", "2 ч"]
    assert offer["repeat_registration_by_leg"] == [True, True]


def test_parser_keeps_endpoint_times_when_some_segment_times_are_missing():
    offer = normalize_card_data(
        {
            "card_text": "Шанхай\n30 сен\n08:00\n14:00\n10 ч в пути / 1 пересадка\n9 ч в пути / Прямой",
            "destination": "Шанхай",
            "source_url": "https://www.aviasales.ru/search/LED3009PVG1",
            "price_texts": ["26 120 ₽"],
            "text_items": ["10 ч в пути / 1 пересадка", "9 ч в пути / Прямой"],
            "date_items": ["30 сен", "8 окт"],
            "airlines": [],
        },
        "LED",
        "RT",
        date(2026, 9, 1),
    )

    assert offer["flight_times"] == ["08:00", "14:00"]


def test_parser_keeps_explicit_next_day_arrival_marker():
    offer = normalize_card_data(
        {
            "card_text": "Анталья\n29 сен\n21:15\n16:15 +1\n10 ч в пути / Прямой",
            "destination": "Анталья",
            "source_url": "https://www.aviasales.ru/search/MOW2909AYT1",
            "price_texts": ["12 000 ₽"],
            "text_items": ["10 ч в пути / Прямой"],
            "date_items": ["29 сен"],
            "airlines": [],
        },
        "MOW",
        "OW",
        date(2026, 9, 1),
    )

    assert offer["arrival_day_offsets"] == [1]


def test_parser_finds_next_day_marker_after_invisible_directional_characters():
    offer = normalize_card_data(
        {
            "card_text": "Город\n29 сен\n21:15\n16:15\u2060 +1\n10 ч в пути / Прямой",
            "destination": "Город",
            "source_url": "https://www.aviasales.ru/search/MOW2909XXX1",
            "price_texts": ["12 000 ₽"],
            "text_items": ["10 ч в пути / Прямой"],
            "date_items": ["29 сен"],
            "airlines": [],
        },
        "MOW",
        "OW",
        date(2026, 9, 1),
    )

    assert offer["arrival_day_offsets"] == [1]


def test_parser_derives_next_day_offset_from_explicit_arrival_date():
    offer = normalize_card_data(
        {
            "card_text": (
                "Нячанг\n9 окт\n14:50\n05:15\n10 окт, сб\nCXR Камрань\n"
                "10 ч в пути / Прямой\n16 окт\n05:45\n13:05\n11 ч в пути / Прямой"
            ),
            "destination": "Нячанг",
            "source_url": "https://www.aviasales.ru/search/MOW0910CXR16101",
            "price_texts": ["74 269 ₽"],
            "text_items": ["10 ч в пути / Прямой", "11 ч в пути / Прямой"],
            "date_items": ["9 окт", "16 окт"],
            "airlines": [],
        },
        "MOW",
        "RT",
        date(2026, 9, 25),
    )

    assert offer["arrival_day_offsets"] == [1, None]


def test_parser_derives_next_day_offset_across_new_year():
    offer = normalize_card_data(
        {
            "card_text": "Город\n31 дек\n21:00\n00:20\n1 янв, пт\n5 ч в пути / Прямой",
            "destination": "Город",
            "source_url": "https://www.aviasales.ru/search/MOW3112XXX1",
            "price_texts": ["12 000 ₽"],
            "text_items": ["5 ч в пути / Прямой"],
            "date_items": ["31 дек"],
            "airlines": [],
        },
        "MOW",
        "OW",
        date(2026, 12, 1),
    )

    assert offer["arrival_day_offsets"] == [1]


def test_parser_does_not_claim_global_airline_is_attached_to_each_leg():
    offer = normalize_card_data(
        {
            "card_text": "Направление\n5 окт\n08:00\n12:00\n4 ч в пути / Прямой",
            "destination": "Направление",
            "source_url": "https://www.aviasales.ru/search/MOW0510XXX1",
            "price_texts": ["4 000 ₽"],
            "text_items": ["4 ч в пути / Прямой"],
            "date_items": ["5 окт"],
            "airlines": ["Carrier A"],
            "airline_by_leg": [[]],
        },
        "MOW",
        "OW",
        date(2026, 9, 22),
    )

    assert offer["airline"] == ["Carrier A"]
    assert offer["airline_by_leg"] == [[]]


def test_round_trip_keeps_unresolvable_dates_null_and_retains_raw_values():
    offer = normalize_card_data(
        {
            "card_text": "Город\n21 сен\nПрямой\nна 4 дня",
            "destination": "Город",
            "source_url": "https://www.aviasales.ru/search/MOW2109XXX1",
            "price_texts": ["4 200 ₽"],
            "text_items": ["21 сен", "на 4 дня"],
            "date_items": ["21 сен", "25 сен"],
            "airlines": [],
        },
        "MOW",
        "RT",
        date(2026, 9, 22),
    )

    assert offer["departure_date"] is None
    assert offer["departure_date_raw"] == "21 сен"
    assert offer["return_date"] is None
    assert offer["return_date_raw"] == "25 сен"
    assert offer["trip_duration_days"] == 4
    assert any("before today" in warning for warning in offer["warnings"])
