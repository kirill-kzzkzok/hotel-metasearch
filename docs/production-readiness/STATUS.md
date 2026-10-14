# STATUS — Hotel Metasearch Production-Readiness

Обновляется после каждого логического изменения. DONE = проверено.
Противоречия между этим файлом и репозиторием → проверять фактами (git, тесты, live).

**Миссия активирована:** 2026-10-14 (ТЗ: `MISSION-SPEC.md`).

## Фаза 0 — Baseline и документация — DONE

Зафиксировано 2026-10-14 до начала изменений:

- Ветка `master`, HEAD `7cf348e` = `origin/master` (remote синхронизирован).
- Рабочее дерево чистое; единственный untracked — `docs/production-readiness/` (документация миссии).
- История: `2573f50` (публикация) → `45c2b3b` (Sravni /api/v1) → `de360e8` (spider 4030→blocked)
  → `6de0f4a` (queued-учёт) → `8998b88` (gzip) → `f339d70` (rAF-чанки) → `9e14a4b` (unusable в
  счётчиках) → `1c011c6` (WAL) → `7cf348e` (geo_id-персистентность + тест-изоляция).
- Порты 879x свободны; процессов hotel-серверов нет. Посторонние python-процессы: telegram_bot
  (не относится), browser_harness daemons (Hernes-инфраструктура).
- Production-БД `hot-tickets.sqlite3`: integrity ok, FK-нарушений нет, таблицы
  `hotel_city_reference` (13 городов), `hotel_city_geo_profile` (11), `hotel_geo_feature_cache`
  (137, часть error от 2026-09-28). Таблицы `hotel_city_geo_cache` нет → загрязнения нет.
  ⚠ `journal_mode=delete` на диске (см. DECISIONS: WAL применяется при init через schema.py;
  prod-БД будет переведена при следующем штатном открытии; проверить в live-прогоне).
- Scratch-харнесс `live_headed_search.py:26` — `headless=True` (патч вчерашней сессии для
  фоновых smoke; НЕ является прод-кодом, но запрещён как доказательство работы Trip.com).

## Baseline-тесты (проверено 2026-10-14)

- `pytest` полный набор: **618 passed** (команда: `py -3.12 -X utf8 -m pytest -q`).
- `py -3.12 -X utf8 -m compileall -q aviasales_collector tests` — чисто.
- `node --check` (hotels.js, hotel_search_lifecycle.js) + `node --test
  tests/hotel_search_lifecycle.test.cjs` — **7/7 pass**.

## Фаза 1 — Инвариант headed-only Trip.com — DONE

- **Фикс `4feb1f8`:** `tripcom_rates.py:511` → `headless=False` (rate-браузер Trip.com
  был headless с исходного дропа `2573f50`). Regression-гвард `tests/test_tripcom_headed_invariant.py`
  (4 теста: AST-проверка launch-конфигураций всех Trip-файлов + запрет `headless=True` в них).
  Targeted: 16 passed (rate-диагностика + enrichment + meal). Живое headed-подтверждение —
  в smoke-фазе (задача 13). Discovery/login/headed остались нетронуты.
- Scratch `live_headed_search.py:26` — вне репо, не прод-код; будет переведён в headed при
  следующем использовании.

## Фаза 2 — Flaky-тест и изоляция — DONE

- **Фикс `8e6c075`:** conftest-фикстура переписана на точное восстановление: полный replace
  содержимого словарей из снапшота (удаляет добавленное тестом), try/finally, охват всех
  общих mutable-глобалов geo (PHOTON_CITY_EXTENTS, lock-карты, cooldowns, streaks) + скаляры
  пейсинга. `tests/test_geo_global_isolation.py`: flaky-кейс 5× в одном процессе, leak-проба,
  прямая проверка teardown (добавленное удалено, существующее восстановлено), production-кэш
  работает. 627 passed × 2 полных прогона.

## Фаза 3 — WAL и geo-кэш — DONE

- **`4737eb4`:** WAL применяется через настоящий `init_hotel_store` на копии prod-БД
  (delete → wal, integrity ok; путь: `web.py:413 create_app` → init). Кросс-процессный
  geo-кэш: запись в child-процессе с явным `HOT_TICKETS_DB_PATH` → чтение свежим процессом;
  без env — таблица в prod-БД не появляется; не-цифровые geo_id фильтруются. 630 passed.

## Фаза 4 — Пляжи Дананга — DONE

- **Первопричина (доказано на smoke-БД 2026-10-14):** вечерняя нестабильность Overpass
  (ReadTimeout/504, кулдауны до 112с) → 16/17 тайлов Дананга error → photon-fallback возвращал
  пляжи только Point-центроидами → резолвер отказывался от всех кандидатов → 225/233 отелей
  «unknown» без дистанции, хотя Mỹ Khê — реальный natural=beach в ~300м (живой probe Overpass:
  relation 19000664).
- **Фикс `c31e24a`:** в photon-ветке резолвится настоящая геометрия кандидатов через
  nominatim по osm_id (`_beach_geometry`, существовавший, но непоключённый; кэш 30 дней),
  лимит 5 кандидатов/3 геометрии. Метод `photon_resolved_beach_geometry`, confidence medium
  (ближайшесть не доказана именным поиском). Неразрешённые кандидаты — по-прежнему честный
  unknown; Point-центроиды отказываются; 429 не триггерит fallback. Фильтры cache-hit/
  read_model/core принимают новые строки. 3 старых policy-теста обновлены (анти-фрод
  assertions сохранены), новый файл тестов `test_beach_photon_geometry_fallback.py`.
- **Проверка:** 634 passed × 2, compileall, node 7/7. Живой smoke Дананга — в задаче 13.

## Фаза 5 — Альтернативные номера Sravni — DONE (отрицательный результат)

- **Факт (по live raw pages):** контракт Sravni `/api/v1/search` даёт на отель
  `offers[]` по одной на провайдера (yandex/ostrovok/tutu — разные OTA), без room-вариантов.
  Повторных офферов одного провайдера нет (0 из всех отелей в снапшоте). Endpoint
  hotel-rooms не существует. «Альтернативные варианты» Sravni = разные OTA-цены, это уже
  показывается общей моделью карточек.
- **Решение (spec §6 «не имитировать альтернативы, которых источник не даёт»):** не
  добавлять. Зафиксировано в DECISIONS (D9).

## Фаза 6 — Питание/удобства — DONE (проверено, без дефекта кода)

- Meal-классификация (`classify_meal_plan`, meal_v4): разбор value/labels/text, EN+RU,
  включённый/доплатный завтрак, HB/FB/AI, негативные паттерны. Покрытие по фактам источников:
  Sravni 184 breakfast / 752 unknown (label отсутствует = честный unknown; API больше не даёт),
  Trip rate-модель богата (breakfast_included/available, meal_available, meal_confidence,
  meal_extra_amount). Удобства извлекаются только из структурированных полей (не по названию
  и звёздам); отсутствующее ≠ подтверждённо отсутствующее. Дополнительный скрейп 400
  deeplink-страниц за поиск признан непропорциональным — источник-ограничение зафиксировано.

## Фаза 7 — «Личный номер» — DONE (семантика уже корректна, добавлены регрессии)

- `classify_room_privacy`: shared (dorm/bed-in/capsule/койко) ≠ private; bed_type/название
  тарифа сами по себе ничего не доказывают; unknown остаётся unknown. UI-режим 'private'
  фильтрует офферы строго по `room_privacy == "private"`. Тесты: `7542685`
  (17 кейсов EN+RU + проекция + live-source гвард предиката в `_finalize_search`).

## Фаза 8 — Кнопка отзывов Sravni — DONE (`36d97f9`)

- Первопричина: `canonical_url` (реальная страница отеля с отзывами на hotels.travel.ru)
  отбрасывался нормализацией → у фронта не было кандидата URL. Фикс: `source_hotel_url`
  через normalize→read_model→`reviewLink()`. Живая страница проверена (HTTP 200, отзывы).
  6 тестов, полный набор 657 passed.

## Фаза 9 — Exit code 3 — DONE (контракт подтверждён)

- D10: CTRL_BREAK → uvicorn graceful shutdown (логи подтверждают), код 3 = штатное внешнее
  завершение Windows console-процесса. Не дефект.

## Фаза 10 — Живые smoke — IN PROGRESS

- **Headed smoke Дананга (2026-10-14, после всех фиксов):** first_card 11.6с, 538 отелей
  (Sravni 400 partial/limit + Trip 150 ok), 950 офферов, 181 карточка, 0 ошибок UI,
  exit 3 (контракт). **Пляжи Дананга: 200 ok-дистанций через новый
  `photon_resolved_beach_geometry`** (Mỹ Khê, 112м–2.9км, реальные значения), 5 ok через
  overpass-геометрию, 48 unknown + 13 error — честные статусы неразрешённых кандидатов.
  До фикса в тех же условиях: 8 ok / 225 unknown. Улучшение покрытия ~25×.
- Harness: headed Chromium восстановлен (`live_headed_search.py:26` headless=False).
- **Инвентаризация фильтров (§10):** все 26 интерактивных контролов hotels.html
  подключены к hotels.js; применение каждого проверено статически (price/rating/stars/
  reviews/meal/cancellation/amenities/center/beach-distance/beach-line/room-mode/sort/
  view). Кнопки отзывов: фиксировано (`36d97f9`).
- Осталось: полный full-completion smoke с ожиданием rate-пула, failure/cancellation
  smoke, мультигородовая матрица (Париж и др.), серия прогрессивных smoke.

Аудит всех мест создания Chromium (grep `launch`/`headless` по `aviasales_collector/`):

| Место | Режим | Статус |
|---|---|---|
| `hotels/sources/tripcom.py:83` (discovery, persistent profile) | `headless=False` | OK |
| `hotels/sources/tripcom_rates.py:511` (rate-браузер, persistent profile) | **`headless=True`** | **P0 НАРУШЕНИЕ** — реальный путь Trip.com rate-задач, headless с самого `2573f50` |
| `tripcom_login.py:21` (авторизация) | `headless=False` | OK |
| `collector.py:58` + `cli.py:34` (авиасейлс-коллектор, не Trip) | headless по флагу CLI | вне инварианта (не Trip.com) |

План: перевести `tripcom_rates.py:511` в `headless=False`, добавить regression-тест
(запрет headless в launch-конфигурации реального Trip-пути), smoke в headed.
Риск: фоновые прогоны rate-пула на машине без дисплея → зафиксировать как блокер, не
подменяя headless-прогоном.

## Реестр задач

| # | Задача | Приоритет | Статус |
|---|---|---|---|
| 0 | Baseline + GOAL/STATUS/DECISIONS + первый коммит | — | IN PROGRESS |
| 1 | Headed-only инвариант: фикс `tripcom_rates.py:511` + regression-тест | P0 | TODO |
| 2 | Flaky-тест: точное восстановление глобального состояния в conftest (`.update()` → точный rollback) + regression-тесты | P0 | TODO |
| 3 | WAL на prod-БД: проверить применение при штатном запуске | P0 | TODO |
| 4 | Geo-кэш между процессами: подтверждение чтения/записи geo_id на штатной конфигурации | P0 | TODO |
| 5 | Пляжи Дананга: первопричина отсутствия расстояний; общий алгоритм | P0/P1 | TODO |
| 6 | Унификация карточек: альтернативные номера Sravni (что реально даёт API) | P1 | TODO |
| 7 | Питание/удобства: полнота извлечения | P1 | TODO |
| 8 | Фильтр «Личный номер»: семантика приватности | P1 | TODO |
| 9 | Кнопка отзывов Sravni + инвентаризация фильтров | P1 | TODO |
| 10 | Exit code 3 сервера: подтвердить контракт | P1 | TODO |
| 11 | Прогрессивность: первая карточка, счётчики, изоляция поисков | P2 | TODO |
| 12 | Матрица live-городов (Дананг/Париж/приморский+метро/без всего) | P1 | TODO |
| 13 | Progressive / full-completion / failure smokes | P1 | TODO |
| 14 | Субагенты по независимым задачам (нужны >1 параллельных) | по мере | TODO |

## Известные внешние ограничения (фиксируются, не «чинятся» обходом)

- Overpass (OSM): вечерние ReadTimeout/504, кулдауны до ~128с — гео-обогащение замедляется,
  система деградирует честно (unknown-статусы).
- Trip-антибот: сессионная блокировка; spider 4030 уже классифицируется как `blocked`.
- VPN на хосте ломает Trip-навигацию (domcontentloaded не наступает) — smoke без VPN.

## Субагенты

Нет активных. delegate_task в этой среде конфигурирован на несуществующего провайдера
(`brataann`) — при необходимости параллельной работы использовать прямые tools;
проверить доступность делегирования заново при первой реальной потребности.

## Следующее действие

1. Зафиксировать документацию отдельным коммитом (`docs: establish production readiness tracking`).
2. Приступить к задаче 1 (headed-only фикс `tripcom_rates.py:511`) по циклу
   диагностика → фикс → regression → коммит.
