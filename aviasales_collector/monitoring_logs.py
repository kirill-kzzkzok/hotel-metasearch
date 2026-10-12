"""Human-readable presentation helpers for aviation monitoring logs."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import Any


_HUMAN_MONITORING_RECORD = "human_monitoring_record"


class MonitoringFormatter(logging.Formatter):
    """Use a compact Russian-console shape only for explicitly marked events."""

    def format(self, record: logging.LogRecord) -> str:
        if getattr(record, _HUMAN_MONITORING_RECORD, False):
            return f"{self.formatTime(record, self.datefmt)}  {record.getMessage()}"
        return super().format(record)


def log_monitoring(
    logger: logging.Logger,
    level: int,
    message: str,
    *args: Any,
) -> None:
    logger.log(level, message, *args, extra={_HUMAN_MONITORING_RECORD: True})


def configure_utf8_console() -> None:
    """Keep Unicode log text intact when the bot is launched outside its .cmd wrapper."""
    # ``pythonw.exe`` has no console streams. Some libraries (notably Uvicorn)
    # still probe ``isatty`` during startup, so give them quiet file-like sinks.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                # Embedded consoles and test capture streams may not permit reconfiguration.
                pass


def format_trip_type(trip_type: str) -> str:
    labels = {"OW": "в одну сторону", "RT": "туда-обратно"}
    normalized = str(trip_type or "").upper()
    return labels.get(normalized, normalized or "неизвестный режим")


def format_price_alert(
    *,
    origin: str,
    destination: str,
    current_price: int,
    previous_price: int | None,
    previous_change_amount: int | float | None,
    previous_change_percent: int | float | None,
    historical_low: int | float | None,
    observation_count: int,
    reason: str,
    currency: str,
) -> str:
    """Render the evaluator's existing values; this function does no scoring."""
    route = f"{origin} → {destination}"
    price = _money(current_price, currency)
    observations = _observation_count(observation_count)

    if reason == "insufficient_baseline":
        return f"⏳ {route} · {price} · {observations} · недостаточно данных"

    if previous_price is None:
        return f"🔎 {route} · {price} · {observations}"

    if current_price < previous_price:
        change = _signed_money(previous_change_amount, currency)
        percent = _signed_percent(previous_change_percent)
        movement = f"{_money(previous_price, currency)} → {price}"
        if change is not None:
            movement += f" · {change}"
        if percent is not None:
            movement += f" ({percent})"
        return f"🔥 {route} · {movement}"

    if current_price > previous_price:
        change = _signed_money(previous_change_amount, currency)
        movement = f"{_money(previous_price, currency)} → {price}"
        if change is not None:
            movement += f" · {change}"
        return f"📈 {route} · {movement}"

    details = ["без изменения"]
    if historical_low is not None and historical_low < current_price:
        details.append(f"минимум {_money(historical_low, currency)}")
    details.append(observations)
    return f"🔎 {route} · {price} · {' · '.join(details)}"


def describe_error(error: BaseException | str) -> str:
    """Translate recognizable transport failures without printing exception dumps."""
    if isinstance(error, BaseException):
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(getattr(error, "response", None), "status_code", None)
        if status is None:
            status = getattr(error, "code", None)
        error_type = type(error).__name__
        detail = str(error)
    else:
        status = None
        error_type = ""
        detail = str(error)

    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None

    text = f"{error_type} {detail}".casefold()
    if status == 429 or re.search(r"\b(?:http|status(?: code)?)\D{0,5}429\b|\b429\s+(?:too many|rate)", text):
        return "временное ограничение запроса"
    if status is not None and 500 <= status <= 599:
        return "ошибка сервера"
    if re.search(r"\b(?:http|status(?: code)?)\D{0,5}5\d\d\b", text):
        return "ошибка сервера"
    if "timeout" in error_type.casefold() or re.search(r"\btime(?:d)? out\b", text):
        return "таймаут"
    if status is not None and 400 <= status <= 499:
        return "ошибка запроса"
    return "ошибка выполнения"


def log_monitoring_exception(
    logger: logging.Logger,
    *,
    message: str,
    error: BaseException,
    debug_context: str,
    args: tuple[Any, ...] = (),
) -> None:
    error_label = describe_error(error)
    all_args = (*args, error_label)
    log_monitoring(logger, logging.ERROR, message, *all_args)
    logger.debug(
        "[DEBUG][%s] " + message,
        debug_context,
        *all_args,
        exc_info=(type(error), error, error.__traceback__),
    )


def scheduler_clock_time(deadline: float, monotonic_now: float) -> str:
    """Render a scheduler's already-computed monotonic deadline as local HH:MM."""
    remaining = max(0.0, float(deadline) - float(monotonic_now))
    return time.strftime("%H:%M", time.localtime(time.time() + remaining))


def summarize_warnings(warnings: Any) -> str:
    if not isinstance(warnings, (list, tuple)):
        warnings = [warnings] if warnings else []
    useful = [str(item) for item in warnings if item]
    for warning in useful:
        explanation = describe_error(warning)
        if explanation != "ошибка выполнения":
            return explanation
    if useful:
        return f"предупреждений: {len(useful)}"
    return "причина не указана"


def _observation_count(count: int) -> str:
    count = max(0, int(count))
    last_two = count % 100
    last = count % 10
    if 11 <= last_two <= 14:
        noun = "наблюдений"
    elif last == 1:
        noun = "наблюдение"
    elif 2 <= last <= 4:
        noun = "наблюдения"
    else:
        noun = "наблюдений"
    return f"{count} {noun}"


def _number(value: int | float) -> str:
    numeric = float(value)
    if numeric.is_integer():
        rendered = f"{int(numeric):,}"
    else:
        rendered = f"{numeric:,.2f}".rstrip("0").rstrip(".")
    return rendered.replace(",", " ").replace(".", ",")


def _money(value: int | float, currency: str) -> str:
    suffix = "₽" if str(currency).upper() == "RUB" else str(currency).upper()
    return f"{_number(value)} {suffix}"


def _signed_money(value: int | float | None, currency: str) -> str | None:
    if value is None:
        return None
    sign = "−" if value < 0 else "+" if value > 0 else ""
    return f"{sign}{_money(abs(value), currency)}"


def _signed_percent(value: int | float | None) -> str | None:
    if value is None:
        return None
    sign = "−" if value < 0 else "+" if value > 0 else ""
    rendered = f"{abs(float(value)):.1f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{sign}{rendered}%"
