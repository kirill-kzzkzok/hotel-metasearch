const assert = require("node:assert/strict");
const test = require("node:test");

const lifecycle = require("../aviasales_collector/web_assets/hotel_search_lifecycle.js");

test("coordinate processing identity is independent of room mode", () => {
  const all = lifecycle.coordinateProcessingKeys("search-1", ["trip_com:42"], "fallback");
  const privateMode = lifecycle.coordinateProcessingKeys("search-1", ["trip_com:42"], "fallback");
  assert.deepEqual(privateMode, all);
  assert.deepEqual(all, ["search-1:trip_com:42"]);
});

test("stale search responses are rejected by generation and search id", () => {
  assert.equal(lifecycle.isCurrentSearch("search-1", 4, {search_id: "search-1"}, 4), true);
  assert.equal(lifecycle.isCurrentSearch("search-1", 4, {search_id: "search-1"}, 5), false);
  assert.equal(lifecycle.isCurrentSearch("search-1", 4, {search_id: "search-2"}, 4), false);
});

test("result counts distinguish discovered, ready, and pending hotels", () => {
  const search = {search_id: "search-1", query: {check_in: "2026-10-20", check_out: "2026-10-25"},
    hotels: Array.from({length: 121}, (_, index) => ({
    id: index,
    ready: index < 1,
    pending: true,
  }))};
  const counts = lifecycle.resultProgressCounts(search,
    (currentSearch, hotel) => currentSearch.query.check_in && hotel.ready,
    (currentSearch, hotel) => currentSearch.search_id === "search-1" && hotel.pending);
  assert.deepEqual(counts, {discovered: 121, ready: 1, pending: 121});
});

test("identical room-mode requests are coalesced and released after completion", async () => {
  const coalescer = lifecycle.createRequestCoalescer();
  let calls = 0;
  const operation = async () => {
    calls += 1;
    return "ok";
  };
  const first = coalescer.run("search-1:private", operation);
  const second = coalescer.run("search-1:private", operation);
  assert.strictEqual(first, second);
  assert.equal(await first, "ok");
  assert.equal(calls, 1);
  assert.equal(await coalescer.run("search-1:private", operation), "ok");
  assert.equal(calls, 2);
});

test("room-mode projection preserves the loaded snapshot and filters only private offers", () => {
  const search = {search_id: "search-1", query: {room_mode: "all"}, hotels: [
    {source: "sravniotel", source_hotel_id: "1", offers: [
      {amount: 100, room_privacy: "private"}, {amount: 90, room_privacy: "shared"},
    ]},
    {source: "sravniotel", source_hotel_id: "2", offers: [
      {amount: 80, room_privacy: "shared"},
    ]},
  ], room_audit: {mode: "all", eligible_hotels: 2, all_hotels: 2}};
  const projected = lifecycle.projectRoomModeSearch(search, "private");
  assert.equal(projected.query.room_mode, "private");
  assert.equal(projected.hotels.length, 1);
  assert.deepEqual(projected.hotels[0].offers, [{amount: 100, room_privacy: "private"}]);
  assert.equal(projected.room_audit.eligible_hotels, 1);
  assert.equal(search.hotels.length, 2);
  assert.equal(search.hotels[0].offers.length, 2);
  const all = lifecycle.projectRoomModeSearch(search, "all");
  assert.equal(all.hotels.length, 2);
  assert.equal(all.hotels[0].offers.length, 2);
});

test("returning to the active room mode cancels stale pending UI state", () => {
  const pending = {searchId: "search-1", mode: "private", revision: 1};
  assert.deepEqual(lifecycle.cancelPendingRoomModeOnCurrentSelection(
    "search-1", "all", "all", pending, 1,
  ), {pending: null, revision: 2, cancelled: true});
  assert.deepEqual(lifecycle.cancelPendingRoomModeOnCurrentSelection(
    "search-2", "all", "all", pending, 1,
  ), {pending, revision: 1, cancelled: false});
  assert.deepEqual(lifecycle.cancelPendingRoomModeOnCurrentSelection(
    "search-1", "private", "all", pending, 1,
  ), {pending, revision: 1, cancelled: false});
});
