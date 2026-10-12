"""City suggestion orchestration across confirmed Sravni IDs and translated aliases."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlencode

from .models import SourceError
from .normalization import _city_label_distance, _normalize_city_label
from .runtime import EXECUTOR
from .sources.sravni import (CITY_AUTOCOMPLETE_URL, SRAVNIOTEL_SOURCE,
                             SravniOtelSource, _request_json)


async def suggest_cities(query: str) -> tuple[list[dict[str, Any]], int, int]:
    source = SRAVNIOTEL_SOURCE
    if not isinstance(source, SravniOtelSource):
        raise SourceError("unavailable", "City suggestions are unavailable")
    normalized_query = _normalize_city_label(query)
    started = time.perf_counter()
    try:
        direct, http_status, _latency = await source.suggest(query)
    except SourceError:
        direct, http_status = [], None
    if direct:
        max_label_distance = 1 if len(normalized_query) >= 5 else 0
        ranked_matches = [
            item for item in direct
            if _city_label_distance(query, item["name"]) <= max_label_distance
        ]
        if ranked_matches:
            return ranked_matches, http_status or 200, int((time.perf_counter() - started) * 1000)

    loop = asyncio.get_running_loop()
    try:
        aliases = await loop.run_in_executor(EXECUTOR, _travelpayouts_city_aliases, query)
    except SourceError:
        aliases = []

    if not aliases:
        if direct:
            return [], http_status or 200, int((time.perf_counter() - started) * 1000)
        raise SourceError("unavailable", "City lookup is temporarily unavailable",
                          http_status=http_status)

    # Only use the airport autocomplete to translate a label. Search IDs still come
    # from SravniOtel; an IATA code is never treated as a hotel geo_id.
    aliases = aliases[:1]
    lookups = await asyncio.gather(
        *(source.suggest(alias["name"]) for alias in aliases), return_exceptions=True
    )
    confirmed: list[dict[str, Any]] = []
    for alias, response in zip(aliases, lookups, strict=True):
        if isinstance(response, Exception):
            continue
        cities, _status, _elapsed = response
        alias_name = _normalize_city_label(alias["name"])
        alias_country = _normalize_city_label(alias.get("country") or "")
        matching_names = [city for city in cities
                          if _normalize_city_label(city["name"]) == alias_name]
        country_matches = [city for city in matching_names
                           if alias_country and _normalize_city_label(city.get("country") or "") == alias_country]
        if country_matches:
            confirmed.extend(country_matches)
        elif len(matching_names) == 1:
            confirmed.extend(matching_names)

    if confirmed:
        return confirmed[:8], 200, int((time.perf_counter() - started) * 1000)

    # If the hotel catalog cannot resolve this city, keep the independent Trip.com
    # search usable. geo_id remains null and the caller skips SravniOtel.
    alias = aliases[0]
    trip_only = [{"geo_id": None, "name": alias["name"], "country": alias.get("country"),
                  "city_slug": None, "country_slug": None, "iata_code": alias.get("iata_code"),
                  "resolution_source": "travelpayouts"}]
    return trip_only, 200, int((time.perf_counter() - started) * 1000)

def _travelpayouts_city_aliases(query: str) -> list[dict[str, Any]]:
    url = f"{CITY_AUTOCOMPLETE_URL}?{urlencode({'term': query, 'locale': 'ru', 'types[]': 'city'})}"
    payload, _status, _latency = _request_json(url, timeout=5.0)
    aliases = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or item.get("type") != "city":
            continue
        name = " ".join(str(item.get("name") or "").split())
        if not name:
            continue
        aliases.append({"name": name, "country": item.get("country_name"),
                        "iata_code": item.get("code")})
        if len(aliases) == 8:
            break
    return aliases
