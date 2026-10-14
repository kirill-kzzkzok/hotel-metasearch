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

## Фаза 1 — Инвариант headed-only Trip.com — IN PROGRESS

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
