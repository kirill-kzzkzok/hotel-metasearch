(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.HotelSearchLifecycle = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function coordinateProcessingKeys(searchId, identities, fallbackIdentity) {
    const stableIdentities = [...new Set((Array.isArray(identities) ? identities : [])
      .filter((identity) => typeof identity === "string" && identity))];
    const keys = stableIdentities.length ? stableIdentities : [`hotel:${fallbackIdentity}`];
    return keys.map((identity) => `${searchId}:${identity}`);
  }

  function isCurrentSearch(expectedSearchId, expectedGeneration, currentSearch, currentGeneration) {
    return expectedGeneration === currentGeneration
      && currentSearch?.search_id === expectedSearchId;
  }

  function resultProgressCounts(search, isReady, isPending) {
    const candidates = Array.isArray(search?.hotels) ? search.hotels : [];
    const ready = candidates.filter((hotel) => isReady(search, hotel)).length;
    const pending = candidates.filter((hotel) => isPending(search, hotel)).length;
    // discovered > ready + pending happens when a hotel never passes the
    // required-data gate (no valid offer/price, or a name-less record).
    // Surface that remainder as unusable instead of hiding it in the sum.
    const unusable = Math.max(0, candidates.length - ready - pending);
    return {
      discovered: candidates.length,
      ready,
      pending,
      unusable,
    };
  }

  // Room mode changes only the offer projection. Keep the already loaded
  // source snapshot and project private-room offers synchronously so a filter
  // click does not rebuild the complete read model on the server.
  function projectRoomModeSearch(search, mode) {
    if (!search || !Array.isArray(search.hotels) || !search.query) return search;
    const requestedMode = mode === "private" ? "private" : "all";
    if (requestedMode === "all") {
      return {...search, query: {...search.query, room_mode: "all"},
        room_audit: {...(search.room_audit || {}), mode: "all",
          eligible_hotels: search.hotels.length, all_hotels: search.hotels.length}};
    }
    const hotels = search.hotels.map(hotel => {
      const offers = (hotel.offers || []).filter(offer => offer.room_privacy === "private");
      return offers.length ? {...hotel, offers} : null;
    }).filter(Boolean);
    return {...search, query: {...search.query, room_mode: "private"}, hotels,
      room_audit: {...(search.room_audit || {}), mode: "private",
        eligible_hotels: hotels.length, all_hotels: search.hotels.length}};
  }

  function createRequestCoalescer() {
    const pending = new Map();
    return {
      run(key, operation) {
        if (pending.has(key)) return pending.get(key);
        const request = Promise.resolve().then(operation);
        pending.set(key, request);
        const clear = () => {
          if (pending.get(key) === request) pending.delete(key);
        };
        request.then(clear, clear);
        return request;
      },
    };
  }

  function cancelPendingRoomModeOnCurrentSelection(searchId, selectedMode, currentMode, pending, revision) {
    if (!pending || pending.searchId !== searchId || selectedMode !== currentMode) {
      return {pending, revision, cancelled: false};
    }
    return {pending: null, revision: revision + 1, cancelled: true};
  }

  return {coordinateProcessingKeys, isCurrentSearch, resultProgressCounts, projectRoomModeSearch,
    createRequestCoalescer,
    cancelPendingRoomModeOnCurrentSelection};
});
