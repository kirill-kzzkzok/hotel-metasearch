import logging
import re
import time

from aviasales_collector.monitoring_logs import (
    MonitoringFormatter,
    describe_error,
    format_price_alert,
    format_trip_type,
    log_monitoring_exception,
    scheduler_clock_time,
)


def _price_log(**overrides):
    values = {
        "origin": "LED",
        "destination": "Дубай",
        "current_price": 41200,
        "previous_price": 41200,
        "previous_change_amount": 0,
        "previous_change_percent": 0,
        "historical_low": 39800,
        "observation_count": 3,
        "reason": "not_below_historical_reference",
        "currency": "RUB",
    }
    values.update(overrides)
    return format_price_alert(**values)


def test_price_log_is_short_and_uses_real_previous_and_historical_low_values():
    assert _price_log(
        destination="Стамбул", current_price=8741, previous_price=8741,
        historical_low=8741,
    ) == "🔎 LED → Стамбул · 8 741 ₽ · без изменения · 3 наблюдения"
    assert _price_log(
        destination="Москва", current_price=4169, previous_price=4169,
        historical_low=4149,
    ) == "🔎 LED → Москва · 4 169 ₽ · без изменения · минимум 4 149 ₽ · 3 наблюдения"


def test_price_log_renders_decrease_increase_and_insufficient_data():
    assert _price_log(
        current_price=40500, previous_price=43000,
        previous_change_amount=-2500, previous_change_percent=-5.81,
    ) == "🔥 LED → Дубай · 43 000 ₽ → 40 500 ₽ · −2 500 ₽ (−5,8%)"
    assert _price_log(
        current_price=41200, previous_price=40500,
        previous_change_amount=700, previous_change_percent=1.73,
    ) == "📈 LED → Дубай · 40 500 ₽ → 41 200 ₽ · +700 ₽"
    assert _price_log(
        destination="Тбилиси", current_price=13798, previous_price=13798,
        observation_count=2, reason="insufficient_baseline",
    ) == "⏳ LED → Тбилиси · 13 798 ₽ · 2 наблюдения · недостаточно данных"
    assert _price_log(
        destination="Тбилиси", current_price=13798, previous_price=13798,
        observation_count=1, reason="insufficient_baseline",
    ) == "⏳ LED → Тбилиси · 13 798 ₽ · 1 наблюдение · недостаточно данных"


def test_monitoring_formatter_compacts_only_marked_monitoring_records():
    formatter = MonitoringFormatter(
        "[%(asctime)s] %(levelname)s %(name)s %(message)s", datefmt="%H:%M:%S"
    )
    formatter.converter = time.gmtime
    record = logging.LogRecord("test.monitor", logging.INFO, "test.py", 1, "🔎 LED → Сочи", (), None)
    record.created = 0
    record.human_monitoring_record = True
    assert formatter.format(record) == "00:00:00  🔎 LED → Сочи"
    assert re.match(r"\[.*\] INFO test\.monitor", formatter.format(
        logging.LogRecord("test.monitor", logging.INFO, "test.py", 1, "technical", (), None)
    ))


def test_monitoring_error_and_trip_labels_use_known_semantics():
    assert describe_error("TimeoutError: operation timed out") == "таймаут"
    assert describe_error("HTTP 429 Too Many Requests") == "временное ограничение запроса"
    assert describe_error("HTTP 503 Service Unavailable") == "ошибка сервера"
    assert format_trip_type("OW") == "в одну сторону"
    assert format_trip_type("RT") == "туда-обратно"
    # REVERSE is not a valid stored monitoring trip_type, so leave it uninterpreted.
    assert format_trip_type("REVERSE") == "REVERSE"


def test_scheduler_clock_displays_its_computed_deadline(monkeypatch):
    import aviasales_collector.monitoring_logs as monitor_logs

    monkeypatch.setattr(monitor_logs.time, "time", lambda: 3600)
    monkeypatch.setattr(monitor_logs.time, "localtime", time.gmtime)
    assert scheduler_clock_time(deadline=700, monotonic_now=100) == "01:10"


def test_monitoring_error_is_short_in_info_and_keeps_traceback_in_debug(caplog):
    logger = logging.getLogger("test.monitoring.error")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    try:
        raise TimeoutError("upstream request timed out after 30 seconds")
    except TimeoutError as error:
        log_monitoring_exception(
            logger,
            message="❌ %s · ошибка проверки: %s",
            error=error,
            debug_context="SCAN",
            args=("LED",),
        )

    info_record, debug_record = caplog.records[-2:]
    assert info_record.levelno == logging.ERROR
    assert info_record.getMessage() == "❌ LED · ошибка проверки: таймаут"
    assert debug_record.levelno == logging.DEBUG
    assert debug_record.exc_info is not None


def test_console_setup_supports_pythonw_without_stdout_or_stderr(monkeypatch):
    import aviasales_collector.monitoring_logs as monitor_logs

    monkeypatch.setattr(monitor_logs.sys, "stdout", None)
    monkeypatch.setattr(monitor_logs.sys, "stderr", None)

    monitor_logs.configure_utf8_console()

    stdout, stderr = monitor_logs.sys.stdout, monitor_logs.sys.stderr
    try:
        assert stdout is not None and callable(stdout.isatty)
        assert stderr is not None and callable(stderr.isatty)
    finally:
        stdout.close()
        stderr.close()
