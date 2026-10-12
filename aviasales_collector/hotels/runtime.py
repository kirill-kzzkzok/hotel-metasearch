"""Shared bounded worker, transport, and profiling infrastructure."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any
from urllib.request import urlopen

log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024

EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hotel-source")

BACKGROUND_SEARCH_TASKS: set[asyncio.Task[Any]] = set()

_SEARCH_TIMELINE_LOCK = threading.Lock()
_SEARCH_TIMELINE_STARTS: dict[str, float] = {}
_ACTIVE_SEARCH_TIMELINE_ID: ContextVar[str] = ContextVar("hotel_search_timeline_id", default="")

def start_search_timeline(search_id: str) -> None:
    """Keep an in-process monotonic origin for correlated hotel-search diagnostics."""
    with _SEARCH_TIMELINE_LOCK:
        _SEARCH_TIMELINE_STARTS[search_id] = time.perf_counter()
        while len(_SEARCH_TIMELINE_STARTS) > 512:
            _SEARCH_TIMELINE_STARTS.pop(next(iter(_SEARCH_TIMELINE_STARTS)))

def log_search_timeline(search_id: str, event: str, **details: Any) -> None:
    """Emit a UTC and search-relative timestamp without adding persistent state/schema."""
    search_id = search_id or _ACTIVE_SEARCH_TIMELINE_ID.get()
    with _SEARCH_TIMELINE_LOCK:
        started = _SEARCH_TIMELINE_STARTS.get(search_id)
    elapsed = (time.perf_counter() - started) if started is not None else None
    values = " ".join(f"{key}={value}" for key, value in details.items())
    log.info("[HOTEL_TIMELINE] search_id=%s event=%s utc=%s t_plus_s=%s %s",
             search_id, event, datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
             f"{elapsed:.3f}" if elapsed is not None else "unknown", values)

_ACTIVE_ENRICHMENT_PROFILE: ContextVar[Any | None] = ContextVar("hotel_enrichment_profile", default=None)

_DEFAULT_URLOPEN = urlopen

def _safe_json_loads(value: Any, fallback: Any) -> Any:
    """Decode persisted/cache JSON without turning one bad row into a failed search."""
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback

def _safe_json_list(value: Any) -> list[Any]:
    decoded = _safe_json_loads(value, [])
    return decoded if isinstance(decoded, list) else []

class _EnrichmentProfile:
    """Thread-safe per-hotel stage, HTTP, and SQLite measurements."""

    def __init__(self, hotel_name: str):
        self.hotel_name = hotel_name
        self.started = time.perf_counter()
        self.lock = threading.Lock()
        self.stages_ms: dict[str, float] = collections.defaultdict(float)
        self.http_ms: dict[str, float] = collections.defaultdict(float)
        self.http_open_ms: dict[str, float] = collections.defaultdict(float)
        self.http_read_ms: dict[str, float] = collections.defaultdict(float)
        self.http_phase_ms: dict[str, dict[str, float]] = collections.defaultdict(dict)
        self.http_bytes: dict[str, int] = collections.defaultdict(int)
        self.actual_http_requests = 0
        self.http_attempts = 0
        self.sqlite_queries = 0
        self.sqlite_commits = 0
        self.sqlite_exec_ms = 0.0
        self.sqlite_connection_ms = 0.0
        self.cpu_ms = 0.0
        self.wait_ms = 0.0
        self.cache_hits: collections.Counter[str] = collections.Counter()
        self.cache_misses: collections.Counter[str] = collections.Counter()

    def add_stage(self, name: str, elapsed_ms: float) -> None:
        with self.lock:
            self.stages_ms[name] += max(0.0, elapsed_ms)

    def add_http_open(self, name: str, elapsed_ms: float) -> None:
        with self.lock:
            self.actual_http_requests += 1
            self.http_ms[name] += max(0.0, elapsed_ms)
            self.http_open_ms[name] += max(0.0, elapsed_ms)

    def add_http_attempt(self) -> None:
        with self.lock:
            self.http_attempts += 1

    def add_http_read(self, name: str, elapsed_ms: float) -> None:
        with self.lock:
            self.http_ms[name] += max(0.0, elapsed_ms)
            self.http_read_ms[name] += max(0.0, elapsed_ms)

    def add_http_phase(self, name: str, phase: str, elapsed_ms: float) -> None:
        with self.lock:
            self.http_phase_ms[name][phase] = max(0.0, elapsed_ms)

    def add_http_bytes(self, name: str, size: int) -> None:
        with self.lock:
            self.http_bytes[name] += max(0, int(size))

    def add_sql(self, elapsed_ms: float) -> None:
        with self.lock:
            self.sqlite_queries += 1
            self.sqlite_exec_ms += max(0.0, elapsed_ms)

    def add_sql_count(self) -> None:
        with self.lock:
            self.sqlite_queries += 1

    def add_commit(self, elapsed_ms: float) -> None:
        with self.lock:
            self.sqlite_commits += 1
            self.sqlite_exec_ms += max(0.0, elapsed_ms)

    def add_sqlite_connection(self, elapsed_ms: float) -> None:
        with self.lock:
            self.sqlite_connection_ms += max(0.0, elapsed_ms)

    def add_cpu(self, elapsed_ms: float) -> None:
        with self.lock:
            self.cpu_ms += max(0.0, elapsed_ms)

    def add_wait(self, elapsed_ms: float) -> None:
        with self.lock:
            self.wait_ms += max(0.0, elapsed_ms)

    def add_cache(self, name: str, hit: bool) -> None:
        with self.lock:
            (self.cache_hits if hit else self.cache_misses)[name] += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            stages = dict(self.stages_ms)
            http = dict(self.http_ms)
            http_open = dict(self.http_open_ms)
            http_read = dict(self.http_read_ms)
            http_phases = {name: dict(phases) for name, phases in self.http_phase_ms.items()}
            http_bytes = dict(self.http_bytes)
            requests = self.actual_http_requests
            attempts = self.http_attempts
            query_count, commit_count = self.sqlite_queries, self.sqlite_commits
            db_exec_ms, db_connection_ms = self.sqlite_exec_ms, self.sqlite_connection_ms
            cpu_ms, wait_ms = self.cpu_ms, self.wait_ms
            cache_hits, cache_misses = dict(self.cache_hits), dict(self.cache_misses)
        network_ms = sum(http.values())
        total_ms = (time.perf_counter() - self.started) * 1000
        return {
            "stages_ms": stages, "http_ms": http,
            "http_open_ms": http_open, "http_read_ms": http_read,
            "http_phases_ms": http_phases,
            "http_bytes": http_bytes,
            "actual_http_requests": requests, "http_attempts": attempts, "network_ms": network_ms,
            "external_api_ms": network_ms, "sqlite_queries": query_count,
            "sqlite_commits": commit_count, "db_exec_ms": db_exec_ms,
            "db_connection_wall_ms": db_connection_ms,
            "cpu_ms": cpu_ms, "waiting_ms": wait_ms,
            "cache_hits": cache_hits, "cache_misses": cache_misses,
            "total_ms": total_ms,
        }

class _ProfiledHTTPResponse:
    def __init__(self, response: Any, profile: _EnrichmentProfile | None, stage: str):
        self._response = response
        self._profile = profile
        self._stage = stage

    def __enter__(self):
        entered = self._response.__enter__() if hasattr(self._response, "__enter__") else self._response
        if entered is not self._response:
            self._response = entered
        return self

    def __exit__(self, *args):
        if hasattr(self._response, "__exit__"):
            return self._response.__exit__(*args)
        self.close()
        return False

    def read(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._response.read(*args, **kwargs)
        finally:
            if self._profile:
                self._profile.add_http_read(self._stage, (time.perf_counter() - started) * 1000)

    def read1(self, *args, **kwargs):
        reader = getattr(self._response, "read1", self._response.read)
        started = time.perf_counter()
        try:
            return reader(*args, **kwargs)
        finally:
            if self._profile:
                self._profile.add_http_read(self._stage, (time.perf_counter() - started) * 1000)

    def __getattr__(self, name: str):
        return getattr(self._response, name)

def _profiled_urlopen(request: Any, timeout: float, stage: str):
    profile = _ACTIVE_ENRICHMENT_PROFILE.get()
    started = time.perf_counter()
    try:
        response = urlopen(request, timeout=timeout)
    finally:
        if profile:
            profile.add_http_open(stage, (time.perf_counter() - started) * 1000)
    return _ProfiledHTTPResponse(response, profile, stage)

def _read_http_body_bounded(response: Any, max_bytes: int, deadline_seconds: float) -> bytes:
    """Read an HTTP body with both a byte cap and an absolute wall deadline.

    Socket timeouts alone can be extended indefinitely by a server that keeps
    trickling response bytes. read1 lets us re-check the total deadline between
    chunks while retaining the complete response whenever it arrives in time.
    """
    deadline = time.monotonic() + max(0.1, deadline_seconds)
    body = bytearray()
    reader = getattr(response, "read1", response.read)
    raw = getattr(response, "_response", response)
    sock = getattr(getattr(getattr(raw, "fp", None), "raw", None), "_sock", None)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP response exceeded wall deadline")
        if sock is not None:
            sock.settimeout(remaining)
        chunk = reader(min(64 * 1024, max_bytes + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError("HTTP response exceeded size limit")

class _ProfiledSQLiteConnection:
    def __init__(self, connection: sqlite3.Connection, profile: _EnrichmentProfile):
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_profile", profile)
        object.__setattr__(self, "_opened", time.perf_counter())
        connection.set_trace_callback(lambda _sql: self._record_query())

    def _record_query(self) -> None:
        profile = object.__getattribute__(self, "_profile")
        profile.add_sql_count()

    def execute(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._connection.execute(*args, **kwargs)
        finally:
            self._profile.add_sql((time.perf_counter() - started) * 1000)

    def executemany(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._connection.executemany(*args, **kwargs)
        finally:
            self._profile.add_sql((time.perf_counter() - started) * 1000)

    def executescript(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            return self._connection.executescript(*args, **kwargs)
        finally:
            self._profile.add_sql((time.perf_counter() - started) * 1000)

    def commit(self):
        started = time.perf_counter()
        try:
            return self._connection.commit()
        finally:
            self._profile.add_commit((time.perf_counter() - started) * 1000)

    def close(self):
        try:
            return self._connection.close()
        finally:
            self._profile.add_sqlite_connection((time.perf_counter() - self._opened) * 1000)

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

def _profiled_sqlite_connect(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    profile = _ACTIVE_ENRICHMENT_PROFILE.get()
    return _ProfiledSQLiteConnection(connection, profile) if profile else connection

def _profiled_worker_call(function, *args, **kwargs):
    profile = _ACTIVE_ENRICHMENT_PROFILE.get()
    started = time.thread_time()
    try:
        return function(*args, **kwargs)
    finally:
        if profile:
            profile.add_cpu((time.thread_time() - started) * 1000)

def _record_cache(name: str, hit: bool) -> None:
    profile = _ACTIVE_ENRICHMENT_PROFILE.get()
    if profile:
        profile.add_cache(name, hit)
    log.info("[CACHE] %s %s", name, "HIT" if hit else "MISS")
