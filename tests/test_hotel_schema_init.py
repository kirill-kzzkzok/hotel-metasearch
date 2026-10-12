from concurrent.futures import ThreadPoolExecutor

from aviasales_collector.hotels import schema


def test_hotel_schema_initialization_runs_once_per_database_identity(tmp_path, monkeypatch):
    database = tmp_path / "hotel-store.sqlite3"
    original = schema._initialize_hotel_store_uncached
    calls = 0

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(schema, "_initialize_hotel_store_uncached", counted)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: schema.init_hotel_store(database), range(8)))
    schema.init_hotel_store(database)

    assert calls == 1
