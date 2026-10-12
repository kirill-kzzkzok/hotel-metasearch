"""Flight identity from ticket itineraries, independent of a UI destination label."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs


# Aviasales searches can use city codes while tickets use individual airports.
CITY_AIRPORTS = {
    "BAK": {"BAK", "GYD"},
    "BJS": {"BJS", "PEK", "PKX"},
    "CTU": {"CTU", "TFU"},
    "MIL": {"MIL", "MXP", "LIN", "BGY"},
    "ROM": {"ROM", "FCO", "CIA"},
    "NYC": {"NYC", "JFK", "LGA", "EWR"},
    "NHA": {"NHA", "CXR"},
    "ULY": {"ULY", "ULV"},
    "RTW": {"RTW", "GSV"},
    "GOI": {"GOI", "GOX"},
    "IZM": {"IZM", "ADB"},
    "MOW": {"MOW", "SVO", "VKO", "DME", "ZIA"},
    "DXB": {"DXB", "DWC"},
    "IST": {"IST", "SAW"},
    "BKK": {"BKK", "DMK"},
    "TYO": {"TYO", "NRT", "HND"},
    "SHA": {"SHA", "PVG"},
    "SEL": {"SEL", "ICN", "GMP"},
    "PAR": {"PAR", "CDG", "ORY"},
    "LON": {"LON", "LHR", "LGW", "STN", "LTN", "LCY", "SEN"},
}


def city_code(code: str | None) -> str | None:
    return next((city for city, airports in CITY_AIRPORTS.items() if code in airports), code)


def search_route(url: str | None) -> dict | None:
    try:
        path = urlparse(url or "").path
    except ValueError:
        return None
    match = re.fullmatch(r"/search/([A-Z]{3})(\d{4})([A-Z]{3})(\d{4})?([1-9]\d*)/?", path)
    if not match:
        return None
    return {"origin": match[1], "destination": match[3],
            "trip_type": "RT" if match[4] else "OW",
            "departure_token": match[2], "return_token": match[4]}


def airport_legs(airports, stops, trip_type: str) -> list[list[dict]]:
    points = [a for a in (airports or []) if isinstance(a, dict) and
              re.fullmatch(r"[A-Z]{3}", str(a.get("code") or ""))]
    if trip_type == "OW":
        return [points]
    counts = stops if isinstance(stops, list) else []
    if len(counts) >= 2 and all(type(x) is int and x >= 0 for x in counts[:2]):
        lengths = [x + 2 for x in counts[:2]]
        if len(points) == sum(lengths):
            return [points[:lengths[0]], points[lengths[0]:]]
    if len(points) == 4:
        return [points[:2], points[2:]]
    # Missing return airports must stay unknown rather than being invented.
    return [points, []] if len(points) <= 2 else [[], []]


def ticket_legs(url: str | None) -> list[dict]:
    """Read the itinerary carried by the ticket link, including connection airports.

    Aviasales encodes each leg as departure/arrival timestamps, duration and
    an IATA chain. Intermediate segment times are not provided by this format.
    """
    try:
        token = parse_qs(urlparse(url or '').query).get('t', [''])[0]
    except ValueError:
        return []
    legs = []
    for match in re.finditer(r'(\d{10})(\d{10})\d{6}([A-Z]{6,})', token):
        chain = match[3]
        if len(chain) % 3:
            continue
        try:
            # This link format encodes rendered local wall times as epoch-like
            # numbers; it does not carry each airport's time zone/UTC offset.
            departure = datetime.fromtimestamp(int(match[1]), timezone.utc).replace(tzinfo=None).isoformat()
            arrival = datetime.fromtimestamp(int(match[2]), timezone.utc).replace(tzinfo=None).isoformat()
        except (ValueError, OverflowError, OSError):
            continue
        legs.append({'airports': [chain[i:i+3] for i in range(0,len(chain),3)],
                     'departure_at': departure, 'arrival_at': arrival})
    return legs


def flight_identity(offer: dict, trip_type: str, requested_origin: str,
                    requested_destination: str | None = None, context: str | None = None) -> dict:
    url_route = search_route(offer.get("source_url"))
    legs = airport_legs(offer.get("airports"), offer.get("stops"), trip_type)
    out, back = legs[0], legs[1] if len(legs) > 1 else []
    start = out[0]["code"] if out else None
    end = out[-1]["code"] if len(out) >= 2 else None
    return_start = back[0]["code"] if back else None
    return_end = back[-1]["code"] if len(back) >= 2 else None
    linked_legs = ticket_legs(offer.get('source_url'))
    needed_legs = 2 if trip_type == 'RT' else 1
    link_consistent = True
    if len(linked_legs) == needed_legs:
        endpoints = [start, end] + ([return_start, return_end] if trip_type == 'RT' else [])
        linked_endpoints = [a for leg in linked_legs for a in (leg['airports'][0],leg['airports'][-1])]
        link_consistent = all(not a or city_code(a)==city_code(b) for a,b in zip(endpoints,linked_endpoints))
        start, end = linked_endpoints[:2]
        if trip_type == 'RT':
            return_start, return_end = linked_endpoints[2:]
    actual_origin = start or (url_route or {}).get("origin")
    actual_destination = end or (url_route or {}).get("destination")
    requested_destination = requested_destination or (url_route or {}).get("destination")
    checks = [(actual_origin, requested_origin), (actual_destination, requested_destination)]
    if trip_type == "RT":
        checks += [(return_start, requested_destination), (return_end, requested_origin)]
    if url_route:
        checks += [(url_route["origin"], requested_origin),
                   (url_route["destination"], requested_destination)]
    mismatch = not link_consistent or any(a and b and city_code(a) != city_code(b) for a, b in checks)
    if url_route and url_route["trip_type"] != trip_type:
        mismatch = True
    verified = bool(requested_destination and actual_origin and actual_destination and
                    (trip_type == "OW" or (return_start and return_end)))
    def segments(index, fallback):
        if len(linked_legs) == needed_legs:
            leg = linked_legs[index]
            chain = leg['airports']
            return [{"origin":a,"destination":b,
                     "departure_at":leg['departure_at'] if i==0 else None,
                     "arrival_at":leg['arrival_at'] if i==len(chain)-2 else None}
                    for i,(a,b) in enumerate(zip(chain,chain[1:]))]
        return [{"origin": a["code"], "destination": b["code"]}
                for a, b in zip(fallback, fallback[1:])]
    return {
        "requested_origin": requested_origin, "requested_destination": requested_destination,
        "actual_origin": actual_origin, "actual_destination": actual_destination,
        "outbound_first_airport": start, "outbound_last_airport": end,
        "return_first_airport": return_start, "return_last_airport": return_end,
        "outbound_segments": segments(0,out), "return_segments": segments(1,back) if trip_type=='RT' else [],
        "departure_at":linked_legs[0]['departure_at'] if linked_legs else None,
        "arrival_at":linked_legs[0]['arrival_at'] if linked_legs else None,
        "return_departure_at":linked_legs[1]['departure_at'] if len(linked_legs)==2 else None,
        "return_arrival_at":linked_legs[1]['arrival_at'] if len(linked_legs)==2 else None,
        "time_basis": 'ticket_encoded_local_time' if linked_legs else None,
        "departure_date": offer.get("departure_date"), "return_date": offer.get("return_date"),
        "search_context": context, "trip_type": trip_type,
        "route_valid": not mismatch,
        "validation": "route_mismatch" if mismatch else ("verified" if verified else "incomplete_itinerary"),
        "group_key": [context, requested_origin, requested_destination, trip_type,
                      city_code(actual_origin), city_code(actual_destination),
                      offer.get("departure_date"), offer.get("return_date")],
    }


def is_candidate_for_search(offer: dict, origin: str, destination: str, trip_type: str) -> bool:
    identity = flight_identity(offer, trip_type, origin, destination)
    return identity["route_valid"] and identity["validation"] == "verified"
