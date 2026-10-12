import asyncio
import json
import sqlite3
from datetime import date

import httpx
import pytest

from aviasales_collector.database import SQLiteStore
from aviasales_collector.flight_routes import flight_identity, is_candidate_for_search
from aviasales_collector.web import create_app
from aviasales_collector import collector


def candidate(destination="DXB", airports=("LED", "DXB", "DXB", "LED"), price=34747):
    return {"origin":"LED", "destination":"Дубай", "price":price, "currency":"RUB",
            "source_url":f"https://www.aviasales.ru/search/LED3009{destination}04101",
            "airports":[{"code":code,"name":code} for code in airports], "stops":[0,0],
            "departure_date":"2026-09-30", "return_date":"2026-10-04"}


def scan(offers):
    return {"origin":"LED", "trip_type":"RT", "complete":True,
            "duration_min_days":3, "duration_max_days":11, "offers":offers,
            "cards_found":len(offers), "unique_destinations":1}


@pytest.mark.parametrize("ticket,expected", [
    (candidate("MOW",("LED","VKO","SVO","LED"),4188),False),
    (candidate(),True),
    (candidate("KUF",("LED","KUF","KUF","LED")),False),
    (candidate("KZN",("LED","KZN","KZN","LED")),False),
    (candidate("DXB",("LED","DXB","KUF","LED")),False),
    (candidate("DXB",("LED","DXB")),False),
])
def test_candidates_are_checked_against_both_actual_legs(ticket, expected):
    assert is_candidate_for_search(ticket,"LED","DXB","RT") is expected


def test_transfer_airports_do_not_change_actual_destination():
    ticket=candidate("DXB",("LED","VKO","DXB","DXB","SVO","LED"))
    ticket['stops']=[1,1]
    assert is_candidate_for_search(ticket,"LED","DXB","RT")
    identity=flight_identity(ticket,"RT","LED","DXB","search-123")
    assert identity['outbound_segments']==[{'origin':'LED','destination':'VKO'},{'origin':'VKO','destination':'DXB'}]
    assert identity['return_first_airport']=='DXB'
    assert identity['actual_destination']=='DXB'


def test_airport_city_resolution_accepts_moscow_airport_change():
    assert is_candidate_for_search(candidate("MOW",("LED","VKO","SVO","LED")),"LED","MOW","RT")


def test_ticket_link_itinerary_conflict_cannot_be_hidden_by_card_airports():
    ticket = candidate()
    ticket['source_url'] += '?t=DP17908098001790814900000085LEDVKO17911011001791105900000080SVOLED_hash_4188'
    identity = flight_identity(ticket,'RT','LED','DXB')
    assert not identity['route_valid']
    assert identity['actual_destination']=='VKO'
    assert identity['return_first_airport']=='SVO'
    assert identity['departure_at']=='2026-09-30T23:10:00'
    assert identity['outbound_segments'][0]['destination']=='VKO'


def test_multi_airport_city_does_not_reject_baku_peking_or_dubai():
    for city,airport in [('BAK','GYD'),('BJS','PKX'),('DXB','DWC')]:
        assert is_candidate_for_search(candidate(city,('LED',airport,airport,'LED')),'LED',city,'RT')


def test_database_rejects_foreign_routes_before_history_and_finding(tmp_path):
    path=tmp_path/'route.sqlite3'
    with SQLiteStore(path) as store:
        store.record_scan(10,-10,scan([candidate()]))
        result=store.record_scan(10,-10,scan([
            candidate("MOW",("LED","VKO","SVO","LED"),4188), candidate(price=33000),
            candidate("KUF",("LED","KUF","KUF","LED"),5684),
        ]),scan_kind='deep',track_price_changes=True)
        offers=store.get_scan_offers(10,-10,result.scan_id)
        assert len(offers)==1 and offers[0]['price']==33000
        assert offers[0]['route_identity']['actual_destination']=='DXB'
        assert store.connection.execute('SELECT price FROM price_observations WHERE scan_id=?',(result.scan_id,)).fetchone()[0]==33000
        assert result.price_findings==()
        offer_id=offers[0]['offer_id']
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(path,frozenset({10}))),base_url='http://local') as client:
            data=(await client.get(f'/api/price-history?origin=LED&offer_id={offer_id}')).json()
            assert [x['price'] for x in data['points']]==[34747,33000]
    asyncio.run(check())


def test_first_deep_scan_keeps_requested_search_identity_without_saved_baseline(tmp_path):
    bad=candidate('MOW',('LED','VKO','SVO','LED'),4188)
    bad['route_identity']=flight_identity(bad,'RT','LED','DXB','requested-dubai')
    with SQLiteStore(tmp_path/'first.sqlite3') as store:
        saved=store.record_scan(10,-10,scan([bad]),scan_kind='deep',track_price_changes=True)
        assert not saved.complete
        assert store.get_scan_offers(10,-10,saved.scan_id)==[]
        assert store.connection.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0]==0


def test_legacy_corruption_is_quarantined_without_deleting_original_rows(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    with SQLiteStore(path) as store:
        store.record_scan(10,-10,scan([candidate()]))
        corrupt=store.record_scan(10,-10,scan([candidate(price=4188)]),scan_kind='deep')
        ticket=candidate('MOW',('LED','VKO','SVO','LED'),4188)
        store.connection.execute('UPDATE offers SET source_url=?,airports_json=? WHERE scan_id=?',
                                 (ticket['source_url'],json.dumps(ticket['airports']),corrupt.scan_id))
        store.connection.execute('UPDATE price_observations SET payload_json=? WHERE scan_id=?',(json.dumps(ticket),corrupt.scan_id))
        store.connection.execute('PRAGMA user_version=15'); store.connection.commit()
    with SQLiteStore(path) as store:
        assert store.connection.execute('SELECT COUNT(*) FROM offers').fetchone()[0]==2
        assert store.connection.execute('SELECT COUNT(*) FROM price_observations').fetchone()[0]==2
        assert store.get_scan_offers(10,-10,corrupt.scan_id)==[]
        assert store.connection.execute('SELECT route_valid FROM price_observations WHERE scan_id=?',(corrupt.scan_id,)).fetchone()[0]==0
        assert store.connection.execute('SELECT last_known_price FROM destinations').fetchone()[0]==34747
        next_scan=store.record_scan(10,-10,scan([candidate(price=20000)]),track_price_changes=True)
        assert len(next_scan.price_findings)==1
        metrics=next_scan.price_findings[0]['deal_metrics']
        assert metrics['previous_price']==34747
        assert metrics['historical_low']==34747
        assert store.connection.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert store.connection.execute('PRAGMA foreign_key_check').fetchall()==[]
    with SQLiteStore(path) as store:
        assert store.connection.execute('SELECT COUNT(*) FROM offers').fetchone()[0]==3
