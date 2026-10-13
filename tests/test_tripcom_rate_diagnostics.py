import asyncio
import gc
import json
import sys
import types

from aviasales_collector.hotels.sources.tripcom_rates import (
    _classify_tripcom_rate_response,
    _read_rate_response_body,
    collect_tripcom_room_rates,
)
import aviasales_collector.hotels.sources.tripcom_rates as tripcom_rates


def test_body_text_timeout_does_not_discard_a_received_rate_payload():
    status, inventory, error_type, error_message = _classify_tripcom_rate_response(
        200,
        {"data": {"saleRoomMap": {}}},
        body_wait_error="TimeoutError: body locator did not become readable",
        source_hotel_id="123",
        check_in="2026-10-20",
        check_out="2026-10-25",
        adults=2,
        children_count=0,
    )

    assert status == "no_rates"
    assert inventory["status"] == "no_rates"
    assert error_type is None
    assert error_message is None


def test_body_timeout_without_rate_response_stays_a_failure():
    status, inventory, error_type, error_message = _classify_tripcom_rate_response(
        None,
        None,
        body_wait_error="TimeoutError: body locator did not become readable",
        source_hotel_id="123",
        check_in="2026-10-20",
        check_out="2026-10-25",
        adults=2,
        children_count=0,
    )

    assert status == "body_timeout"
    assert inventory is None
    assert error_type == "body_wait_timeout"
    assert "TimeoutError" in error_message


def test_rate_payload_without_expected_data_shape_stays_invalid():
    status, inventory, error_type, error_message = _classify_tripcom_rate_response(
        200,
        {"unexpected": True},
        body_wait_error="TimeoutError: body locator did not become readable",
        source_hotel_id="123",
        check_in="2026-10-20",
        check_out="2026-10-25",
        adults=2,
        children_count=0,
    )

    assert status == "invalid_response"
    assert inventory is None
    assert error_type is not None
    assert error_message


def test_spider_action_error_code_is_blocked_not_absent_inventory():
    """HTTP 200 + data.htlSpiderActionErrorCode is Trip.com's anti-bot marker.

    Observed live: {"data": {"htlSpiderActionErrorCode": 4030}, "ResponseStatus": ...}
    Previously normalized as rate_inventory_absent (a success-family terminal
    status), hiding a protection response behind a benign outcome.
    """
    status, inventory, error_type, error_message = _classify_tripcom_rate_response(
        200,
        {"data": {"htlSpiderActionErrorCode": 4030},
         "ResponseStatus": {"Ack": "Success", "Errors": []}},
        body_wait_error=None,
        source_hotel_id="128915998",
        check_in="2026-11-14",
        check_out="2026-11-17",
        adults=2,
        children_count=0,
    )

    assert status == "blocked"
    assert inventory is not None
    assert inventory["status"] == "blocked"
    assert inventory["spider_action_error_code"] == 4030
    assert inventory["rates"] == []


def test_absent_inventory_without_spider_marker_stays_rate_inventory_absent():
    status, inventory, error_type, error_message = _classify_tripcom_rate_response(
        200,
        {"data": {}, "ResponseStatus": {"Ack": "Success", "Errors": []}},
        body_wait_error=None,
        source_hotel_id="123",
        check_in="2026-10-20",
        check_out="2026-10-25",
        adults=2,
        children_count=0,
    )

    assert status == "rate_inventory_absent"
    assert inventory["status"] == "rate_inventory_absent"
    assert error_type is None


def test_http_200_with_stalled_rate_body_is_a_terminal_body_timeout():
    status, inventory, error_type, error_message = _classify_tripcom_rate_response(
        200,
        None,
        body_wait_error=None,
        response_body_timeout="Trip.com rate response body exceeded 0.01s deadline after HTTP 200 headers",
        source_hotel_id="123",
        check_in="2026-10-20",
        check_out="2026-10-25",
        adults=2,
        children_count=0,
    )

    assert status == "body_timeout"
    assert inventory is None
    assert error_type == "rate_response_body_timeout"
    assert "HTTP 200" in error_message


def test_rate_response_body_reader_has_a_bounded_deadline():
    class HangingResponse:
        async def body(self):
            await asyncio.Event().wait()

    async def run():
        try:
            await _read_rate_response_body(HangingResponse(), timeout_seconds=0.01)
        except asyncio.TimeoutError:
            return
        raise AssertionError("rate response body reader exceeded its deadline")

    asyncio.run(run())


def test_trip_rate_body_is_captured_before_page_dom_lifecycle_can_invalidate_it(monkeypatch, tmp_path):
    class FakePageResponse:
        status = 200
        url = "https://ru.trip.com/restapi/soa2/33269/getHotelRoomListOversea"

        def __init__(self):
            self.invalidated = False

        async def body(self):
            if self.invalidated:
                raise RuntimeError("No resource with given identifier found after page navigation")
            return json.dumps({"data": {"saleRoomMap": {}}}).encode()

    class FakeFetchedResponse:
        status = 200
        url = "https://ru.trip.com/restapi/soa2/33269/getHotelRoomListOversea"

        async def body(self):
            return json.dumps({"data": {"saleRoomMap": {}}}).encode()

    fetched_response = FakeFetchedResponse()
    aborted_routes = []

    class TimeoutError(Exception):
        pass

    class TargetClosedError(Exception):
        pass

    class FakeRoute:
        def __init__(self, page, *, duplicate=False):
            self.page = page
            self.duplicate = duplicate
            self.request = types.SimpleNamespace(
                url="https://ru.trip.com/restapi/soa2/33269/getHotelRoomListOversea",
            )

        async def fetch(self, timeout=None):
            assert timeout == tripcom_rates.TRIPCOM_RATE_BODY_TIMEOUT_SECONDS * 1000
            if self.page.current_hotel_id == "456":
                raise TimeoutError(
                    "Timeout 12000ms exceeded.\n  - ← 200 OK",
                )
            return fetched_response

        async def fulfill(self, response=None):
            assert response is fetched_response
            await asyncio.sleep(0.01 if self.page.current_hotel_id == "999" else 0.05)
            if self.page.current_hotel_id == "999":
                # Match Playwright's _race_with_page_close: fulfill starts a
                # Channel.send task and waits for either it or page closure.
                # Cancellation of the callback must not orphan that task.
                async def channel_send():
                    await self.page.route_release.wait()
                    await asyncio.sleep(0.02)
                    raise TargetClosedError("Route.fulfill: Target page has been closed")

                send_task = asyncio.create_task(channel_send())
                self.page.route_send_tasks.append(send_task)
                close_wait = asyncio.create_task(self.page.route_release.wait())
                self.page.route_close_waits.append(close_wait)
                await asyncio.wait((send_task, close_wait), return_when=asyncio.FIRST_COMPLETED)
                if send_task.done() and send_task.exception():
                    raise send_task.exception()
                if close_wait.done():
                    await asyncio.gather(send_task, return_exceptions=True)
                    return
                await send_task
            if self.page.current_hotel_id == "789":
                # A concurrent duplicate can be routed before the response
                # listener observes the first request's response. Its
                # continue assertion is the Playwright route-settlement race
                # that previously escaped as an event-listener traceback.
                await self.page.route_callback(FakeRoute(self.page, duplicate=True))
            self.page.listeners["response"](self.page.response)

        async def continue_(self):
            if self.duplicate:
                raise AssertionError("Route.continue_: Assertion error")
            raise AssertionError("a failed route.fetch must not duplicate the Trip request")

        async def abort(self, error_code=None):
            assert error_code == "timedout"
            aborted_routes.append(self.page.current_hotel_id)
            await asyncio.sleep(0.001 if self.page.current_hotel_id == "456" else 0.05)
            if self.page.current_hotel_id == "456":
                raise RuntimeError("Route is already handled!")

    class FakeLocator:
        def __init__(self, page):
            self.page = page

        async def inner_text(self, timeout=None):
            self.page.response.invalidated = True
            return ""

    class FakePage:
        def __init__(self, context):
            self.context = context
            self.listeners = {}
            self.route_callbacks_active = 0
            self.route_tasks = []
            self.route_send_tasks = []
            self.route_close_waits = []
            self.route_release = asyncio.Event()
            self.closed = False
            self.close_release_task = None
            self.response = FakePageResponse()

        def on(self, event, callback):
            self.listeners[event] = callback

        def remove_listener(self, event, callback):
            assert self.listeners[event] is callback
            del self.listeners[event]

        async def route(self, pattern, callback):
            assert pattern.endswith(tripcom_rates.TRIPCOM_RATE_ENDPOINT_PATH)
            assert self.route_callbacks_active == 0, "page reused while its previous route callback was active"
            self.context.route_count += 1
            if self.context.route_count == 3:
                raise RuntimeError("simulated Playwright route registration race")
            self.route_callback = callback

        async def unroute(self, pattern, callback):
            assert pattern.endswith(tripcom_rates.TRIPCOM_RATE_ENDPOINT_PATH)
            assert self.route_callback is callback
            del self.route_callback

        async def unroute_all(self, behavior=None):
            assert behavior == "wait"
            if self.route_tasks:
                outcomes = await asyncio.gather(*self.route_tasks, return_exceptions=True)
                assert not [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
            assert self.route_callbacks_active == 0
            if hasattr(self, "route_callback"):
                del self.route_callback

        async def goto(self, url, wait_until=None, timeout=None):
            self.current_hotel_id = next(
                hotel_id for hotel_id in ("123", "456", "789", "999")
                if f"hotelId={hotel_id}" in url
            )
            self.route_callbacks_active += 1

            async def dispatch_route():
                try:
                    await self.route_callback(FakeRoute(self))
                finally:
                    self.route_callbacks_active -= 1

            self.route_tasks.append(asyncio.create_task(dispatch_route()))
            await asyncio.sleep(0)
            self.response.invalidated = True
            return types.SimpleNamespace(status=200, url=url)

        async def wait_for_timeout(self, _milliseconds):
            await asyncio.sleep(0.001)

        def locator(self, _selector):
            return FakeLocator(self)

        async def close(self):
            self.closed = True
            if getattr(self, "current_hotel_id", None) == "999":
                async def release_after_dispatch():
                    await asyncio.sleep(0.03)
                    self.route_release.set()

                self.close_release_task = asyncio.create_task(release_after_dispatch())
            else:
                self.route_release.set()

    class FakeContext:
        def __init__(self):
            self.pages = []
            self.route_count = 0

        async def new_page(self):
            page = FakePage(self)
            self.pages.append(page)
            return page

        async def close(self):
            for page in self.pages:
                await page.close()
            tasks = [task for page in self.pages for task in page.route_tasks]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            delayed_releases = [page.close_release_task for page in self.pages if page.close_release_task]
            if delayed_releases:
                await asyncio.gather(*delayed_releases)
            close_waits = [task for page in self.pages for task in page.route_close_waits]
            for task in close_waits:
                if not task.done():
                    task.cancel()
            if close_waits:
                await asyncio.gather(*close_waits, return_exceptions=True)
            return None

    context = FakeContext()

    class FakeChromium:
        async def launch_persistent_context(self, *_args, **_kwargs):
            return context

    class FakePlaywrightManager:
        async def __aenter__(self):
            return types.SimpleNamespace(chromium=FakeChromium())

        async def __aexit__(self, *_args):
            return None

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = FakePlaywrightManager
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", fake_api)
    async def no_property_details(_page):
        return {}

    async def no_room_details(_page):
        return {}

    monkeypatch.setattr(tripcom_rates, "_tripcom_extract_property_details", no_property_details)
    monkeypatch.setattr(tripcom_rates, "_tripcom_extract_room_details", no_room_details)
    monkeypatch.setattr(tripcom_rates, "TRIPCOM_ROUTE_CALLBACK_IDLE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(tripcom_rates, "TRIPCOM_ROUTE_CALLBACK_DRAIN_TIMEOUT_SECONDS", 0.01)

    sample = lambda hotel_id: {
        "search_id": "search-1", "source_hotel_id": hotel_id, "city_id": "2657056",
        "check_in": "2026-10-20", "check_out": "2026-10-25", "adults": 2,
        "children_count": 0,
    }
    unhandled_task_errors = []
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _loop, context: unhandled_task_errors.append(context))
    try:
        results = loop.run_until_complete(collect_tripcom_room_rates(
            [sample("123"), sample("456"), sample("777"), sample("789"), sample("999")],
            profile=tmp_path / "trip-profile", concurrency=1,
        ))
        loop.run_until_complete(asyncio.sleep(0.05))
        for page in context.pages:
            page.route_send_tasks.clear()
            page.route_close_waits.clear()
        gc.collect()
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()
    result, timed_out_result, route_registration_error, result_after_route_race, retired_page_result = results

    assert result["status"] == "no_rates"
    assert result["diagnostics"]["rate_payload_size"] > 0
    assert result["diagnostics"]["rate_body_wait_error"] is None
    assert result["diagnostics"]["rate_body_capture_mode"] == "route_fetch"
    assert timed_out_result["status"] == "body_timeout"
    assert timed_out_result["rate_status"] == 200
    assert timed_out_result["error_type"] == "rate_response_body_timeout"
    assert timed_out_result["diagnostics"]["rate_body_capture_mode"] == "route_fetch_failed"
    assert timed_out_result["diagnostics"]["rate_route_cleanup_wait_ms"] >= 0
    assert timed_out_result["diagnostics"]["rate_route_settlement_error"].startswith(
        "abort: RuntimeError: Route is already handled"
    )
    assert route_registration_error["status"] == "navigation_error"
    assert "simulated Playwright route registration race" in route_registration_error["error_message"]
    assert result_after_route_race["status"] == "no_rates"
    assert result_after_route_race["diagnostics"]["rate_route_settlement_error"].startswith(
        "continue_: AssertionError: Route.continue_: Assertion error"
    )
    assert retired_page_result["status"] == "no_rates"
    assert "did not settle within 0.01s" in retired_page_result["diagnostics"]["rate_route_cleanup_error"]
    assert len(context.pages) == 6  # the timed-out route page was retired, then replaced
    assert all(page.closed for page in context.pages)
    assert all("response" not in page.listeners for page in context.pages)
    assert all(page.route_callbacks_active == 0 for page in context.pages)
    assert all(task.done() for page in context.pages for task in page.route_tasks)
    assert not unhandled_task_errors
    assert all(task.done() for page in context.pages for task in page.route_send_tasks)
    assert aborted_routes == ["456"]
