const byId = (id) => document.getElementById(id);
const hotelSearchLifecycle = window.HotelSearchLifecycle;
const form = byId("search-form");
const cityInput = byId("city");
const cityOptions = byId("city-options");
let selectedCity = null;
let currentSearch = null;
let roomModeBaseSearch = null;
let cardVariantCacheSearchId = null;
const cardVariantCache = new Map();
let roomModeRequestRevision = 0;
let roomModePending = null;
let cardInsertionGeneration = 0;
const roomModeRequestCoalescer = hotelSearchLifecycle.createRequestCoalescer();
const selectedMealPlans = new Set();
const selectedAmenities = new Set();
const tripcomAutoCompleted = new Set();
const tripcomInFlightHotels = new Set();
let tripcomActiveSearchId = null;
let debounceTimer;
let cityLookupGeneration = 0;
let hotelMap = null;
let hotelMarkers = null;
let markerByKey = new Map();
let selectedMarkerKey = null;
let hoveredMarkerKey = null;
let mapModalHotelKey = null;
let mapFocusGeneration = 0;
let mapHotels = [];
let mapMode = "list";
let coordinateLoading = false;
let coordinateRetryCount = 0;
let coordinateRetryTimer = null;
let coordinateRequestController = null;
const coordinateRequests = new Set();
const coordinateProcessedHotels = new Set();
let coordinateRenderTimer = null;
let searchGeneration = 0;
let searchRequestController = null;
let loadMorePromise = null;
let loadMoreObserver = null;
let paginationAutoTimer = null;
let geoRefreshTimer = null;
let geoRefreshAttempts = 0;
let sourcePollTimer = null;
let lastSourceRevision = "";
let calendarMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
let calendarSelecting = null;
let calendarHoverDate = null;

function setDateDefaults() {
  const today = new Date();
  byId("check-in").value = "";
  byId("check-out").value = "";
  calendarMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  updateDateTriggers();
}

function dateToISO(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function parseLocalDate(iso) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso || "")) return null;
  const [year, month, day] = iso.split("-").map(Number);
  const date = new Date(year, month - 1, day, 12);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null;
}
function updateDateTriggers() {
  const checkIn = byId("check-in").value, checkOut = byId("check-out").value;
  const format = (value) => value ? new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "short", year: "numeric"})
    .format(parseLocalDate(value)).replace(/\.$/, "") : "";
  const checkInTrigger = byId("check-in-trigger");
  const checkOutTrigger = byId("check-out-trigger");
  checkInTrigger.textContent = format(checkIn) || "Когда";
  checkOutTrigger.textContent = format(checkOut) || "Обратно";
  checkInTrigger.classList.toggle("date-placeholder", !checkIn);
  checkOutTrigger.classList.toggle("date-placeholder", !checkOut);
  checkInTrigger.setAttribute("aria-label", `Заезд: ${format(checkIn) || "Когда"}`);
  checkOutTrigger.setAttribute("aria-label", `Выезд: ${format(checkOut) || "Обратно"}`);
}
function nightsBetween(start, end) {
  if (!start || !end || end <= start) return null;
  return Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000);
}
function showDateCalendar(selecting) {
  const popup = byId("date-calendar");
  if (popup.hidden) {
    const anchor = parseLocalDate(byId("check-in").value) || new Date();
    calendarMonth = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  }
  calendarSelecting = selecting || (byId("check-in").value ? "checkout" : "checkin");
  if (calendarSelecting === "checkout" && !byId("check-in").value) calendarSelecting = "checkin";
  calendarHoverDate = null;
  popup.hidden = false;
  byId("check-in-trigger").setAttribute("aria-expanded", String(calendarSelecting === "checkin"));
  byId("check-out-trigger").setAttribute("aria-expanded", String(calendarSelecting === "checkout"));
  renderCalendar();
}
function renderCalendar() {
  const monthFormatter = new Intl.DateTimeFormat("ru-RU", {month: "long", year: "numeric"});
  const months = [0, 1].map((offset) => new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + offset, 1, 12));
  const capitalize = (value) => value[0].toLocaleUpperCase("ru-RU") + value.slice(1);
  byId("calendar-month-label").textContent = `${capitalize(monthFormatter.format(months[0]))} — ${capitalize(monthFormatter.format(months[1]))}`;
  byId("calendar-selecting").textContent = calendarSelecting === "checkin" ? "Выберите дату заезда" : "Теперь выберите дату выезда";
  const monthContainer = byId("calendar-months"); monthContainer.replaceChildren();
  const today = dateToISO(new Date());
  const checkIn = byId("check-in").value, checkOut = byId("check-out").value;
  const previewEnd = calendarHoverDate && calendarSelecting === "checkout" && calendarHoverDate > checkIn
    ? calendarHoverDate : checkOut;
  const rangeStart = checkIn, rangeEnd = previewEnd;
  for (const month of months) {
    const panel = document.createElement("section"); panel.className = "calendar-month";
    const heading = document.createElement("h3"); heading.className = "calendar-month-title";
    heading.textContent = capitalize(monthFormatter.format(month)); panel.append(heading);
    const weekdays = document.createElement("div"); weekdays.className = "calendar-weekdays";
    weekdays.setAttribute("aria-hidden", "true");
    for (const label of ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]) {
      const weekday = document.createElement("span"); weekday.textContent = label; weekdays.append(weekday);
    }
    panel.append(weekdays);
    const grid = document.createElement("div"); grid.className = "calendar-grid"; grid.setAttribute("role", "grid");
    const firstOfMonth = new Date(month.getFullYear(), month.getMonth(), 1, 12);
    const mondayOffset = (firstOfMonth.getDay() + 6) % 7;
    const start = new Date(firstOfMonth); start.setDate(firstOfMonth.getDate() - mondayOffset);
    for (let index = 0; index < 42; index += 1) {
      const date = new Date(start); date.setDate(start.getDate() + index);
      const iso = dateToISO(date); const inMonth = date.getMonth() === month.getMonth();
      const day = document.createElement("button"); day.type = "button"; day.className = "calendar-day";
      day.textContent = String(date.getDate()); day.dataset.date = iso;
      day.setAttribute("role", "gridcell"); day.setAttribute("aria-label", new Intl.DateTimeFormat("ru-RU", {dateStyle: "full"}).format(date));
      if (!inMonth) day.classList.add("calendar-outside-month");
      if (iso === today) day.classList.add("calendar-today");
      if (iso === checkIn || iso === rangeEnd) day.classList.add("calendar-range-endpoint");
      if (rangeStart && rangeEnd && iso > rangeStart && iso < rangeEnd) day.classList.add("calendar-range-middle");
      const invalidPast = iso < today;
      const invalidCheckout = calendarSelecting === "checkout" && checkIn && iso <= checkIn;
      day.disabled = invalidPast || Boolean(invalidCheckout) || !inMonth;
      day.addEventListener("pointerenter", () => {
        if (calendarSelecting === "checkout") { calendarHoverDate = iso; updateCalendarPreview(); }
      });
      day.addEventListener("click", (event) => {
        // Check-in selection rebuilds the month grids and detaches this day
        // before the document-level outside-click handler runs.
        event.stopPropagation();
        if (calendarSelecting === "checkin") {
          byId("check-in").value = iso;
          byId("check-out").value = "";
          calendarSelecting = "checkout"; calendarHoverDate = null;
          updateDateTriggers(); renderCalendar();
        } else {
          if (!byId("check-in").value || iso <= byId("check-in").value) return;
          byId("check-out").value = iso;
          updateDateTriggers();
          closeDateCalendar();
        }
      });
      grid.append(day);
    }
    panel.append(grid); monthContainer.append(panel);
  }
  const nights = nightsBetween(checkIn, previewEnd);
  byId("calendar-range-summary").textContent = nights
    ? `${dateLabel(checkIn)} — ${dateLabel(previewEnd)} · ${nights} ${plural(nights, "ночь", "ночи", "ночей")}`
    : "Выберите даты поездки";
}
function updateCalendarPreview() {
  const checkIn = byId("check-in").value;
  const checkOut = calendarHoverDate && calendarHoverDate > checkIn ? calendarHoverDate : byId("check-out").value;
  byId("calendar-months").querySelectorAll("[data-date]").forEach((day) => {
    day.classList.toggle("calendar-range-middle", Boolean(checkIn && checkOut
      && day.dataset.date > checkIn && day.dataset.date < checkOut));
    day.classList.toggle("calendar-range-endpoint", Boolean(checkIn && day.dataset.date === checkIn)
      || Boolean(checkOut && day.dataset.date === checkOut));
  });
  const nights = nightsBetween(checkIn, checkOut);
  byId("calendar-range-summary").textContent = nights
    ? `${dateLabel(checkIn)} — ${dateLabel(checkOut)} · ${nights} ${plural(nights, "ночь", "ночи", "ночей")}`
    : "Выберите даты поездки";
}
function closeDateCalendar() {
  byId("date-calendar").hidden = true; calendarSelecting = null; calendarHoverDate = null;
  byId("check-in-trigger").setAttribute("aria-expanded", "false");
  byId("check-out-trigger").setAttribute("aria-expanded", "false");
}

function escapeText(value) { return String(value ?? ""); }
function escapeHtml(value) {
  return escapeText(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}
function formatMoney(amount, currency) {
  if (amount === null || amount === undefined || !currency) return "Цена неизвестна";
  const number = Number(amount);
  if (!Number.isFinite(number)) return "Цена неизвестна";
  const wholeCurrency = ["RUB", "RUR"].includes(String(currency).toUpperCase());
  try { return new Intl.NumberFormat("ru-RU", {
    style: "currency", currency, maximumFractionDigits: wholeCurrency ? 0 : 2,
    minimumFractionDigits: wholeCurrency ? 0 : undefined,
  }).format(number); }
  catch { return `${number.toLocaleString("ru-RU")} ${currency}`; }
}
function stayNights(query) {
  return Math.max(1, Math.round((new Date(`${query.check_out}T00:00:00`) - new Date(`${query.check_in}T00:00:00`)) / 86400000));
}
function safeLink(url) {
  try { const parsed = new URL(url); return parsed.protocol === "https:" ? parsed.href : null; }
  catch { return null; }
}
function validCoordinates(value) {
  if (!value) return false;
  const lat = Number(value.lat), lng = Number(value.lng);
  return Number.isFinite(lat) && Number.isFinite(lng) && Math.abs(lat) <= 90 && Math.abs(lng) <= 180;
}
function safeHotelImage(hotel) {
  const images = (hotel.images || []).filter((image) => image && safeLink(image.x1024 || image.x500 || image.url || image.x240));
  images.sort((a, b) => Number(b.role === "hotel_photo") - Number(a.role === "hotel_photo"));
  return images.map((image) => safeLink(image.x1024 || image.x500 || image.url || image.x240)).find(Boolean) || null;
}
function hotelIdentityKeys(hotel) {
  const records = hotelSourceRecords(hotel);
  return records.map((record) => record.source && record.source_hotel_id != null
    ? record.source + ":" + record.source_hotel_id : null).filter(Boolean);
}
function hotelSourceRecords(hotel) {
  return Array.isArray(hotel?.source_records) && hotel.source_records.length
    ? hotel.source_records : (hotel ? [hotel] : []);
}
function hotelKey(hotel) { return hotel.canonical_hotel_id || hotelIdentityKeys(hotel)[0] || hotel.name; }
function geoIdentityKeys(hotel) {
  const keys = hotelIdentityKeys(hotel);
  if (keys.length) return [...new Set(keys)].sort();
  const canonicalId = hotel?.canonical_hotel_id || hotel?.canonical_id;
  return canonicalId ? [`canonical:${canonicalId}`] : [];
}
function mergeHotelPage(previous, latest) {
  const merged = [...previous];
  for (const hotel of latest) {
    const keys = new Set(hotelIdentityKeys(hotel));
    const matchingIndexes = [];
    merged.forEach((old, index) => {
      if (hotelIdentityKeys(old).some((key) => keys.has(key))
        || (hotel.canonical_hotel_id && old.canonical_hotel_id === hotel.canonical_hotel_id)) {
        matchingIndexes.push(index);
      }
    });
    if (matchingIndexes.length) {
      merged[matchingIndexes[0]] = hotel;
      // Enrichment can match two previously separate source cards. Keep the
      // canonical response once and remove every superseded source card.
      for (let i = matchingIndexes.length - 1; i > 0; i -= 1) merged.splice(matchingIndexes[i], 1);
    } else merged.push(hotel);
  }
  return merged;
}
function mergeHotelDelta(previous, latest, changedIdentityKeys) {
  if (!Array.isArray(changedIdentityKeys)) return mergeHotelPage(previous, latest);
  const changed = new Set(changedIdentityKeys.map(String));
  const retained = previous.filter((hotel) =>
    !hotelIdentityKeys(hotel).some((key) => changed.has(key)));
  return mergeHotelPage(retained, latest);
}

// The API exposes enrichment status on the canonical card after a reload,
// while older nested source records can legitimately omit the same field.
// Treat that canonical terminal status as authoritative so a coordinate
// refresh or a rerender cannot schedule the same Trip.com hotel again.
function tripcomRecordStatus(hotel, record) {
  return record?.room_rate_enrichment_status || (
    record?.source === "trip_com" ? hotel?.room_rate_enrichment_status : null
  ) || null;
}
const TERMINAL_TRIPCOM_ENRICHMENT = new Set([
  "ok", "blocked", "no_rates", "rate_inventory_absent", "rate_inventory_timeout", "body_timeout",
  "navigation_error", "invalid_response", "http_error", "error", "not_started",
  "partial", "invalid_input",
]);
function cardReadinessKey(data, hotel) {
  return `${data.search_id}:${data.query?.room_mode || "all"}:${hotelKey(hotel)}`;
}
function coordinateProcessingKeys(data, hotel) {
  const identities = geoIdentityKeys(hotel);
  return hotelSearchLifecycle.coordinateProcessingKeys(data.search_id, identities, hotelKey(hotel));
}
function coordinatesProcessed(data, hotel) {
  return coordinateProcessingKeys(data, hotel).every((key) => coordinateProcessedHotels.has(key));
}
function markCoordinatesProcessed(data, hotel) {
  coordinateProcessingKeys(data, hotel).forEach((key) => coordinateProcessedHotels.add(key));
}
function clearCoordinatesProcessed(data, hotel) {
  coordinateProcessingKeys(data, hotel).forEach((key) => coordinateProcessedHotels.delete(key));
}
function hotelRequiredDataReady(data, hotel) {
  return Boolean(hotel.name?.trim() && data.query?.check_in && data.query?.check_out
    && (hotel.offers || []).some((offer) =>
    Number(offer.amount) > 0 && Boolean(offer.currency)
      && Boolean(offer.source || offer.provider || hotel.source || hotel.sources?.length)));
}
function hotelReadyForRender(data, hotel) {
  if (!hotelRequiredDataReady(data, hotel)
      || !coordinatesProcessed(data, hotel)) return false;
  // A card is ready only after geo enrichment and each Trip.com rate task has
  // reached a terminal result, including honest unknown/unavailable outcomes.
  // 2026-10-15 user directive: cards must render COMPLETE (geo, metro, center
  // distances present), never half-empty - the speed fix belongs to the
  // enrichment pipeline, not to weakening this gate.
  return hotelSourceRecords(hotel).every((record) => record.source !== "trip_com"
    || TERMINAL_TRIPCOM_ENRICHMENT.has(String(tripcomRecordStatus(hotel, record) || "").toLowerCase()));
}
function scheduleCoordinateRender(searchId, generation) {
  if (coordinateRenderTimer) return;
  coordinateRenderTimer = setTimeout(() => {
    coordinateRenderTimer = null;
    if (generation === searchGeneration && currentSearch?.search_id === searchId) render(currentSearch);
  }, 250);
}
function initializeMap() {
  if (hotelMap || !window.L) return Boolean(hotelMap);
  hotelMap = L.map(byId("hotel-map"), {scrollWheelZoom: true, preferCanvas: true}).setView([0, 0], 2);
  // Use the public OSM France HOT endpoint rather than the main OSM tile
  // server, which can reject browser traffic under its volunteer-server
  // policy. No API key is required; keep both OSM and HOT attribution.
  // 2026-10-16: tile providers rate-limit bursts (a 400-marker map asks for
  // dozens of tiles at once) and answer with EMPTY 1x1 PNGs - the map then
  // looks greyed-out/blurry. An empty tile is a valid image so Leaflet fires
  // tileload, not tileerror; we detect it by naturalWidth==1 and re-fetch
  // the same tile from the fallback provider (Carto Voyager CDN).
  const baseTiles = L.tileLayer("https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png", {
    subdomains: "abc",
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, tiles style <a href="https://hotosm.org/">HOT</a>',
  }).addTo(hotelMap);
  const retriedTiles = new Set();
  baseTiles.on("tileload", (event) => {
    const tile = event.tile;
    if (!tile || tile.naturalWidth !== 1 || retriedTiles.has(tile.src)) return;
    retriedTiles.add(tile.src);
    const retry = new Image();
    retry.onload = () => {
      if (retry.naturalWidth > 1) {
        tile.src = retry.src;
        tile.style.visibility = "";
      }
    };
    retry.src = tile.src.replace("tile.openstreetmap.fr/hot", "tile.openstreetmap.de");
  });
  baseTiles.on("tileerror", (event) => {
    const tile = event.tile;
    if (!tile || retriedTiles.has(tile.src)) return;
    retriedTiles.add(tile.src);
    tile.src = tile.src.replace("tile.openstreetmap.fr/hot", "tile.openstreetmap.de");
  });
  // Every hotel remains an independent price marker. Clustering hides the
  // provider price context and makes hover/click selection ambiguous.
  hotelMarkers = L.featureGroup();
  hotelMap.addLayer(hotelMarkers);
  const restoreSelectedPopup = () => {
    if (!selectedMarkerKey) return;
    const selected = markerByKey.get(selectedMarkerKey);
    if (selected && !selected.isPopupOpen()) selected.openPopup();
  };
  hotelMap.on("zoomend moveend", restoreSelectedPopup);
  return true;
}
function updateMap(hotels = mapHotels) {
  mapHotels = hotels;
  if (!hotelMap || !hotelMarkers) return;
  // A focused “На карте” overlay must stay scoped to its hotel even when a
  // background coordinate/enrichment render refreshes the full result list.
  const visibleHotels = mapModalHotelKey
    ? hotels.filter((hotel) => hotelKey(hotel) === mapModalHotelKey)
    : hotels;
  const withCoordinates = visibleHotels.filter((hotel) => validCoordinates(hotel.location?.coordinates));
  byId("map-empty").hidden = withCoordinates.length > 0;
  if (!withCoordinates.length && !coordinateLoading) byId("map-empty").textContent = "Для этих отелей нет подтверждённых точек на карте.";
  byId("hotel-map").hidden = withCoordinates.length === 0;
  let markersChanged = false;
  const desired = new Map(withCoordinates.map((hotel) => [hotelKey(hotel), hotel]));
  if (selectedMarkerKey && !desired.has(selectedMarkerKey)) {
    selectedMarkerKey = null;
    mapFocusGeneration += 1;
  }
  for (const [key, marker] of markerByKey) {
    const hotel = desired.get(key);
    const coordinates = hotel?.location?.coordinates;
    const nextCoordinateKey = hotel ? `${coordinates.lat},${coordinates.lng}` : null;
    if (!hotel || marker._hotelCoordinateKey !== nextCoordinateKey) {
      hotelMarkers.removeLayer(marker);
      markerByKey.delete(key);
      if (hoveredMarkerKey === key) hoveredMarkerKey = null;
      markersChanged = true;
    }
  }
  for (const hotel of withCoordinates) {
    const key = hotelKey(hotel);
    let marker = markerByKey.get(key);
    if (!marker) {
      const coordinates = hotel.location.coordinates;
      marker = L.marker([coordinates.lat, coordinates.lng], {title: hotel.name});
      marker.on("mouseover", () => {
        hoveredMarkerKey = key;
        marker.openPopup();
        highlightHotelByKey(selectedMarkerKey || key);
      });
      marker.on("mouseout", () => {
        if (hoveredMarkerKey === key) hoveredMarkerKey = null;
        if (selectedMarkerKey !== key) {
          marker.closePopup();
          const selected = markerByKey.get(selectedMarkerKey);
          if (selected && !selected.isPopupOpen()) selected.openPopup();
        }
        highlightHotelByKey(selectedMarkerKey);
      });
      marker._hotelCoordinateKey = `${coordinates.lat},${coordinates.lng}`;
      hotelMarkers.addLayer(marker); markerByKey.set(key, marker);
      markersChanged = true;
    }
    const offer = mapBestOffer(hotel);
    const label = offer ? formatMoney(offer.amount, offer.currency) : "Отель";
    if (marker._hotelPriceLabel !== label) {
      const markerWidth = Math.min(150, Math.max(64, Math.ceil(label.length * 7 + 18)));
      marker.setIcon(L.divIcon({
        className: "hotel-price-marker",
        html: `<span>${escapeHtml(label)}</span>`,
        iconSize: [markerWidth, 30], iconAnchor: [markerWidth / 2, 30], popupAnchor: [0, -30],
      }));
      marker._hotelPriceLabel = label;
    }
    const popupSignature = mapPopupSignature(hotel);
    if (marker._hotelPopupSignature !== popupSignature) {
      const content = mapPopup(hotel, key);
      if (marker.getPopup()) marker.setPopupContent(content);
      else marker.bindPopup(content, {maxWidth: 300, minWidth: 220, closeOnClick: false,
        closeButton: false, autoPan: false, autoClose: false});
      marker._hotelPopupSignature = popupSignature;
    }
    bindHotelMarkerSelection(marker, key);
  }
  if (markersChanged && markerByKey.size && !selectedMarkerKey) {
    const bounds = hotelMarkers.getBounds();
    if (bounds.isValid()) hotelMap.fitBounds(bounds.pad(0.12), {maxZoom: 14});
  }
  highlightHotelByKey(selectedMarkerKey || hoveredMarkerKey);
}
function bindHotelMarkerSelection(marker, key) {
  if (marker._hotelSelectionBound) return;
  // bindPopup installs Leaflet's click toggle. Register selection afterwards
  // so a preview already opened by hover remains open after an ordinary click.
  marker.on("click", () => {
    selectedMarkerKey = key;
    hoveredMarkerKey = null;
    for (const other of markerByKey.values()) if (other !== marker) other.closePopup();
    marker.openPopup();
    highlightHotelByKey(key);
  });
  marker._hotelSelectionBound = true;
}
function mapBestOffer(hotel) {
  return (hotel.offers || []).filter((item) => Number(item.amount) > 0 && item.currency)
    .sort((a, b) => Number(a.amount) - Number(b.amount))[0] || null;
}
function mapPopupSignature(hotel) {
  const offer = mapBestOffer(hotel);
  const review = preferredReview(hotel);
  const stars = Math.max(0, Math.min(5, Math.round(Number(hotel.stars) || 0)));
  return JSON.stringify([
    hotelKey(hotel), hotel.name, stars, safeHotelImage(hotel) || "",
    review?.rating ?? null, review?.reviews_count ?? null,
    offer?.amount ?? null, offer?.currency || "",
    hotel.location?.distance_to_center_km ?? null,
    hotel.location?.distance_to_beach_km ?? null,
  ]);
}
function mapPopup(hotel, key) {
  const popup = document.createElement("div"); popup.className = "map-popup";
  const imageUrl = safeHotelImage(hotel);
  if (imageUrl) {
    const photo = document.createElement("img"); photo.className = "map-popup-photo";
    photo.src = imageUrl; photo.alt = ""; photo.loading = "lazy";
    popup.append(photo);
  }
  const stars = Math.max(0, Math.min(5, Math.round(Number(hotel.stars) || 0)));
  if (stars) addText(popup, "map-popup-stars", "★".repeat(stars));
  const title = document.createElement("strong"); title.className = "map-popup-title"; title.textContent = hotel.name;
  popup.append(title);
  const review = preferredReview(hotel);
  if (review && Number.isFinite(Number(review.rating)) && Number(review.rating) > 0) {
    const rating = document.createElement("div"); rating.className = "map-popup-rating";
    const count = Number(review.reviews_count);
    rating.textContent = `${Number(review.rating).toFixed(1)} · ${Number.isFinite(count) && count > 0 ? `${count.toLocaleString("ru-RU")} отзывов` : "Отзывы —"}`;
    popup.append(rating);
  }
  const facts = document.createElement("div"); facts.className = "map-popup-facts";
  const offer = mapBestOffer(hotel);
  if (offer) addText(facts, "map-popup-price", `от ${formatMoney(offer.amount, offer.currency)}`);
  const centerDistanceValue = hotel.location?.distance_to_center_km;
  const centerDistance = Number(centerDistanceValue);
  if (hasDistance(centerDistanceValue)) {
    addText(facts, "map-popup-distance", `${formatDistance(centerDistance)} до центра`);
  }
  const beachDistanceValue = hotel.location?.distance_to_beach_km;
  const beachDistance = Number(beachDistanceValue);
  // 2026-10-15: applicability must be explicit (beach_relevant === true).
  // beach_relevant=null (city not in a geo profile) used to pass the !==false
  // gate and render "до пляжа" for inland cities (Paris live smoke).
  const beachApplicable = hotel.location?.beach_relevant === true
    && hotel.location?.beach_status !== "not_applicable";
  if (beachApplicable && hasDistance(beachDistanceValue)) {
    addText(facts, "map-popup-distance", `${formatDistance(beachDistance)} до пляжа`);
  }
  if (facts.childElementCount) popup.append(facts);
  const open = document.createElement("button"); open.type = "button";
  open.className = "map-popup-open"; open.textContent = "Открыть отель";
  open.addEventListener("click", () => {
    hotelMap.closePopup(); setViewMode("list");
    const card = Array.from(document.querySelectorAll(".hotel-card")).find((item) => item.dataset.hotelKey === key);
    card?.scrollIntoView({behavior: "smooth", block: "center"}); card?.classList.add("hotel-card-selected");
    setTimeout(() => card?.classList.remove("hotel-card-selected"), 1800);
  });
  popup.append(open);
  return popup;
}
function highlightHotelByKey(key) {
  document.querySelectorAll(".hotel-price-marker span").forEach((node) =>
    node.classList.toggle("selected", Boolean(key) && markerByKey.get(key)?.getElement()?.contains(node)));
  document.querySelectorAll(".hotel-card").forEach((card) =>
    card.classList.toggle("hotel-card-map-active", Boolean(key) && card.dataset.hotelKey === key));
}
function highlightHotel(hotel) { highlightHotelByKey(selectedMarkerKey || (hotel ? hotelKey(hotel) : null)); }
function closeMapModal() {
  mapModalHotelKey = null;
  // Closing the focused overlay also closes the marker selection. This lets
  // the regular map view recompute bounds and prevents a stale selected key
  // from pinning the map at the previous world zoom after rerender.
  selectedMarkerKey = null;
  hoveredMarkerKey = null;
  for (const marker of markerByKey.values()) marker.closePopup();
  mapFocusGeneration += 1;
  byId("map-panel")?.classList.remove("map-modal-open");
  if (mapMode === "map") setViewMode("list");
  updateMap(mapHotels);
}
function focusHotel(hotel) {
  if (!initializeMap()) return;
  setViewMode("map");
  const coordinates = hotel.location?.coordinates;
  if (!validCoordinates(coordinates)) return;
  const key = hotelKey(hotel);
  const generation = searchGeneration;
  const focusGeneration = ++mapFocusGeneration;
  selectedMarkerKey = key;
  hoveredMarkerKey = null;
  mapModalHotelKey = key;
  byId("map-panel")?.classList.add("map-modal-open");
  requestAnimationFrame(() => {
    hotelMap.invalidateSize();
    // A card action opens a focused overlay. Keep the selected hotel as the
    // only marker in that context; closing the overlay restores the full set.
    const allMapHotels = mapHotels;
    updateMap([hotel]);
    mapHotels = allMapHotels;
    hotelMap.flyTo([coordinates.lat, coordinates.lng], Math.max(hotelMap.getZoom(), 15));
    let marker = markerByKey.get(key);
    // If a cluster layer was created while the panel was hidden, make the
    // focused marker visible directly on the map as a resilient fallback.
    if (!marker) {
      marker = L.marker([coordinates.lat, coordinates.lng], {title: hotel.name})
        .bindPopup(mapPopup(hotel, key), {maxWidth: 300, minWidth: 220, closeOnClick: false});
      const labelOffer = mapBestOffer(hotel);
      marker._hotelPriceLabel = labelOffer ? formatMoney(labelOffer.amount, labelOffer.currency) : "Отель";
      marker._hotelPopupSignature = mapPopupSignature(hotel);
      const markerWidth = Math.min(150, Math.max(64, Math.ceil(marker._hotelPriceLabel.length * 7 + 18)));
      marker.setIcon(L.divIcon({className: "hotel-price-marker", html: `<span>${escapeHtml(marker._hotelPriceLabel)}</span>`, iconSize: [markerWidth, 30], iconAnchor: [markerWidth / 2, 30], popupAnchor: [0, -30]}));
      marker.addTo(hotelMarkers); markerByKey.set(key, marker);
      bindHotelMarkerSelection(marker, key);
    }
    if (!marker) return;
    const openPopup = () => {
      if (generation !== searchGeneration || focusGeneration !== mapFocusGeneration
          || markerByKey.get(key) !== marker || !hotelMarkers.hasLayer(marker)) return;
      selectedMarkerKey = key;
      marker.openPopup();
      highlightHotelByKey(key);
    };
    if (typeof hotelMarkers.zoomToShowLayer === "function") hotelMarkers.zoomToShowLayer(marker, openPopup);
    else openPopup();
  });
}
function setViewMode(mode) {
  mapMode = mode;
  if (mode !== "map") {
    mapModalHotelKey = null;
    byId("map-panel")?.classList.remove("map-modal-open");
  }
  byId("map-panel").hidden = mode !== "map";
  byId("results-area").classList.toggle("map-view", mode === "map");
  document.querySelectorAll("[data-view]").forEach((button) =>
    button.setAttribute("aria-pressed", String(button.dataset.view === mode)));
  byId("mobile-map-open").hidden = mode === "map";
  byId("mobile-map-close").hidden = mode !== "map";
  if (mode === "map") {
    byId("map-empty").textContent = "Проверяем подтверждённые координаты…";
    byId("map-empty").hidden = false;
    if (!initializeMap()) {
      byId("map-empty").textContent = "Карта не загрузилась. Проверьте подключение и повторите попытку.";
      byId("map-empty").hidden = false;
    }
    else loadMapCoordinates();
    requestAnimationFrame(() => { hotelMap?.invalidateSize(); updateMap(); });
  }
}
async function loadMapCoordinates() {
  if (!currentSearch || coordinateLoading) return;
  const searchAtStart = currentSearch;
  const generationAtStart = searchGeneration;
  const pending = (searchAtStart.hotels || []).filter((hotel) =>
    !coordinatesProcessed(searchAtStart, hotel));
  if (!pending.length) return;
  const pendingHotelKeys = [...new Set(pending.flatMap(geoIdentityKeys))];
  if (!pendingHotelKeys.length) return;
  coordinateLoading = true;
  const requestController = new AbortController();
  coordinateRequestController?.abort();
  coordinateRequestController = requestController;
  let receivedBatch = false;
  const sameSearch = () => hotelSearchLifecycle.isCurrentSearch(
    searchAtStart.search_id, generationAtStart, currentSearch, searchGeneration,
  );
  const applyBatch = (enrichedHotels) => {
    if (!sameSearch() || !Array.isArray(enrichedHotels)) return;
    const mergeSnapshot = (snapshot) => {
      if (!snapshot || snapshot.search_id !== searchAtStart.search_id) return snapshot;
      const hotels = [...(snapshot.hotels || [])];
      const indexByIdentity = new Map();
      hotels.forEach((hotel, index) => {
        indexByIdentity.set(hotelKey(hotel), index);
        hotelIdentityKeys(hotel).forEach((key) => indexByIdentity.set(key, index));
      });
      let changed = false;
      for (const enriched of enrichedHotels) {
        const index = [hotelKey(enriched), ...hotelIdentityKeys(enriched)]
          .map((key) => indexByIdentity.get(key)).find((value) => value !== undefined);
        if (index === undefined) continue;
        const previous = hotels[index];
        const location = {...(previous.location || {}), ...(enriched.location || {})};
        // Never erase a verified field if a later source snapshot is sparse.
        for (const key of ["coordinates", "distance_to_center_m", "distance_to_center_km",
          "distance_to_beach_km", "coordinate_source", "center_source", "beach_source"]) {
          if (previous.location?.[key] != null && enriched.location?.[key] == null) {
            location[key] = previous.location[key];
          }
        }
        const details = new Map((previous.provider_details || []).map((item) => [item.provider, item]));
        for (const item of enriched.provider_details || []) details.set(item.provider, item);
        hotels[index] = {...previous, location, provider_details: [...details.values()]};
        markCoordinatesProcessed(searchAtStart, hotels[index]);
        changed = true;
      }
      return changed ? {...snapshot, hotels} : snapshot;
    };
    const previousCurrentSearch = currentSearch;
    const updatedCurrentSearch = mergeSnapshot(previousCurrentSearch);
    currentSearch = updatedCurrentSearch;
    // A private-room projection owns a separate hotel array. Keep the full
    // all-room snapshot in sync so a later mode switch cannot restore stale
    // coordinates or beach distances after a delayed geo batch.
    if (roomModeBaseSearch?.search_id === searchAtStart.search_id) {
      roomModeBaseSearch = roomModeBaseSearch === previousCurrentSearch
        ? updatedCurrentSearch : mergeSnapshot(roomModeBaseSearch);
    }
    if (updatedCurrentSearch !== previousCurrentSearch) receivedBatch = true;
    scheduleCoordinateRender(searchAtStart.search_id, generationAtStart);
  };
  try {
    const response = await fetch("/api/hotels/search/" + encodeURIComponent(searchAtStart.search_id) + "/coordinates", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({room_mode: searchAtStart.query.room_mode || "all", stream: true,
        hotel_keys: pendingHotelKeys, batch_size: 25}),
      signal: requestController.signal,
    });
    if (!response.ok || !response.body) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail?.message || data.detail || "Не удалось уточнить координаты отелей");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    const consumeLines = (flush = false) => {
      const lines = buffer.split("\n");
      buffer = flush ? "" : lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const batch = JSON.parse(line);
        applyBatch(batch.hotels);
        if (Array.isArray(batch.unresolved_hotel_keys)) {
          for (const key of batch.unresolved_hotel_keys) {
            const markUnresolved = (snapshot) => {
              if (!snapshot || snapshot.search_id !== searchAtStart.search_id) return snapshot;
              const hotels = [...(snapshot.hotels || [])];
              const index = hotels.findIndex((hotel) => geoIdentityKeys(hotel).includes(key));
              if (index < 0) return snapshot;
              hotels[index] = {...hotels[index], location: {
                ...(hotels[index].location || {}), coordinate_status: "ERROR",
                coordinate_reason: "geo_identity_not_returned_by_finalized_search",
                beach_status: "source_unavailable", nearby_geo_status: "error",
              }};
              markCoordinatesProcessed(searchAtStart, hotels[index]);
              return {...snapshot, hotels};
            };
            const previousCurrentSearch = currentSearch;
            const updatedCurrentSearch = markUnresolved(previousCurrentSearch);
            currentSearch = updatedCurrentSearch;
            if (roomModeBaseSearch?.search_id === searchAtStart.search_id) {
              roomModeBaseSearch = roomModeBaseSearch === previousCurrentSearch
                ? updatedCurrentSearch : markUnresolved(roomModeBaseSearch);
            }
            if (updatedCurrentSearch !== previousCurrentSearch) receivedBatch = true;
          }
        }
      }
      if (flush && lines.length === 0 && buffer.trim()) {
        const batch = JSON.parse(buffer);
        applyBatch(batch.hotels);
        buffer = "";
      }
    };
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      consumeLines();
    }
    buffer += decoder.decode();
    consumeLines(true);
    coordinateRetryCount = 0;
  } catch (error) {
    if (error.name !== "AbortError" && sameSearch()) {
      showNotice(`Догружаем детали отелей: ${error.message || "временная ошибка"}`);
      coordinateRetryCount += 1;
      if (coordinateRetryCount >= 3) {
        // A repeated transport/server failure is terminally visible as unknown
        // for this search. Source-side 429 remains a handled geo status in the
        // stream and is never converted into an alternate-endpoint request.
        const currentHotels = [...(currentSearch.hotels || [])];
        const stranded = currentHotels.filter((hotel) => !coordinatesProcessed(currentSearch, hotel));
        for (const hotel of stranded) {
          const index = currentHotels.findIndex((item) => hotelKey(item) === hotelKey(hotel)
            || hotelIdentityKeys(item).some((key) => hotelIdentityKeys(hotel).includes(key)));
          if (index < 0) continue;
          currentHotels[index] = {...currentHotels[index], location: {
            ...(currentHotels[index].location || {}), coordinate_status: "ERROR",
            coordinate_reason: `coordinate_request:${error.name || "error"}`,
            beach_status: "source_unavailable", nearby_geo_status: "error",
          }};
          markCoordinatesProcessed(searchAtStart, currentHotels[index]);
        }
        currentSearch = {...currentSearch, hotels: currentHotels};
        scheduleCoordinateRender(searchAtStart.search_id, generationAtStart);
      }
    }
  } finally {
    if (coordinateRequestController === requestController) {
      coordinateRequestController = null;
      coordinateLoading = false;
    }
    if (sameSearch() && receivedBatch) {
      scheduleCoordinateRender(searchAtStart.search_id, generationAtStart);
    }
    if (mapMode === "map") updateMap(mapHotels);
    scheduleGeoRefresh();
    const hasUnprocessedHotels = currentSearch?.hotels?.some((hotel) =>
      !coordinatesProcessed(currentSearch, hotel));
    if (currentSearch && hasUnprocessedHotels) {
      if (coordinateRetryCount >= 3) return;
      if (coordinateRetryCount) {
        clearTimeout(coordinateRetryTimer);
        coordinateRetryTimer = setTimeout(() => {
          coordinateRetryTimer = null;
          void loadMapCoordinates();
        }, Math.min(20000, 1000 * (2 ** (coordinateRetryCount - 1))));
      } else queueMicrotask(() => loadMapCoordinates());
    }
  }
}

function scheduleGeoRefresh() {
  if (!currentSearch || geoRefreshTimer || geoRefreshAttempts >= 5) return;
  const pending = (currentSearch.hotels || []).some((hotel) => {
    const location = hotel.location || {};
    return ["error", "source_unavailable"].includes(location.beach_status) || location.nearby_geo_status === "error";
  });
  if (!pending) return;
  const searchAtStart = currentSearch;
  const generationAtStart = searchGeneration;
  geoRefreshTimer = setTimeout(async () => {
    geoRefreshTimer = null;
    geoRefreshAttempts += 1;
    try {
      const response = await fetch(`/api/hotels/search/${encodeURIComponent(searchAtStart.search_id)}`);
      const data = await response.json();
      if (!response.ok || generationAtStart !== searchGeneration
        || currentSearch?.search_id !== searchAtStart.search_id) return;
      currentSearch = data;
      render(data);
      if (mapMode === "map") updateMap(mapHotels);
    } catch { /* background refresh is best effort; the card remains usable */ }
  }, 6000);
}

async function lookupCities(term) {
  const requestedTerm = term.trim();
  if (requestedTerm.length < 2) { cityOptions.hidden = true; return; }
  const generation = ++cityLookupGeneration;
  const response = await fetch(`/api/hotels/cities?q=${encodeURIComponent(requestedTerm)}`);
  if (!response.ok) throw new Error("Подсказки городов временно недоступны. Попробуйте ещё раз.");
  const data = await response.json();
  if (generation !== cityLookupGeneration || cityInput.value.trim() !== requestedTerm) return;
  cityOptions.replaceChildren();
  for (const city of data.cities) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    button.setAttribute("role", "option");
    const title = document.createElement("span"); title.textContent = city.name;
    const detail = document.createElement("small"); detail.textContent = [city.country, city.hotels_count ? `${Number(city.hotels_count).toLocaleString("ru-RU")} объектов в каталоге` : ""].filter(Boolean).join(" · ");
    button.append(title, detail);
    button.addEventListener("click", () => {
      selectedCity = city; cityInput.value = city.name; byId("geo-id").value = city.geo_id; cityOptions.hidden = true;
    });
    cityOptions.append(button);
  }
  cityOptions.hidden = data.cities.length === 0;
  if (!data.cities.length) showNotice("Не удалось однозначно определить город. Попробуйте другое написание.");
}

function addText(parent, className, text) {
  const node = document.createElement("div"); node.className = className; node.textContent = escapeText(text); parent.append(node); return node;
}
function addBadge(parent, text, kind = "") {
  const node = document.createElement("span"); node.className = `hotel-badge ${kind}`.trim(); node.textContent = escapeText(text); parent.append(node); return node;
}

function startTripcomBackgroundEnrichment(data) {
  if (!data?.search_id || !Array.isArray(data.hotels)
      || tripcomActiveSearchId === data.search_id || tripcomActiveSearchId) return;
  const tripRecordsById = new Map();
  for (const hotel of data.hotels) {
    for (const record of hotelSourceRecords(hotel)) {
      if (record.source !== "trip_com" || !/^\d+$/.test(String(record.source_hotel_id || ""))) continue;
      const effectiveStatus = tripcomRecordStatus(hotel, record);
      if (effectiveStatus && !record.room_rate_enrichment_status) record.room_rate_enrichment_status = effectiveStatus;
      if (!tripRecordsById.has(String(record.source_hotel_id))) tripRecordsById.set(String(record.source_hotel_id), record);
    }
  }
  const tripRecords = [...tripRecordsById.values()];
  if (!tripRecords.length || !data.search_id) return;
  const generationAtRender = searchGeneration;
  const requestKey = (hotelId) => `${data.search_id}:${hotelId}`;
  const inFlightIds = new Set(tripRecords
    .filter((record) => tripcomInFlightHotels.has(requestKey(String(record.source_hotel_id))))
    .map((record) => String(record.source_hotel_id)));
  // 2026-10-17: a previous pool crash leaves status='queued' rows in the
  // DB (written before the browser pool starts). 'queued' is not a terminal
  // status, so hotelReadyForRender would wait forever; the backend treats
  // queued rows as retryable, so include them in the next enrichment POST.
  const pending = tripRecords.filter((record) => {
    const id = String(record.source_hotel_id);
    const status = String(record.room_rate_enrichment_status || "").toLowerCase();
    const retryable = !status || status === "queued" || status === "running";
    return retryable
      && !tripcomAutoCompleted.has(requestKey(id))
      && !tripcomInFlightHotels.has(requestKey(id));
  });
  const pendingIds = new Set(pending.map((record) => String(record.source_hotel_id)));
  const loadingIds = new Set([...inFlightIds, ...pendingIds]);
  const markLoading = (target) => {
    for (const hotel of target?.hotels || []) {
      for (const record of hotelSourceRecords(hotel)) {
        if (record.source === "trip_com" && loadingIds.has(String(record.source_hotel_id))) {
          record.room_rate_enrichment_status = "loading";
          hotel.room_rate_enrichment_status = "loading";
        }
      }
    }
  };
  markLoading(data);
  if (currentSearch?.search_id === data.search_id && generationAtRender === searchGeneration) markLoading(currentSearch);
  if (!pending.length) return;

  // One persistent browser context enriches the whole bounded-concurrency
  // queue. Each hotel is committed as it finishes, and the client polls those
  // terminal rows so ready Trip.com cards can appear before the queue ends.
  tripcomActiveSearchId = data.search_id;
  for (const record of pending) tripcomInFlightHotels.add(requestKey(String(record.source_hotel_id)));
  void (async () => {
    let requestSettled = false;
    const sameSearch = () => currentSearch?.search_id === data.search_id
      && searchGeneration === generationAtRender;
    const refreshSearch = async (hotelIds = []) => {
      if (!sameSearch()) return;
      const ids = [...new Set(hotelIds.map(String).filter((id) => /^\d+$/.test(id)))];
      if (!ids.length) return;
      const refreshedResponse = await fetch(
        `/api/hotels/search/${encodeURIComponent(data.search_id)}/room-rates/results?hotel_ids=${encodeURIComponent(ids.join(","))}`,
      );
      if (!refreshedResponse.ok) return;
      const refreshed = await refreshedResponse.json();
      if (!sameSearch()) return;
      const updates = new Map((refreshed.hotels || []).map((hotel) => [String(hotel.source_hotel_id), hotel]));
      if (!updates.size) return;
      const changedHotels = new Set();
      const coordinateRefreshKeys = new Set();
      const apply = (target) => {
        for (const hotel of target?.hotels || []) {
          for (const record of hotelSourceRecords(hotel)) {
            if (record.source !== "trip_com") continue;
            const update = updates.get(String(record.source_hotel_id));
            if (!update) continue;
            const oldCoordinates = hotel.location?.coordinates;
            const newCoordinates = update.coordinates;
            if (validCoordinates(newCoordinates) && (
              !validCoordinates(oldCoordinates)
              || Number(oldCoordinates.lat) !== Number(newCoordinates.lat)
              || Number(oldCoordinates.lng) !== Number(newCoordinates.lng)
            )) {
              clearCoordinatesProcessed(currentSearch, hotel);
              coordinateRefreshKeys.add(hotelKey(hotel));
            }
            for (const key of ["address", "stars", "rating", "rating_original", "rating_scale",
              "rating_normalized", "rating_source", "reviews_count", "coordinates", "coordinate_source",
              "coordinate_method", "coordinate_confidence", "coordinate_status", "coordinate_reason",
              "images", "amenities", "provider_details"]) {
              if (update[key] != null) record[key] = update[key];
            }
            record.room_options = update.room_options || [];
            record.room_rates = update.room_rates || [];
            record.property_details = update.property_details || null;
            record.room_rate_enrichment_status = update.room_rate_enrichment_status || "partial";
            hotel.room_options = [...new Map(hotelSourceRecords(hotel)
              .flatMap((item) => item.room_options || [])
              .map((item) => [JSON.stringify(item), item])).values()];
            hotel.room_rates = [...new Map(hotelSourceRecords(hotel)
              .flatMap((item) => item.room_rates || [])
              .map((item) => [JSON.stringify(item), item])).values()];
            if (hotel.stars == null && update.stars != null) hotel.stars = update.stars;
            if (hotel.rating == null && update.rating != null) hotel.rating = update.rating;
            if (hotel.rating_normalized == null && update.rating_normalized != null) {
              hotel.rating_normalized = update.rating_normalized;
            }
            if (hotel.reviews_count == null && update.reviews_count != null) hotel.reviews_count = update.reviews_count;
            if (!hotel.address && update.address) hotel.address = update.address;
            hotel.images = [...new Map([...(hotel.images || []), ...(update.images || [])]
              .map((item) => [item.url || JSON.stringify(item), item])).values()];
            hotel.amenities = [...new Set([...(hotel.amenities || []), ...(update.amenities || [])])];
            const details = new Map((hotel.provider_details || []).map((item) => [
              `${item.provider || ""}:${item.source || ""}:${item.method || ""}`, item,
            ]));
            for (const item of update.provider_details || []) {
              details.set(`${item.provider || ""}:${item.source || ""}:${item.method || ""}`, item);
            }
            hotel.provider_details = [...details.values()];
            hotel.property_details = record.property_details || hotel.property_details || null;
            hotel.room_rate_enrichment_status = record.room_rate_enrichment_status;
            changedHotels.add(hotelKey(hotel));
          }
        }
      };
      apply(currentSearch);
      apply(data);
      if (changedHotels.size) render(currentSearch);
      if (coordinateRefreshKeys.size) void loadMapCoordinates();
    };
    const pollCompletedHotels = async () => {
      while (!requestSettled && sameSearch()) {
        await new Promise((resolve) => setTimeout(resolve, 900));
        if (requestSettled || !sameSearch()) break;
        try {
          const response = await fetch(`/api/hotels/search/${encodeURIComponent(data.search_id)}/room-rates/status`);
          if (!response.ok) continue;
          const statusData = await response.json();
          const completed = (statusData.hotels || []).filter((item) =>
            pendingIds.has(String(item.source_hotel_id))
              && TERMINAL_TRIPCOM_ENRICHMENT.has(String(item.status || "").toLowerCase())
              && !tripcomAutoCompleted.has(requestKey(String(item.source_hotel_id))));
          if (!completed.length) continue;
          for (const item of completed) tripcomAutoCompleted.add(requestKey(String(item.source_hotel_id)));
          await refreshSearch(completed.map((item) => String(item.source_hotel_id)));
        } catch {
          // The full POST response remains authoritative if a progress poll
          // briefly fails; keep polling without restarting source requests.
        }
      }
    };
    let pollTask = null;
    try {
      pollTask = pollCompletedHotels();
      const response = await fetch(`/api/hotels/search/${encodeURIComponent(data.search_id)}/room-rates`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({hotel_ids: pending.map((record) => String(record.source_hotel_id)), concurrency: 5}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Не удалось загрузить данные Trip.com");
      const byHotelId = new Map((result.hotels || []).map((item) => [String(item.source_hotel_id), item]));
      const applyEnrichment = (target) => {
        for (const hotel of target?.hotels || []) {
          for (const record of hotelSourceRecords(hotel)) {
            if (record.source !== "trip_com") continue;
            const hotelId = String(record.source_hotel_id);
            const enriched = byHotelId.get(hotelId);
            if (!enriched) continue;
            record.room_options = enriched.room_options || [];
            record.room_rates = enriched.room_rates || [];
            record.property_details = enriched.property_details || null;
            record.room_rate_enrichment_status = enriched.enrichment?.status || "partial";
            hotel.room_options = record.room_options;
            hotel.room_rates = record.room_rates;
            hotel.property_details = record.property_details;
            hotel.room_rate_enrichment_status = record.room_rate_enrichment_status;
          }
        }
      };
      applyEnrichment(data);
      const isCurrent = sameSearch();
      if (isCurrent && currentSearch !== data) applyEnrichment(currentSearch);
      for (const record of pending) tripcomAutoCompleted.add(requestKey(String(record.source_hotel_id)));
      if (isCurrent) render(currentSearch);
    } catch (error) {
      const markFailed = (target) => {
        for (const hotel of target?.hotels || []) {
          for (const record of hotelSourceRecords(hotel)) {
            const hotelId = String(record.source_hotel_id);
            if (record.source === "trip_com" && pendingIds.has(hotelId)) {
              record.room_rate_enrichment_status = "error";
              tripcomAutoCompleted.add(requestKey(hotelId));
              hotel.room_rate_enrichment_status = "error";
            }
          }
        }
      };
      markFailed(data);
      const isCurrent = sameSearch();
      if (isCurrent && currentSearch !== data) markFailed(currentSearch);
      if (isCurrent) render(currentSearch);
    } finally {
      requestSettled = true;
      if (pollTask) await pollTask;
      for (const record of pending) tripcomInFlightHotels.delete(requestKey(String(record.source_hotel_id)));
      tripcomActiveSearchId = null;
      if (currentSearch) {
        const nextGeneration = searchGeneration;
        queueMicrotask(() => {
          if (currentSearch && nextGeneration === searchGeneration) startTripcomBackgroundEnrichment(currentSearch);
        });
      }
    }
  })();
}

const AMENITY_LABELS = {
  wifi: "Wi‑Fi", pool: "Бассейн", spa: "Спа", parking: "Парковка", gym: "Тренажёрный зал",
  air_conditioning: "Кондиционер", reception_24h: "Ресепшен 24/7", beach: "Пляж",
  restaurant: "Ресторан", breakfast: "Завтрак", elevator: "Лифт", shuttle: "Трансфер",
  sauna: "Сауна", laundry: "Прачечная", balcony: "Балкон", refrigerator: "Холодильник", television: "Телевизор",
  pets: "Можно с животными", kitchen: "Кухня", heating: "Отопление", luggage: "Хранение багажа",
  terrace: "Терраса", seaside: "Вид на море",
};
const CORE_AMENITY_FILTERS = ["wifi", "pool", "spa", "parking", "gym", "air_conditioning", "reception_24h"];
const SUPPORTED_AMENITY_FILTERS = Object.keys(AMENITY_LABELS);
const AMENITY_GROUPS = [
  {title: "Основное", keys: ["wifi", "pool", "parking", "air_conditioning", "restaurant", "breakfast"]},
  {title: "Для отдыха", keys: ["beach", "spa", "sauna", "gym"]},
  {title: "Сервис", keys: ["reception_24h", "shuttle", "laundry"]},
  {title: "Для номера", keys: ["balcony", "refrigerator", "television", "elevator"]},
];
function amenityKey(value) {
  const text = String(value || "").trim().toLowerCase().replace(/[‑–—]/g, "-");
  if (!text) return null;
  if (/wi-?fi|wifi|wireless|интернет/.test(text)) return "wifi";
  if (/pool|бассейн/.test(text)) return "pool";
  if (/spa|спа|сауна/.test(text)) return "spa";
  if (/park|парков/.test(text)) return "parking";
  if (/gym|fitness|тренаж|фитнес/.test(text)) return "gym";
  if (/air.?condition|кондиционер/.test(text)) return "air_conditioning";
  if (/24.?[/:]?7|круглосуточ|24.?hour|reception/.test(text)) return "reception_24h";
  if (/beach|пляж/.test(text)) return "beach";
  if (/restaurant|ресторан/.test(text)) return "restaurant";
  if (/breakfast|завтрак/.test(text)) return "breakfast";
  if (/elevator|лифт/.test(text)) return "elevator";
  if (/shuttle|трансфер/.test(text)) return "shuttle";
  if (/sauna|саун/.test(text)) return "sauna";
  if (/laundry|прачеч/.test(text)) return "laundry";
  if (/balcony|балкон/.test(text)) return "balcony";
  if (/refrigerator|fridge|холодильник/.test(text)) return "refrigerator";
  if (/television|\btv\b|телевизор/.test(text)) return "television";
  if (/pets|животн/.test(text)) return "pets";
  if (/kitchen|кухн|готов.{0,12}еды/.test(text)) return "kitchen";
  if (/heating|отоплен/.test(text)) return "heating";
  if (/luggage|багаж/.test(text)) return "luggage";
  if (/terrace|террас|смотровая площадк/.test(text)) return "terrace";
  if (/seaside|first line|первая линия|вид на море|sea view/.test(text)) return "seaside";
  return null;
}
function hotelAmenities(hotel) {
  const values = [...(Array.isArray(hotel.amenities) ? hotel.amenities : [])];
  for (const detail of hotel.provider_details || []) {
    if (Array.isArray(detail?.hotel_amenities)) values.push(...detail.hotel_amenities);
  }
  if (Array.isArray(hotel.property_details?.amenities)) values.push(...hotel.property_details.amenities);
  const seen = new Set();
  return values.map(amenityKey).filter((key) => key && !seen.has(key) && seen.add(key));
}
function hotelPolicy(hotel) {
  const details = [...(hotel.provider_details || [])].reverse();
  const checkIn = details.find((item) => item?.check_in)?.check_in || hotel.property_details?.check_in_text
    || hotel.check_in || hotel.checkin || null;
  const checkOut = details.find((item) => item?.check_out)?.check_out || hotel.property_details?.check_out_text
    || hotel.check_out || hotel.checkout || null;
  return {checkIn, checkOut};
}
function cardRenderSignature(hotel, index, nights, filters, currency, providers, offers) {
  // Cheap, stable signature over only the fields the card actually renders.
  // The previous JSON.stringify of the whole hotel object cost several ms per
  // card on hundreds of cards per render; the priced fields that drive the
  // visible content (name, rating, distances, offer prices, room rate count
  // and status) are all present, so stale cached variants stay impossible.
  const location = hotel.location || {};
  const offerSignature = (offers || []).map((offer) => [
    offer.source || offer.provider || null,
    offer.price?.value ?? offer.price ?? null,
    offer.price?.currency ?? offer.currency ?? null,
    offer.provider_rate_id || null,
    (offer.labels || []).join(","),
  ]).join("|");
  const reviewSignature = (hotel.review_stats || []).map((item) => [
    item.source || null, item.rating ?? null, item.reviews_count ?? null]).join("|");
  const roomRateCount = (hotel.room_rates || []).length;
  const roomOptionCount = (hotel.room_options || []).length;
  return [
    hotelKey(hotel), index, nights, hotel.name || "", hotel.address || "",
    hotel.stars ?? "", hotel.rating ?? "", hotel.reviews_count ?? "",
    reviewSignature, reviewLink(hotel) || "",
    (hotel.images || []).length, location.coordinates || "",
    location.distance_to_center_km ?? "", location.distance_to_beach_km ?? "",
    location.distance_to_metro_km ?? "", location.distance_to_airport_km ?? "",
    location.distance_to_landmark_km ?? "", location.landmark_name || "",
    location.beach_line || "unknown",
    (hotelAmenities(hotel) || []).join(","), JSON.stringify(hotelPolicy(hotel) || null),
    offerSignature, roomOptionCount, roomRateCount,
    hotel.room_rate_enrichment_status || "",
    (hotel.provider_details || []).map((item) => [item.provider, item.stars ?? "",
      item.rating ?? "", item.reviews_count ?? ""].join(",")).join("|"),
    JSON.stringify(hotel.property_details?.stars ?? null),
    hotel.property_details?.description ? "d" : "",
    hotel.property_details?.review_level || hotel.review_level || "",
    JSON.stringify(hotel.property_details?.rating_subscores || hotel.rating_subscores || []),
    (hotel.source_records || []).map((record) => [record.source,
      record.source_hotel_id, record.room_rate_enrichment_status || ""].join(",")).join("|"),
    // 2026-10-16: every NEW rendered field must be in the signature or a
    // re-render with fresh enrichment data is skipped as "unchanged":
    // policy times (sravni API), description, subscores, landmarks, offer
    // tags (scarcity/late checkout), room photos/area/bed.
    JSON.stringify(hotelPolicy(hotel)),
    (hotel.property_details?.description_text || "").slice(0, 80),
    JSON.stringify(hotel.rating_subscores || []),
    hotel.landmarks_desc || "",
    // 2026-10-18: verdict/one-line-review are rendered on the card - they
    // must be in the signature or a refresh with fresh review facts is
    // skipped as unchanged.
    hotel.rating_description || hotel.property_details?.rating_description || "",
    hotel.review_summary_line || "",
    (hotel.offer_tags || []).join(","),
    (hotel.room_options || []).map((option) => [
      (option.images || []).length, option.room_area || "",
      option.amenities?.length || 0]).join(";"),
    JSON.stringify(filters), currency || "", JSON.stringify(providers || []),
  ].join("~");
}
function openModal(title, bodyBuilder) {
  const overlay = document.createElement("div"); overlay.className = "modal-overlay";
  overlay.setAttribute("role", "dialog"); overlay.setAttribute("aria-modal", "true");
  const dialog = document.createElement("section"); dialog.className = "modal-dialog";
  const header = document.createElement("header"); header.className = "modal-header";
  addText(header, "modal-title", title);
  const close = document.createElement("button"); close.type = "button"; close.className = "modal-close"; close.textContent = "×"; close.setAttribute("aria-label", "Закрыть");
  header.append(close); dialog.append(header);
  const body = document.createElement("div"); body.className = "modal-body"; bodyBuilder(body);
  dialog.append(body); overlay.append(dialog); document.body.append(overlay);
  const remove = () => overlay.remove(); close.addEventListener("click", remove); overlay.addEventListener("click", (event) => { if (event.target === overlay) remove(); });
  return overlay;
}
function reviewLink(hotel) {
  const candidates = [hotel.review_url, hotel.reviews_url, hotel.property_details?.review_url,
    hotel.property_details?.reviews_url, hotel.source_hotel_url];
  for (const detail of hotel.provider_details || []) {
    candidates.push(detail?.review_url, detail?.reviews_url, detail?.source_hotel_url);
  }
  for (const record of hotel.source_records || []) {
    candidates.push(record?.review_url, record?.reviews_url, record?.source_hotel_url);
  }
  const explicitReviewUrl = candidates.map(safeLink).find(Boolean);
  if (explicitReviewUrl) return explicitReviewUrl;
  // Trip.com exposes its reviews on the public hotel detail page, but may not
  // publish a separate review URL in its result/detail payload. Keep the count
  // useful by linking to that real property page; never open a synthetic modal.
  const tripReviewCountKnown = (hotel.review_stats || []).some((item) =>
    item.source === "trip_com" && Number(item.reviews_count) > 0)
    || (hotel.source === "trip_com" && Number(hotel.reviews_count) > 0);
  if (tripReviewCountKnown) {
    const tripOffer = (hotel.offers || []).find((offer) =>
      offer.source === "trip_com" || offer.provider === "trip.com");
    return safeLink(tripOffer?.deeplink) || null;
  }
  return null;
}

function isKnownRoomType(value) {
  const name = String(value || "").trim();
  return Boolean(name && !/^(?:room|номер)\s*\d+$/i.test(name)
    && !/^(?:unknown|неизвестно|номер|room)$/i.test(name));
}
function knownRoomInventory(hotel) {
  return (Array.isArray(hotel.room_options) ? hotel.room_options : [])
    .filter((option) => isKnownRoomType(option?.room_type) && Array.isArray(option?.rates) && option.rates.length);
}

// Room inventory comes only from populated room_options. Provider offers stay
// in the price comparison and cannot create room variants by themselves.
function roomOfferRows(hotel) {
  const rows = [];
  const seen = new Set();
  const append = (value, defaults = {}) => {
    if (!value || typeof value !== "object") return;
    const row = {...defaults, ...value};
    const amount = row.amount ?? row.price_amount ?? row.price;
    const key = [row.provider || row.source || "", row.room_type || "", row.bed_type || "",
      amount ?? "", row.currency || "", row.meal_plan || "", row.cancellation || "",
      row.deeplink || ""].join("|");
    if (seen.has(key)) return;
    seen.add(key); rows.push(row);
  };
  for (const option of knownRoomInventory(hotel)) {
    for (const rate of Array.isArray(option?.rates) ? option.rates : []) {
      append(rate, {room_type: option.room_type, bed_type: option.bed_type,
        room_amenities: option.amenities, room_images: option.images || null,
        rooms_left: rate.rooms_left ?? null, original_price_display: rate.original_price_display || null,
        provider: "trip.com", source: "trip_com"});
    }
  }
  return rows.filter((row) => isKnownRoomType(row.room_type)
    && Number(row.amount ?? row.price_amount ?? row.price) > 0 && Boolean(row.currency));
}
function mealPresentation(row, labels, breakfastLabel = "Завтрак") {
  const plan = labels[row?.meal_plan];
  const raw = String(row?.meal_raw_text || "");
  const hasBreakfast = row?.breakfast_available === true || /breakfast|завтрак/i.test(raw);
  const extra = Number(row?.meal_extra_amount);
  const extraText = Number.isFinite(extra) && extra > 0 && row?.meal_extra_currency
    ? ` · ${formatMoney(extra, row.meal_extra_currency)}` : "";
  const explicitlyPaid = row?.meal_status === "paid"
    || /(?:breakfast|meal|board|dinner|lunch|завтрак|питан\w*|ужин|обед|пансион)[^|;]{0,100}(?:additional|extra|paid|charge|surcharge|optional|доплат|платн|по желанию|по запросу)/i.test(raw)
    || /(?:additional|extra|paid|charge|surcharge|optional|доплат|платн|по желанию|по запросу)[^|;]{0,100}(?:breakfast|meal|board|dinner|lunch|завтрак|питан\w*|ужин|обед|пансион)/i.test(raw);
  if (explicitlyPaid) return `${hasBreakfast ? breakfastLabel : (plan || "Питание")} за доплату${extraText}`;
  if (row?.meal_status === "not_included") return "Без питания";
  if (plan) return row?.meal_plan === "breakfast" ? `${breakfastLabel} включён` : plan;
  if (hasBreakfast) return "Завтрак доступен, включение не указано";
  return row?.meal_status === "unknown" || row?.meal_plan === "unknown" ? "Питание не указано" : (raw || "");
}
function openRoomsModal(hotel, nights) {
  openModal(`Варианты номеров · ${hotel.name}`, (body) => {
    const rows = roomOfferRows(hotel);
    if (!rows.length) { addText(body, "modal-empty", hotel.room_rate_enrichment_status === "loading" ? "Номера ещё загружаются…" : "Подтверждённые варианты номеров не предоставлены."); return; }
    const mealLabels = {room_only: "Без питания", breakfast: "Завтрак", half_board: "Полупансион",
      breakfast_dinner: "Завтрак + ужин", full_board: "Полный пансион", all_inclusive: "Всё включено",
      dinner: "Ужин", lunch_dinner: "Обед + ужин"};
    const groups = new Map();
    for (const row of rows) {
      const name = String(row.room_type).trim();
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(row);
    }
    for (const [roomType, offers] of groups) {
      const group = document.createElement("section"); group.className = "room-type-group";
      addText(group, "room-type-heading", roomType);
      // 2026-10-16: per-room photo gallery (rate payload physicRoomMap) and
      // the strikethrough pre-discount price + rooms-left scarcity.
      const roomImages = (offers.find((row) => Array.isArray(row.room_images) && row.room_images.length)?.room_images) || [];
      if (roomImages.length) {
        const gallery = document.createElement("div"); gallery.className = "room-modal-gallery";
        roomImages.slice(0, 5).forEach((url, index) => {
          const img = document.createElement("img");
          img.src = url; img.alt = `${roomType} — фото ${index + 1}`; img.loading = "lazy";
          img.decoding = "async"; img.className = "room-modal-photo";
          gallery.append(img);
        });
        group.append(gallery);
      }
      const scarcity = offers.find((row) => Number(row.rooms_left) > 0)?.rooms_left;
      if (Number(scarcity) > 0 && Number(scarcity) <= 9) {
        addText(group, "room-modal-scarcity",
          `Осталось ${scarcity} ${plural(Number(scarcity), "номер", "номера", "номеров")} по этой цене`);
      }
      for (const row of offers) {
        const block = document.createElement("article"); block.className = "room-modal-row";
        const details = [row.bed_type, row.room_area,
          row.occupancy?.adults ? `${row.occupancy.adults} ${plural(row.occupancy.adults, "гость", "гостя", "гостей")}` : null]
          .filter(Boolean);
        if (details.length) addText(block, "room-modal-meta", details.join(" · "));
        const line = document.createElement("div"); line.className = "room-modal-rate";
        const numericAmount = row.amount ?? row.price_amount;
        const rawPrice = row.price_display_text || formatMoney(numericAmount, row.currency);
        if (row.original_price_display && row.original_price_display !== rawPrice) {
          const old = document.createElement("span"); old.className = "room-modal-price-old";
          old.textContent = row.original_price_display; line.append(old);
        }
        addText(line, "room-modal-price", rawPrice);
        const meal = mealPresentation(row, mealLabels);
        if (meal) addText(line, "room-modal-meta", meal);
        const cancellation = row.cancellation_raw_text || (row.cancellation === "free" ? "Бесплатная отмена"
          : row.cancellation === "non_refundable" ? "Без возврата" : null);
        if (cancellation) addText(line, "room-modal-meta", cancellation);
        const charges = row.taxes_fees_raw_text || row.taxes || row.fees;
        if (charges) addText(line, "room-modal-meta", charges);
        const providerNameText = providerName(row, currentSearch?.providers || [], {trip_com: "Trip.com"});
        if (providerNameText && providerNameText !== "Источник —") {
          const provider = document.createElement("span"); provider.className = "room-modal-meta";
          provider.textContent = providerNameText; line.append(provider);
        }
        const link = safeLink(row.deeplink); if (link) { const a = document.createElement("a"); a.href = link; a.target = "_blank"; a.rel = "noopener noreferrer"; a.className = "room-modal-link"; a.textContent = "Открыть"; line.append(a); }
        block.append(line); group.append(block);
      }
      body.append(group);
    }
  });
}

function openAmenitiesModal(hotel) {
  openModal(`Удобства · ${hotel.name}`, (body) => {
    const values = new Set(hotelAmenities(hotel));
    let shown = 0;
    for (const group of AMENITY_GROUPS) {
      const keys = group.keys.filter((key) => values.has(key));
      if (!keys.length) continue;
      addText(body, "amenity-group-title", group.title);
      const list = document.createElement("div"); list.className = "amenity-modal-list";
      for (const key of keys) { addBadge(list, AMENITY_LABELS[key], "badge-amenity"); shown += 1; }
      body.append(list);
    }
    if (!shown) addText(body, "modal-empty", "Подтверждённые удобства не предоставлены.");
  });
}

function renderActiveFilters({mealLabels, cancellation, maxCenterDistance, maxBeachDistance,
  minPrice, maxPrice, minRating, minReviews, minStars, beachLine}) {
  const container = byId("active-filters");
  if (!container) return;
  container.replaceChildren();
  const entries = [];
  if (currentSearch?.query?.room_mode === "private") {
    entries.push(["Личный номер", async () => {
      const searchId = currentSearch.search_id, generation = searchGeneration;
      const response = await fetch(`/api/hotels/search/${encodeURIComponent(searchId)}?room_mode=all`);
      const data = await response.json();
      if (response.ok && generation === searchGeneration && currentSearch?.search_id === searchId) render(data);
    }]);
  }
  for (const meal of selectedMealPlans) entries.push([mealLabels[meal] || meal, () => selectedMealPlans.delete(meal)]);
  for (const amenity of selectedAmenities) entries.push([AMENITY_LABELS[amenity] || amenity, () => selectedAmenities.delete(amenity)]);
  if (cancellation === "free") entries.push(["Бесплатная отмена", () => { byId("cancellation").value = "any"; }]);
  if (maxCenterDistance) entries.push([`До центра ${maxCenterDistance} км`, () => { byId("max-center-distance").value = "0"; }]);
  if (maxBeachDistance) entries.push([`До пляжа ${maxBeachDistance} км`, () => { byId("max-beach-distance").value = "0"; }]);
  if (minPrice) entries.push([`От ${Number(minPrice).toLocaleString("ru-RU")} ₽/ночь`, () => { byId("min-price").value = ""; }]);
  if (maxPrice) entries.push([`До ${Number(maxPrice).toLocaleString("ru-RU")} ₽/ночь`, () => { byId("max-price").value = ""; }]);
  if (minRating) entries.push([`Рейтинг от ${minRating}`, () => { byId("min-rating").value = "0"; }]);
  if (minReviews) entries.push([`От ${minReviews} отзывов`, () => { byId("min-reviews").value = "0"; }]);
  if (minStars) entries.push([`От ${minStars}★`, () => { byId("min-stars").value = "0"; }]);
  if (beachLine && beachLine !== "any") entries.push([({first: "1-я линия", second: "2-я линия", third: "3-я линия"})[beachLine] || "Пляж",
    () => { byId("beach-line").value = "any"; }]);
  for (const [label, clear] of entries) {
    const chip = document.createElement("button"); chip.type = "button"; chip.className = "active-filter-chip";
    chip.textContent = `${label} ×`;
    chip.addEventListener("click", async () => { await clear(); if (currentSearch) render(currentSearch); });
    container.append(chip);
  }
  container.hidden = entries.length === 0;
}

function render(data) {
  currentSearch = data;
  if (cardVariantCacheSearchId !== data?.search_id) {
    cardVariantCacheSearchId = data?.search_id || null;
    cardVariantCache.clear();
  }
  if (data?.query?.room_mode === "all" && Array.isArray(data.hotels)) {
    roomModeBaseSearch = data;
  } else if (roomModeBaseSearch?.search_id !== data?.search_id) {
    roomModeBaseSearch = null;
  }
  if (!coordinateLoading) void loadMapCoordinates();
  startTripcomBackgroundEnrichment(data);
  const sourceLabels = {sravniotel: "СравниОтель", trip_com: "Trip.com"};
  const cards = byId("cards");
  const existingCards = new Map([...cards.querySelectorAll(":scope > .hotel-card")]
    .map((card) => [card.dataset.hotelKey, card]));
  cards.querySelectorAll(":scope > .empty").forEach((node) => node.remove());
  const nights = stayNights(data.query);
  const resultProgress = hotelSearchLifecycle.resultProgressCounts(data, hotelReadyForRender, (search, hotel) => {
    if (!hotelRequiredDataReady(search, hotel)) return false;
    const geoPending = !coordinatesProcessed(search, hotel);
    const ratesPending = hotelSourceRecords(hotel).some((record) => record.source === "trip_com"
      && !TERMINAL_TRIPCOM_ENRICHMENT.has(
        String(tripcomRecordStatus(hotel, record) || "").toLowerCase()));
    return geoPending || ratesPending;
  });
  let hotels = [...(data.hotels || [])].filter((hotel) => hotelReadyForRender(data, hotel));
  const minRating = Number(byId("min-rating").value);
  const minStars = Number(byId("min-stars").value);
  const minReviews = Number(byId("min-reviews").value || 0);
  const maxCenterDistance = Number(byId("max-center-distance").value || 0);
  const maxBeachDistance = Number(byId("max-beach-distance").value || 0);
  const minPrice = Number(byId("min-price").value || 0);
  const maxPrice = Number(byId("max-price").value || 0);
  const beachLine = byId("beach-line").value || "any";
  const priceCurrency = byId("price-currency").value;
  const cancellation = byId("cancellation").value;
  const mealLabels = {room_only: "Без питания", breakfast: "Завтрак", half_board: "Полупансион",
    full_board: "Полный пансион", all_inclusive: "Всё включено", breakfast_dinner: "Завтрак + ужин",
    dinner: "Ужин", lunch_dinner: "Обед + ужин"};
  const currencies = [...new Set(data.hotels.flatMap((hotel) => hotel.offers.map((offer) => offer.currency).filter(Boolean)))].sort();
  const activeCurrency = currencies.includes(priceCurrency) ? priceCurrency : (currencies[0] || "");
  const availableOffers = (hotel) => hotel.offers.filter((offer) => {
    if (selectedMealPlans.size && !selectedMealPlans.has(offer.meal_plan)) return false;
    if (cancellation === "free" && offer.cancellation !== "free") return false;
    return true;
  });
  const offersByHotel = new Map();
  const passesNumericFilter = (value, threshold) => !threshold || (value != null && Number(value) >= threshold);
  const passesPriceFilter = (offers) => (!minPrice && !maxPrice) || offers.some((offer) => {
    if (offer.currency !== activeCurrency || offer.price_basis !== "search_context_stay_total") return false;
    const nightly = Number(offer.nightly_price);
    return Number.isFinite(nightly) && nightly > 0
      && (!minPrice || nightly >= minPrice) && (!maxPrice || nightly <= maxPrice);
  });
  const passesLocationFilter = (hotel) => {
    if (!maxCenterDistance) return true;
    const distance = hotel.location?.distance_to_center_km;
    return distance != null && Number.isFinite(Number(distance)) && Number(distance) <= maxCenterDistance;
  };
  const passesBeachDistanceFilter = (hotel) => {
    if (!maxBeachDistance) return true;
    const distance = hotel.location?.distance_to_beach_km;
    return distance != null && Number.isFinite(Number(distance)) && Number(distance) <= maxBeachDistance;
  };
  const reviewCount = (hotel) => preferredReview(hotel)?.reviews_count ?? hotel.reviews_count;
  const comparableRating = (hotel) => hotel.rating_normalized != null
    ? hotel.rating_normalized
    : hotel.rating != null && hotel.rating_scale
      ? Number(hotel.rating) * 10 / Number(hotel.rating_scale) : hotel.rating;
  hotels = hotels.filter((hotel) => {
    const offers = availableOffers(hotel);
    offersByHotel.set(hotelKey(hotel), offers);
    return passesNumericFilter(comparableRating(hotel), minRating)
      && passesNumericFilter(hotel.stars, minStars)
      && passesNumericFilter(reviewCount(hotel), minReviews)
      && passesLocationFilter(hotel)
      && passesBeachDistanceFilter(hotel)
      && (beachLine === "any" || hotel.location?.beach_line === beachLine)
      && [...selectedAmenities].every((key) => hotelAmenities(hotel).includes(key))
      && offers.length > 0
      && passesPriceFilter(offers);
  });
  const sort = byId("sort").value;
  const hotelOffers = (hotel) => offersByHotel.get(hotelKey(hotel)) || [];
  const minOffer = (hotel) => hotelOffers(hotel).filter((offer) => !activeCurrency || offer.currency === activeCurrency)
    .map((offer) => Number(offer.amount)).filter((value) => Number.isFinite(value) && value > 0).sort((a, b) => a - b)[0] ?? Infinity;
  const locationDistance = (hotel) => {
    const raw = hotel.location?.distance_to_center_km;
    const calculated = Number(raw);
    if (hasDistance(raw)) return calculated;
    return (hotel.nearby_places || []).filter((place) => place.type === "city_center" && Number.isFinite(Number(place.distance_meters)))
      .map((place) => Number(place.distance_meters) / 1000).sort((a, b) => a - b)[0] ?? null;
  };
  if (sort === "price_asc") hotels.sort((a, b) => minOffer(a) - minOffer(b));
  if (sort === "rating_desc") hotels.sort((a, b) => (b.rating ?? -1) - (a.rating ?? -1));
  if (sort === "location") hotels.sort((a, b) => (locationDistance(a) ?? Infinity) - (locationDistance(b) ?? Infinity));
  const prices = hotels.map((hotel) => minOffer(hotel)).filter(Number.isFinite);
  const cheapestPrice = prices.length ? Math.min(...prices) : null;
  const bestRating = hotels.map((hotel) => preferredReview(hotel)?.rating).filter(Number.isFinite);
  const topRating = bestRating.length ? Math.max(...bestRating) : null;
  const centerDistances = hotels.map((hotel) => hotel.location?.distance_to_center_km).filter(Number.isFinite);
  const closestCenter = centerDistances.length ? Math.min(...centerDistances) : null;
  byId("results-area").hidden = false;
  byId("results-title").textContent = data.query.city_name || "Отели";
  const resultCount = `${resultProgress.discovered} найдено · ${resultProgress.ready} готово`;
  const preparingCount = resultProgress.pending ? ` · уточняем детали у ${resultProgress.pending}` : "";
  const unusableCount = resultProgress.unusable ? ` · ${resultProgress.unusable} без цены` : "";
  const pendingMode = roomModePending?.searchId === data.search_id ? roomModePending.mode : null;
  const modeUpdate = pendingMode ? ` · применяем фильтр «${pendingMode === "private" ? "Личный номер" : "Все варианты"}»` : "";
  const resultsMeta = byId("results-meta");
  resultsMeta.dataset.baseText = `${dateLabel(data.query.check_in)} – ${dateLabel(data.query.check_out)} · ${data.query.adults} ${plural(data.query.adults, "взрослый", "взрослых", "взрослых")} · ${resultCount}${preparingCount}${unusableCount}`;
  resultsMeta.textContent = resultsMeta.dataset.baseText + modeUpdate;
  if (pendingMode) resultsMeta.setAttribute("aria-busy", "true");
  else resultsMeta.removeAttribute("aria-busy");
  byId("load-more").hidden = !data.has_more;
  byId("results-area").dataset.roomMode = data.query.room_mode || "all";
  document.querySelectorAll("[data-room-mode]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.roomMode === (data.query.room_mode || "all")));
  });
  document.querySelectorAll("[data-sort]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.sort === sort)));
  document.querySelectorAll("[data-price]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.price) === maxPrice)));
  document.querySelectorAll("[data-beach-distance]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.beachDistance) === maxBeachDistance)));
  document.querySelectorAll("[data-rating]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.rating) === minRating)));
  document.querySelectorAll("[data-stars]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.stars) === minStars)));
  document.querySelectorAll("[data-meal]").forEach((button) => {
    const value = button.dataset.meal;
    button.setAttribute("aria-pressed", String(value === "any" ? selectedMealPlans.size === 0 : selectedMealPlans.has(value)));
  });
  const amenityFilter = byId("amenity-filter");
  const amenityOptions = byId("amenity-options");
  const availableAmenityKeys = [...new Set([...CORE_AMENITY_FILTERS, ...SUPPORTED_AMENITY_FILTERS,
    ...data.hotels.flatMap(hotelAmenities)])]
    .map(amenityKey).filter(Boolean).sort((a, b) => (AMENITY_LABELS[a] || a).localeCompare(AMENITY_LABELS[b] || b, "ru"));
  amenityFilter.hidden = !availableAmenityKeys.length;
  amenityOptions.replaceChildren();
  const anyAmenity = document.createElement("button"); anyAmenity.type = "button";
  anyAmenity.className = "amenity-any"; anyAmenity.dataset.amenity = "any";
  anyAmenity.textContent = "Любые"; amenityOptions.append(anyAmenity);
  for (const group of AMENITY_GROUPS) {
    const keys = group.keys.filter((key) => availableAmenityKeys.includes(key));
    if (!keys.length) continue;
    const section = document.createElement("section"); section.className = "amenity-filter-group";
    addText(section, "amenity-filter-group-title", group.title);
    const grid = document.createElement("div"); grid.className = "amenity-filter-grid";
    for (const key of keys) {
      const button = document.createElement("button"); button.type = "button";
      button.className = "amenity-filter-option"; button.dataset.amenity = key;
      button.textContent = AMENITY_LABELS[key] || key; grid.append(button);
    }
    section.append(grid); amenityOptions.append(section);
  }
  amenityOptions.querySelectorAll("[data-amenity]").forEach((button) => button.setAttribute("aria-pressed",
    String(button.dataset.amenity === "any" ? selectedAmenities.size === 0 : selectedAmenities.has(button.dataset.amenity))));
  renderActiveFilters({mealLabels, cancellation, maxCenterDistance, maxBeachDistance,
    minPrice, maxPrice, minRating, minReviews, minStars, beachLine});
  document.querySelectorAll("[data-cancellation]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.cancellation === cancellation)));
  document.querySelectorAll("[data-distance]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.distance) === maxCenterDistance)));
  document.querySelectorAll("[data-beach-line]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.beachLine === beachLine)));
  if (!hotels.length) {
    const sourceRows = data.sources || [];
    const allFailed = sourceRows.length && sourceRows.every((item) => !["ok", "no_results"].includes(item.status));
    const hasRawHotels = sourceRows.some((item) => Number(item.hotel_count) > 0);
    const message = resultProgress.pending ? `Подготавливаем ${resultProgress.pending} карточек: проверяем номера, питание и расстояния…`
      : data.hotels.length ? "Нет вариантов с такими условиями."
      : allFailed ? "Источники не вернули результаты поиска."
      : hasRawHotels ? "Не найдено отелей, подходящих по качеству и условиям."
      : "По выбранным датам доступных предложений нет.";
    cards.replaceChildren(); addText(cards, "empty", message); updateMap([]); return;
  }
  const orderedCards = [];
  for (const [index, hotel] of hotels.entries()) {
    const key = hotelKey(hotel);
    const renderSignature = cardRenderSignature(hotel, index, nights,
      {meals: [...selectedMealPlans].sort(), amenities: [...selectedAmenities].sort(), cancellation,
        minPrice, maxPrice, minRating, minReviews, minStars, maxCenterDistance, maxBeachDistance, beachLine},
      activeCurrency, data.providers || [], hotelOffers(hotel));
    const existing = existingCards.get(key);
    if (existing && existing.dataset.renderSignature === renderSignature) {
      orderedCards.push(existing); existingCards.delete(key);
      let variants = cardVariantCache.get(key);
      if (!variants) cardVariantCache.set(key, variants = new Map());
      variants.set(renderSignature, existing);
      continue;
    }
    const cached = cardVariantCache.get(key)?.get(renderSignature);
    if (cached) {
      orderedCards.push(cached); existingCards.delete(key);
      continue;
    }
    const card = document.createElement("article"); card.className = "hotel-card";
    card.dataset.hotelKey = key;
    card.dataset.renderSignature = renderSignature;
    const images = (hotel.images || []).filter((image) => image && safeLink(image.x1024 || image.x500 || image.url || image.x240));
    images.sort((a, b) => Number(b.role === "hotel_photo") - Number(a.role === "hotel_photo"));
    const imageUrl = safeHotelImage(hotel);
    if (imageUrl) {
      const media = document.createElement("div"); media.className = "hotel-media";
      const img = document.createElement("img"); img.className = "hotel-image"; img.src = safeLink(imageUrl);
      img.alt = hotel.name; img.loading = "lazy";
      img.addEventListener("error", () => {
        const nextImage = images.findIndex((image) => (image.x1024 || image.x500 || image.url || image.x240) === img.src);
        const fallback = images.slice(Math.max(0, nextImage + 1)).find((image) => safeLink(image.x1024 || image.x500 || image.url || image.x240));
        if (fallback) img.src = safeLink(fallback.x1024 || fallback.x500 || fallback.url || fallback.x240);
        else { media.replaceChildren(); addText(media, "image-placeholder", "Фото нет"); }
      });
      media.append(img);
      if (images.length > 1) {
        let imageIndex = 0;
        const counter = document.createElement("span"); counter.className = "photo-counter"; counter.textContent = "1 / " + images.length;
        const previous = document.createElement("button"); previous.className = "photo-arrow photo-prev"; previous.type = "button"; previous.textContent = "‹"; previous.title = "Предыдущее фото"; previous.setAttribute("aria-label", previous.title);
        const next = document.createElement("button"); next.className = "photo-arrow photo-next"; next.type = "button"; next.textContent = "›"; next.title = "Следующее фото"; next.setAttribute("aria-label", next.title);
        const selectImage = (step) => { imageIndex = (imageIndex + step + images.length) % images.length; img.src = safeLink(images[imageIndex].x1024 || images[imageIndex].x500 || images[imageIndex].url || images[imageIndex].x240); counter.textContent = (imageIndex + 1) + " / " + images.length; };
        previous.addEventListener("click", () => selectImage(-1)); next.addEventListener("click", () => selectImage(1));
        let touchStartX = null;
        media.addEventListener("touchstart", (event) => { touchStartX = event.changedTouches[0]?.clientX ?? null; }, {passive:true});
        media.addEventListener("touchend", (event) => {
          if (touchStartX === null) return;
          const delta = event.changedTouches[0].clientX - touchStartX;
          if (Math.abs(delta) > 45) selectImage(delta < 0 ? 1 : -1);
          touchStartX = null;
        }, {passive:true});
        media.append(previous, next, counter);
      }
      card.append(media);
    }
    else addText(card, "image-placeholder", "Фото нет");
    const main = document.createElement("div"); main.className = "hotel-main";
    const labels = document.createElement("div"); labels.className = "hotel-labels";
    if (index === 0) addText(labels, "hotel-label label-best", "Лучший выбор");
    const price = minOffer(hotel);
    if (cheapestPrice !== null && price === cheapestPrice) addText(labels, "hotel-label label-price", "Низкая цена");
    const review = preferredReview(hotel);
    if (review && topRating !== null && review.rating === topRating) addText(labels, "hotel-label label-rating", "Высокая оценка");
    if (closestCenter !== null && hotel.location?.distance_to_center_km === closestCenter) addText(labels, "hotel-label label-location", "Ближе к центру");
    if (labels.childElementCount) main.append(labels);
    const meta = document.createElement("div"); meta.className = "hotel-meta";
    if (review && Number(review.rating) > 0) {
      const rating = document.createElement("span"); rating.className = "rating";
      rating.textContent = `${Number(review.rating).toFixed(1)}${review.rating_scale ? `/${review.rating_scale}` : ""} ★`;
      meta.append(rating);
    } else addText(meta, "rating unknown", "Рейтинг —");
    const reviewCountValue = review?.reviews_count == null ? Number.NaN : Number(review.reviews_count);
    const reviewText = Number.isInteger(reviewCountValue) && reviewCountValue >= 0
      ? `${reviewCountValue.toLocaleString("ru-RU")} ${plural(reviewCountValue, "отзыв", "отзыва", "отзывов")}` : "Отзывы —";
    const reviewUrl = reviewLink(hotel);
    const reviews = reviewUrl ? document.createElement("a") : document.createElement("span");
    reviews.className = reviewUrl ? "reviews review-button" : "reviews";
    reviews.textContent = reviewText;
    if (reviewUrl) { reviews.href = reviewUrl; reviews.target = "_blank"; reviews.rel = "noopener noreferrer"; reviews.title = "Открыть отзывы у источника"; }
    meta.append(reviews);
    const cardSubscores = hotel.rating_subscores
      || hotel.property_details?.rating_subscores || [];
    const reviewLevel = hotel.review_level || hotel.property_details?.review_level;
    const subScores = cardSubscores;
    if (subScores.length) {
      const subLine = document.createElement("div"); subLine.className = "subscores";
      for (const sub of subScores.slice(0, 4)) {
        const score = Number(sub?.score);
        if (!Number.isFinite(score) || score <= 0) continue;  // guard: no "0.0" placeholders
        const bar = document.createElement("span"); bar.className = "subscore";
        const label = document.createElement("i"); label.textContent = `${sub.name} ${score.toFixed(1)}`;
        const fill = document.createElement("b");
        fill.style.width = `${Math.min(100, Math.round(Number(sub.score) * 10))}%`;
        bar.append(label, fill); subLine.append(bar);
      }
      meta.append(subLine);
    }
    if (reviewLevel) addText(meta, "review-level", reviewLevel);
    const ratingDescription = hotel.rating_description || hotel.property_details?.rating_description;
    if (ratingDescription) addText(meta, "rating-description", ratingDescription);
    if (hotel.review_stats?.length > 1) {
      const sources = hotel.review_stats.filter((item) => Number(item?.rating) > 0 && item.source !== review?.source)
        .map((item) => `${sourceLabels[item.source] || item.source}: ${Number(item.rating).toFixed(1)}${item.rating_scale ? `/${item.rating_scale}` : ""}${Number(item.reviews_count) > 0 ? ` · ${Number(item.reviews_count).toLocaleString("ru-RU")}` : ""}`);
      if (sources.length) addText(meta, "secondary-rating", sources.join(" · "));
    }
    const offerTags = (hotel.offer_tags || []).filter(Boolean).slice(0, 3);
    const scarcityTag = offerTags.find((tag) => /осталось всего \d+|по этой цене/i.test(tag));
    if (scarcityTag) {
      const scarcity = document.createElement("div"); scarcity.className = "scarcity-line";
      scarcity.textContent = scarcityTag; main.append(scarcity);
    }
    main.append(meta);
    const context = document.createElement("div"); context.className = "hotel-context";
    if (data.query?.check_in && data.query?.check_out) {
      addText(context, "stay-context-line",
        `${dateLabel(data.query.check_in)} — ${dateLabel(data.query.check_out)} · ${nights} ${plural(nights, "ночь", "ночи", "ночей")}`);
    }
    // 2026-10-18: moved below the context declaration - the previous spot
    // referenced `context` before initialization (TDZ ReferenceError that
    // would abort the whole render as soon as the field arrives).
    const reviewSummaryLine = hotel.review_summary_line;
    if (reviewSummaryLine) addText(context, "review-summary", `«${reviewSummaryLine}»`);
    const centerRaw = hotel.location?.distance_to_center_km;
    const centerDistance = Number(centerRaw);
    if (hasDistance(centerRaw)) {
      addText(context, "location-line", `📍 ${formatDistance(centerDistance)} до центра`);
    }
    const beachRaw = hotel.location?.distance_to_beach_km;
    const beachDistance = Number(beachRaw);
    // 2026-10-15: applicability must be explicit (beach_relevant === true).
    // beach_relevant=null (city not in a geo profile) used to pass the !==false
    // gate and rendered "до пляжа" for inland cities (Paris live smoke).
    const beachApplicable = hotel.location?.beach_relevant === true
      && hotel.location?.beach_status !== "not_applicable";
    if (beachApplicable) {
      if (hasDistance(beachRaw)) {
        addText(context, "beach-line", `🏖 ${formatDistance(beachDistance)} до пляжа`);
      }
    }
    // 2026-10-15: one metro line, best source. OSM stage distance and the
    // provider landmark (also a metro station) used to render as two
    // different lines with different distances. Merge them: keep the
    // closest one, show the station name, walking minutes when walkable.
    const osmMetro = hotel.location?.nearby_geo_places?.metro;
    const metroValue = hotel.location?.distance_to_metro_km;
    const metroDistance = Number(metroValue);
    const providerMetro = hotel.location?.landmark_type === "metro"
      ? {name: hotel.location?.landmark_name, km: hotel.location?.distance_to_landmark_km}
      : null;
    let metroLine = null;
    if (hasDistance(metroValue)) {
      metroLine = {km: metroDistance, name: (osmMetro && osmMetro.name) || (providerMetro && providerMetro.name) || null};
      if (providerMetro && hasDistance(providerMetro.km) && Number(providerMetro.km) < metroDistance) {
        metroLine = {km: Number(providerMetro.km), name: providerMetro.name};
      }
    } else if (providerMetro && hasDistance(providerMetro.km)) {
      metroLine = {km: Number(providerMetro.km), name: providerMetro.name};
    }
    if (metroLine) {
      const station = metroLine.name ? ` (${metroLine.name})` : "";
      addText(context, "geo-distance-line",
        `🚇 ${formatDistance(metroLine.km)} до метро${station}` + walkingMinutes(metroLine.km));
    }
    const airportValue = hotel.location?.distance_to_airport_km;
    const airportDistance = Number(airportValue);
    if (airportValue !== null && airportValue !== undefined && Number.isFinite(airportDistance) && airportDistance >= 0) {
      addText(context, "geo-distance-line", `✈ ${formatDistance(airportDistance)} до аэропорта`);
    }
    if (hotel.location?.distance_to_landmark_km !== null && hotel.location?.distance_to_landmark_km !== undefined
      && hotel.location?.landmark_name) {
      // 2026-10-15: metro/airport/beach provider landmarks duplicate the
      // dedicated lines above - only an unrelated landmark (district,
      // sight) renders here.
      const landmarkType = String(hotel.location.landmark_type || "");
      const duplicated = ["metro", "subway", "station", "airport", "beach"].includes(landmarkType);
      if (!duplicated) {
        addText(context, "landmark-line",
          `📍 ${formatDistance(hotel.location.distance_to_landmark_km)} до ${hotel.location.landmark_name}`
          + walkingMinutes(hotel.location.distance_to_landmark_km));
      }
    }
    const osmLandmarkValue = hotel.location?.distance_to_osm_landmark_km;
    if (osmLandmarkValue !== null && osmLandmarkValue !== undefined
      && hotel.location?.nearby_geo_places?.landmark) {
      const place = hotel.location.nearby_geo_places.landmark;
      addText(context, "geo-distance-line",
        `📍 ${formatDistance(Number(osmLandmarkValue))} до ${place.name || "достопримечательности"} · OpenStreetMap`);
    }
    if (hotel.landmarks_desc) addText(context, "landmarks-line", `📍 ${hotel.landmarks_desc}`);
    if (hotel.address) addText(context, "address", hotel.address);
    const amenities = hotelAmenities(hotel);
    const amenityLine = document.createElement("div"); amenityLine.className = "amenities-line";
    const amenityBadges = document.createElement("div"); amenityBadges.className = "hotel-badges";
    if (amenities.length) {
      const visibleAmenities = [...selectedAmenities, ...amenities.filter((key) => !selectedAmenities.has(key))];
      visibleAmenities.slice(0, 4).forEach((key) => addBadge(amenityBadges, AMENITY_LABELS[key] || key, "badge-amenity"));
      if (amenities.length > 4) {
        const more = document.createElement("button"); more.type = "button"; more.className = "hotel-badge badge-more";
        more.textContent = `+${amenities.length - 4} удобства`; more.setAttribute("aria-label", `Показать все удобства: ${hotel.name}`);
        more.addEventListener("click", () => openAmenitiesModal(hotel)); amenityBadges.append(more);
      }
    } // 2026-10-15: no "—" amenity placeholder - unknown amenities are omitted
    amenityLine.append(amenityBadges); context.append(amenityLine);
    const policy = hotelPolicy(hotel);
    // 2026-10-15: no dash placeholders. The check-in/out TIMES exist only in
    // Trip.com detail pages (forbidden to open); Sravni's list API and the
    // rate endpoint carry no policy times. When neither time is known the
    // whole line is omitted instead of rendering "Заезд — · Выезд —".
    if (policy.checkIn || policy.checkOut) {
      addText(context, "policy-line",
        `Заезд ${policy.checkIn || "не указан"} · Выезд ${policy.checkOut || "не указан"}`);
    }
    const earlyLateTags = (hotel.offer_tags || []).filter((tag) => /(?:поздний выезд|ранний заезд|late check|early check)/i.test(tag));
    if (earlyLateTags.length) {
      addText(context, "offer-tag-line", earlyLateTags.join(" · "));
    }
    // 2026-10-16: property description (sravni structured API) - the first
    // sentences give the location context users scan for.
    const descriptionText = String(hotel.property_details?.description_text || "").trim();
    if (descriptionText.length > 40) {
      const descLine = document.createElement("details"); descLine.className = "hotel-description";
      const summary = document.createElement("summary"); summary.textContent = "Об отеле";
      const text = document.createElement("p"); text.textContent = descriptionText.slice(0, 400);
      descLine.append(summary, text); context.append(descLine);
    }
    if (hotel.review_summary?.phrases?.length) {
      const themeNames = {location: "расположение", cleanliness: "чистота", breakfast: "завтрак", staff: "персонал", room: "номера", noise: "шум", bathroom: "ванная", pool: "бассейн", value: "цена и качество"};
      const positive = (hotel.review_summary.positive?.length ? hotel.review_summary.positive : hotel.review_summary.mentions || [])
        .map((theme) => themeNames[theme] || theme);
      const uniquePositive = [...new Set(positive)];
      if (uniquePositive.length) {
        const block = document.createElement("div"); block.className = "review-block";
        addText(block, "review-caption", "Гости чаще хвалят");
        const chips = document.createElement("div"); chips.className = "review-chips";
        uniquePositive.slice(0, 3).forEach((theme) => addText(chips, "review-chip", theme));
        block.append(chips);
        const themePatterns = {
          location: /располож|местополож|локац|location/i,
          cleanliness: /чист|уборк|clean/i,
          breakfast: /завтрак|breakfast/i,
          staff: /персонал|сервис|обслужив|staff|service/i,
          room: /номер|комнат|room/i,
          noise: /шум|noise/i,
          bathroom: /ванн|душ|bathroom/i,
          pool: /бассейн|pool/i,
          value: /соотнош|цена|качеств|value|price/i,
        };
        const sourceThemes = hotel.review_summary.positive?.length ? hotel.review_summary.positive : hotel.review_summary.mentions || [];
        const phrases = [...new Set(hotel.review_summary.phrases || [])]
          .filter((phrase) => !sourceThemes.some((theme) => themePatterns[theme]?.test(phrase)))
          .slice(0, 1);
        if (phrases.length) addText(block, "review-quotes", `«${phrases[0]}»`);
        context.append(block);
      }
    }
    if (context.childElementCount) main.append(context);
    const title = document.createElement("h2"); title.className = "hotel-title"; title.textContent = hotel.name;
    const starCount = Math.max(0, Math.min(5, Math.round(Number(hotel.stars) || 0)));
    // 2026-10-15 user directive: no dash after the hotel name. Hotels without
    // a star classification (guest houses, apartments, unclassified) show no
    // star marker at all instead of a misleading '—' placeholder.
    if (starCount) {
      const titleStars = document.createElement("span"); titleStars.className = "title-stars";
      titleStars.textContent = ` ${"★".repeat(starCount)}`;
      title.append(titleStars);
    }
    main.prepend(title);
    card.append(main);
    const offerPanel = document.createElement("div"); offerPanel.className = "offer-panel";
    const offers = hotelOffers(hotel).filter((offer) => Number.isFinite(Number(offer.amount)) && Number(offer.amount) > 0 && offer.currency);
    const comparable = offers.filter((offer) => !activeCurrency || offer.currency === activeCurrency);
    const rankedComparable = [...comparable].sort((a, b) => Number(a.amount) - Number(b.amount));
    // A missing deeplink must not make a known price disappear from the
    // canonical card. Prefer a bookable offer when available, otherwise keep
    // the cheapest verified amount as the main offer and show it without CTA.
    const best = rankedComparable.find((offer) => safeLink(offer.deeplink)) || rankedComparable[0];
    const roomOffer = best || offers.find((offer) => offer.room_privacy && offer.room_privacy !== "unknown");
    const roomName = roomOffer?.room_type || "";
    if (roomOffer?.room_privacy === "private") {
      const roomLabel = /studio|студи/i.test(roomName) ? "STUDIO" : "ЛИЧНЫЙ НОМЕР";
      const badge = addText(main, "room-privacy", roomLabel);
      badge.title = "Тип размещения подтверждён предложением";
      main.insertBefore(badge, main.querySelector(".hotel-title"));
    }
    if (roomOffer?.room_type) {
      const guests = roomOffer.occupancy?.adults;
      addText(main, "room-detail", roomOffer.room_type + (guests ? ` · ${guests} ${plural(guests, "гость", "гостя", "гостей")}` : ""));
    }
    const offerMealLabels = {room_only: "Без питания", no_meal: "Без питания", breakfast: "Завтрак", half_board: "Полупансион",
      full_board: "Полный пансион", all_inclusive: "Всё включено", breakfast_dinner: "Завтрак + ужин", dinner: "Ужин",
      lunch_dinner: "Обед + ужин", unknown: "Питание не указано"};
    const offerBadges = document.createElement("div"); offerBadges.className = "hotel-badges offer-badges";
    // 2026-10-15: the card meal badge prefers the BEST known meal fact
    // across sources. The offer (Sravni) usually has meal_plan=unknown while
    // Trip.com room_rates carry the real meal facts (breakfast included);
    // the card previously showed "Питание не указано" even when room_rates
    // had the answer. Unknown meal facts omit the badge entirely.
    const mealSource = [roomOffer, ...(hotel.room_rates || [])]
      .find((row) => row && (row.meal_plan || row.meal_status)
        && !(row.meal_status === "unknown" && (row.meal_plan == null || row.meal_plan === "unknown") && !row.meal_raw_text));
    if (mealSource) {
      const mealLabel = mealPresentation(mealSource, offerMealLabels);
      if (mealLabel && mealLabel !== "Питание не указано") {
        const mealClass = mealSource.meal_status === "paid" ? "badge-meal"
          : mealSource.meal_status === "unknown" || mealSource.meal_plan === "unknown"
            ? "badge-muted" : "badge-meal";
        addBadge(offerBadges, mealLabel, mealClass);
      }
    }
    if (roomOffer?.cancellation === "free") addBadge(offerBadges, "Бесплатная отмена", "badge-cancel");
    if (offerBadges.childElementCount) main.append(offerBadges);
    if (best) {
      addText(offerPanel, "best-price-label", "Лучшая цена");
      addText(offerPanel, "best-price", formatMoney(best.amount, best.currency));
      const provider = providerName(best, data.providers, sourceLabels);
      if (provider) addText(offerPanel, "best-provider", provider);
      if (best.price_basis === "search_context_stay_total" && best.nightly_price != null) {
        addText(offerPanel, "price-period", `за ${nights} ${plural(nights, "ночь", "ночи", "ночей")}`);
        addText(offerPanel, "nightly-price", `${formatMoney(best.nightly_price, best.currency)} / ночь`);
      }
      const link = safeLink(best.deeplink);
      if (link) {
        const cta = document.createElement("a"); cta.className = "primary-offer-link";
        cta.href = link; cta.target = "_blank"; cta.rel = "noopener noreferrer";
        cta.textContent = provider ? `Открыть ${provider}` : "Открыть предложение"; offerPanel.append(cta);
      } else addText(offerPanel, "offer-link-missing", "Ссылка на бронирование —");
    } else {
      addText(offerPanel, "best-price-label", "Цена по запросу");
    }
    const providerOffers = new Map();
    for (const offer of offers) {
      const name = providerName(offer, data.providers, sourceLabels);
      if (!name) continue;
      const current = providerOffers.get(name);
      if (!current || Number(offer.amount) < Number(current.amount)) providerOffers.set(name, offer);
    }
    // Keep every provider visible in the card. The primary offer is still
    // highlighted above, but it must not remove the cheapest/competitor row
    // from the comparison list or hide offers quoted in another currency.
    const alternatives = [...providerOffers.values()]
      .sort((a, b) => Number(a.amount) - Number(b.amount));
    if (alternatives.length) {
      const details = document.createElement("div"); details.className = "offer-alternatives";
      addText(details, "alternatives-heading", `Сравнение цен · ${alternatives.length}`);
      alternatives.forEach((offer) => {
        const row = document.createElement("div"); row.className = "alternative-offer";
        if (offer === best) row.classList.add("alternative-best");
        addText(row, "alternative-provider", providerName(offer, data.providers, sourceLabels));
        addText(row, "alternative-price", formatMoney(offer.amount, offer.currency));
        details.append(row);
      });
      offerPanel.append(details);
    }
    const roomRows = roomOfferRows(hotel);
    if (roomRows.length) {
      const roomTypeCount = new Set(roomRows.map((row) => String(row.room_type).trim())).size;
      addText(offerPanel, "room-rate-summary", `${roomTypeCount} типов номеров · ${roomRows.length} предложений`);
      const roomsButton = document.createElement("button"); roomsButton.type = "button"; roomsButton.className = "rooms-open-button";
      roomsButton.textContent = `Показать варианты номеров (${roomRows.length})`; roomsButton.addEventListener("click", () => openRoomsModal(hotel, nights));
      offerPanel.append(roomsButton);
    } else {
      const status = hotel.room_rate_enrichment_status;
      if (status === "loading") addText(offerPanel, "room-rate-empty", "Загружаем номера и тарифы Trip.com…");
      else if (status === "no_rates" || status === "rate_inventory_absent") {
        addText(offerPanel, "room-rate-empty", "Тарифы Trip.com не предоставлены для этих дат.");
      } else if (status === "blocked") {
        addText(offerPanel, "room-rate-empty", "Trip.com временно ограничил загрузку тарифов.");
      } else if (status === "error" || status === "partial") {
        addText(offerPanel, "room-rate-empty", "Детали номеров Trip.com сейчас недоступны.");
      } else if (hotel.source_records?.some((record) => record.source === "trip_com")) {
        addText(offerPanel, "room-rate-empty", "Детали номеров будут загружены автоматически.");
      }
    }
    const actions = document.createElement("div"); actions.className = "card-actions";
    const mapButton = document.createElement("button"); mapButton.type = "button"; mapButton.className = "map-card-action"; mapButton.textContent = "На карте";
    mapButton.disabled = !validCoordinates(hotel.location?.coordinates);
    mapButton.addEventListener("click", () => focusHotel(hotel));
    actions.append(mapButton); card.append(offerPanel, actions);
    card.addEventListener("mouseenter", () => highlightHotel(hotel));
    card.addEventListener("mouseleave", () => highlightHotel(null));
    orderedCards.push(card); existingCards.delete(key);
    let variants = cardVariantCache.get(key);
    if (!variants) cardVariantCache.set(key, variants = new Map());
    variants.set(renderSignature, card);
    while (variants.size > 4) variants.delete(variants.keys().next().value);
  }
  // Bound the global variant cache: hundreds of hotels x 4 variants each
  // kept thousands of detached DOM trees alive after every filter/sort
  // change. Evicting the least recently *set* hotel keys keeps memory flat
  // while preserving variants for the hotels the user is actually looking
  // at; re-entering a filter just rebuilds an evicted card.
  while (cardVariantCache.size > 400) {
    cardVariantCache.delete(cardVariantCache.keys().next().value);
  }
  // Build the order off-DOM and insert in rAF chunks. One synchronous
  // replaceChildren of hundreds of cards produced a single long task
  // (0.8-1.8s) that froze scrolling and the map; chunked insertion keeps
  // the first cards visible immediately while later chunks land between
  // frames. A generation token cancels a still-running insertion when a
  // newer render starts, so the list can never show a stale mix.
  const insertionGeneration = ++cardInsertionGeneration;
  const CHUNK = 60;
  const insertChunk = (start) => {
    if (insertionGeneration !== cardInsertionGeneration) return;
    const fragment = document.createDocumentFragment();
    for (let i = start; i < Math.min(start + CHUNK, orderedCards.length); i += 1) {
      fragment.append(orderedCards[i]);
    }
    cards.append(fragment);
    if (start + CHUNK < orderedCards.length) {
      requestAnimationFrame(() => insertChunk(start + CHUNK));
    }
  };
  cards.replaceChildren();
  insertChunk(0);
  updateMap(hotels);
}

function preferredReview(hotel) {
  const values = (hotel.review_stats || []).filter((item) => Number.isFinite(Number(item.rating)) && Number(item.rating) > 0);
  if (!values.length) {
    const ratingKnown = hotel.rating !== null && hotel.rating !== undefined
      && Number.isFinite(Number(hotel.rating)) && Number(hotel.rating) > 0;
    const reviewsKnown = hotel.reviews_count !== null && hotel.reviews_count !== undefined
      && Number.isInteger(Number(hotel.reviews_count)) && Number(hotel.reviews_count) >= 0;
    if (ratingKnown || reviewsKnown) {
      return {source: hotel.source, rating: ratingKnown ? Number(hotel.rating) : null,
        reviews_count: reviewsKnown ? Number(hotel.reviews_count) : null};
    }
    return null;
  }
  return values.sort((a, b) => Number(b.reviews_count || 0) - Number(a.reviews_count || 0))[0] || null;
}
function providerName(offer, providers, sourceLabels) {
  if (offer.source === "trip_com" || offer.provider === "trip.com") return "Trip.com";
  const directoryName = providers.find((item) => item.code === offer.provider)?.display_name;
  if (directoryName) return directoryName;
  const providerLabels = {ostrovok: "Островок", "ostrovok.ru": "Островок", tutu: "Туту", "tutu.ru": "Туту",
    yandex: "Яндекс Путешествия", sutochno: "Суточно", "sutochno.ru": "Суточно"};
  const provider = String(offer.provider || "").toLowerCase();
  return providerLabels[provider] || sourceLabels?.[offer.source] || "Источник —";
}
function sourceStatus(status) {
  return ({unavailable: "временно недоступен", login_required: "нужен вход", blocked: "отказ источника",
    timeout: "время ожидания истекло", http_error: "ошибка источника", error: "ошибка поиска",
    partial: "получены не все данные", no_results: "нет предложений"})[status] || "нет данных";
}
function freshness(value) {
  if (!value) return "время неизвестно";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return "только что";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин назад`;
  return `${Math.floor(seconds / 3600)} ч назад`;
}
function formatDistance(km) {
  const value = Number(km);
  if (!Number.isFinite(value) || value < 0) return "расстояние неизвестно";
  return value < 1 ? `${Math.round(value * 1000)} м` : `${value.toLocaleString("ru-RU", {maximumFractionDigits: 1})} км`;
}
// Walking time at a calm pace (~4.5 km/h); shown for short reachable
// distances only - beyond 2 km a walk estimate stops being useful.
function walkingMinutes(km) {
  const value = Number(km);
  if (!Number.isFinite(value) || value < 0 || value > 2) return "";
  const minutes = Math.max(1, Math.round(value * 60 / 4.5));
  return ` (${minutes} ${plural(minutes, "минута", "минуты", "минут")} пешком)`;
}
function hasDistance(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) && Number(value) >= 0;
}
function dateLabel(value) {
  return new Intl.DateTimeFormat("ru-RU", {day: "numeric", month: "long"}).format(new Date(`${value}T00:00:00`));
}
function plural(value, one, few, many) {
  const n = Math.abs(Number(value)) % 100; const last = n % 10;
  if (n > 10 && n < 20) return many;
  if (last > 1 && last < 5) return few;
  return last === 1 ? one : many;
}

function showNotice(message, error = false) { const notice = byId("notice"); notice.textContent = message; notice.classList.toggle("error", error); notice.hidden = false; }
function clearNotice() { byId("notice").hidden = true; }

function showCompletedSourceStatus(data) {
  const sources = data.sources || [];
  const completed = sources.filter((item) => item.status !== "running");
  // Reaching our configured Trip.com card cap is a normal completed stop.
  // Older cached rows may still carry status=partial, so don't surface that
  // internal cap as a user-facing source warning.
  const partial = completed.filter((item) => item.status === "partial"
    && item.collection_reason !== "card_limit_reached");
  const failed = completed.filter((item) => !["ok", "no_results", "partial"].includes(item.status));
  const labels = {trip_com: "Trip.com", sravniotel: "СравниОтель"};
  if (partial.length) {
    const names = partial.map((item) => labels[item.source] || item.source);
    showNotice(`${names.join(", ")}: результаты неполные; показаны уже полученные карточки и предложения. Источник не завершил загрузку списка.`, !data.hotels.length);
  } else if (failed.length && data.hotels.length) {
    const names = failed.map((item) => labels[item.source] || item.source);
    showNotice(`${names.join(", ")}: ${failed.map((item) => sourceStatus(item.status)).join("; ")}. Показаны найденные варианты.`);
  } else if (failed.length && !data.hotels.length) {
    showNotice(`Источники не вернули результаты: ${failed.map((item) => `${labels[item.source] || item.source} — ${sourceStatus(item.status)}`).join("; ")}.`, true);
  } else if (!data.hotels.length && completed.some((item) => item.status === "no_results")) {
    showNotice("По выбранным датам доступных предложений нет.");
  }
}

function sourceRevision(data) {
  if (typeof data.progress_revision === "string") return data.progress_revision;
  return JSON.stringify([
    data.status,
    (data.sources || []).map((item) => [item.source, item.status, item.hotel_count, item.observed_at, item.http_status]),
    (data.hotels || []).map((hotel) => [hotelKey(hotel), hotel.source_records?.map((record) => record.source_hotel_id)]),
  ]);
}

function scheduleSourcePoll(searchId, generation, delay = 1400) {
  clearTimeout(sourcePollTimer);
  sourcePollTimer = setTimeout(async () => {
    if (generation !== searchGeneration || currentSearch?.search_id !== searchId) return;
    const roomMode = currentSearch?.query?.room_mode || "all";
    try {
      const progressResponse = await fetch(`/api/hotels/search/${encodeURIComponent(searchId)}/progress`);
      const progress = await progressResponse.json();
      if (!progressResponse.ok || generation !== searchGeneration || currentSearch?.search_id !== searchId) return;
      // Progress carries source status and totals independently from result
      // snapshots. Keep those current without replacing every card; /more,
      // room-rates/results and the coordinate stream deliver their own deltas.
      currentSearch = {...currentSearch, status: progress.status,
        total_hotels: progress.total_hotels, has_more: progress.has_more,
        sources: progress.sources || currentSearch.sources};
      showCompletedSourceStatus(currentSearch);
      const revision = sourceRevision(progress);
      if (revision !== lastSourceRevision) {
        const response = await fetch(`/api/hotels/search/${encodeURIComponent(searchId)}?room_mode=${roomMode}`);
        const data = await response.json();
        if (!response.ok || generation !== searchGeneration || currentSearch?.search_id !== searchId) return;
        lastSourceRevision = revision;
        render(data);
        showCompletedSourceStatus(data);
      }
      if ((progress.sources || []).some((item) => item.status === "running")) {
        scheduleSourcePoll(searchId, generation, 1800);
      }
    } catch {
      if (generation === searchGeneration && currentSearch?.search_id === searchId) {
        scheduleSourcePoll(searchId, generation, 3500);
      }
    }
  }, delay);
}

cityInput.addEventListener("input", () => {
  selectedCity = null; byId("geo-id").value = ""; clearTimeout(debounceTimer); cityLookupGeneration += 1; clearNotice();
  debounceTimer = setTimeout(() => lookupCities(cityInput.value).catch((error) => showNotice(error.message, true)), 250);
});
document.addEventListener("click", (event) => { if (!event.target.closest(".city-field")) cityOptions.hidden = true; });
form.addEventListener("submit", async (event) => {
  event.preventDefault(); clearNotice();
  if (!selectedCity) { showNotice("Выберите город из списка подсказок.", true); cityInput.focus(); return; }
  if (!byId("check-in").value || !byId("check-out").value) {
    showNotice("Выберите даты заезда и выезда.", true); showDateCalendar(byId("check-in").value ? "checkout" : "checkin"); return;
  }
  if (byId("check-out").value <= byId("check-in").value) { showNotice("Дата выезда должна быть позже даты заезда.", true); return; }
  const generation = ++searchGeneration;
  roomModeRequestRevision += 1;
  roomModePending = null;
  roomModeBaseSearch = null;
  searchRequestController?.abort();
  clearTimeout(sourcePollTimer); lastSourceRevision = "";
  if (geoRefreshTimer) { clearTimeout(geoRefreshTimer); geoRefreshTimer = null; }
  geoRefreshAttempts = 0;
  loadMorePromise = null;
  if (paginationAutoTimer) { clearTimeout(paginationAutoTimer); paginationAutoTimer = null; }
  coordinateRequestController?.abort(); coordinateRequestController = null;
  coordinateLoading = false;
  coordinateRequests.clear();
  coordinateProcessedHotels.clear();
  coordinateRetryCount = 0;
  clearTimeout(coordinateRetryTimer); coordinateRetryTimer = null;
  clearTimeout(coordinateRenderTimer);
  coordinateRenderTimer = null;
  selectedMarkerKey = null; mapFocusGeneration += 1;
  const controller = new AbortController();
  searchRequestController = controller;
  currentSearch = null;
  byId("results-area").hidden = true;
  byId("cards").replaceChildren();
  if (hotelMarkers) hotelMarkers.clearLayers();
  markerByKey.clear(); mapHotels = [];
  const button = form.querySelector("button[type=submit]"); button.textContent = "Ищем…";
  try {
    const response = await fetch("/api/hotels/search", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      geo_id: selectedCity.geo_id, city_name: selectedCity.name, country: selectedCity.country, check_in: byId("check-in").value,
      city_slug: selectedCity.city_slug, country_slug: selectedCity.country_slug,
      check_out: byId("check-out").value, adults: Number(byId("adults").value), children_count: Number(byId("children").value), limit: 20,
      room_mode: document.querySelector("[data-room-mode][aria-pressed=true]")?.dataset.roomMode || "all",
    }), signal: controller.signal});
    const data = await response.json();
    if (generation !== searchGeneration) return;
    if (!response.ok) throw new Error(data.detail?.message || data.detail || "Поиск не выполнен");
    render(data);
    scheduleNextPage(generation, data.search_id);
    if (mapMode === "map") void loadMapCoordinates();
    lastSourceRevision = sourceRevision(data);
    showCompletedSourceStatus(data);
    if ((data.sources || []).some((item) => item.status === "running")) scheduleSourcePoll(data.search_id, generation);
  } catch (error) {
    if (error.name !== "AbortError" && generation === searchGeneration) showNotice(error.message, true);
  }
  finally {
    if (generation === searchGeneration) {
      searchRequestController = null;
      button.innerHTML = '<span aria-hidden="true">⌕</span> Найти отели';
    }
  }
});

function scheduleNextPage(generation, searchId) {
  if (paginationAutoTimer || !currentSearch?.has_more || currentSearch.search_id !== searchId) return;
  paginationAutoTimer = setTimeout(() => {
    paginationAutoTimer = null;
    if (generation === searchGeneration && currentSearch?.search_id === searchId) void loadNextPage();
  }, 350);
}

function requestRoomModeSearch(searchId, mode) {
  return roomModeRequestCoalescer.run(`${searchId}:${mode}`, async () => {
    const response = await fetch(`/api/hotels/search/${encodeURIComponent(searchId)}?room_mode=${mode}`);
    const data = await response.json();
    return {ok: response.ok, data,
      error: data.detail?.message || data.detail || "Не удалось изменить тип размещения"};
  });
}

async function loadNextPage() {
  if (loadMorePromise || !currentSearch?.has_more) return;
  const searchAtStart = currentSearch;
  const generationAtStart = searchGeneration;
  let task;
  task = (async () => {
  clearNotice();
  const sentinel = byId("load-more");
  let pageSucceeded = false;
  try {
    const previous = searchAtStart;
    const beforeCount = previous.hotels.length;
    let data = previous, added = 0, pagesScanned = 0;
    while (data.has_more) {
      const response = await fetch(`/api/hotels/search/${encodeURIComponent(previous.search_id)}/more`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({room_mode: previous.query.room_mode || "all", delta: true}),
      });
      const page = await response.json();
      if (!response.ok) throw new Error(page.detail || "Не удалось загрузить страницу");
      if (!hotelSearchLifecycle.isCurrentSearch(
        searchAtStart.search_id, generationAtStart, currentSearch, searchGeneration,
      )) return;
      // A room-mode switch can finish while this page is in flight. Refresh
      // the already-persisted search in the selected mode instead of allowing
      // the older page response to switch the UI back.
      const desiredMode = roomModePending?.searchId === searchAtStart.search_id
        ? roomModePending.mode : (currentSearch?.query?.room_mode || "all");
      const pageMode = page.query?.room_mode || (searchAtStart.query.room_mode || "all");
      if (pageMode !== desiredMode) {
        const refreshed = await requestRoomModeSearch(searchAtStart.search_id, desiredMode);
        if (!hotelSearchLifecycle.isCurrentSearch(
          searchAtStart.search_id, generationAtStart, currentSearch, searchGeneration,
        )) return;
        if (!refreshed.ok) throw new Error(refreshed.error);
        data = refreshed.data;
        added = data.hotels.length - beforeCount;
      } else {
        // Coordinate enrichment can replace the search object while this page
        // is in flight. Always merge into the newest list for the same search.
        page.hotels = mergeHotelDelta(
          currentSearch?.hotels || data.hotels, page.hotels, page.delta_source_identity_keys,
        );
        added = page.hotels.length - beforeCount;
        data = page;
      }
      const changed = Array.isArray(page.delta_source_identity_keys)
        && page.delta_source_identity_keys.length > 0;
      pagesScanned += 1;
      if (pageMode !== desiredMode || added > 0 || changed || !data.has_more) break;
    }
    data.load_more_audit = {before: beforeCount, after: data.hotels.length,
      new_unique: Math.max(0, added), pages_scanned: pagesScanned};
    if (added <= 0 && !data.has_more) sentinel.hidden = true;
    if (generationAtStart !== searchGeneration || currentSearch?.search_id !== searchAtStart.search_id) return;
    render(data);
    pageSucceeded = true;
    if (mapMode === "map") void loadMapCoordinates();
    if (added <= 0 && !data.has_more) sentinel.hidden = true;
  } catch (error) {
    if (generationAtStart === searchGeneration && currentSearch?.search_id === searchAtStart.search_id) showNotice(error.message, true);
  }
  finally {
    if (loadMorePromise === task) loadMorePromise = null;
    // Keep discovering one page at a time in the background. Each response is
    // rendered before the next request, so complete cards remain progressive.
    if (pageSucceeded && generationAtStart === searchGeneration
        && currentSearch?.search_id === searchAtStart.search_id && currentSearch.has_more) {
      scheduleNextPage(generationAtStart, searchAtStart.search_id);
    }
  }
  })();
  loadMorePromise = task;
  await task;
}

if ("IntersectionObserver" in window) {
  loadMoreObserver = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) loadNextPage();
  }, {rootMargin: "700px 0px"});
  loadMoreObserver.observe(byId("load-more"));
}
for (const id of ["sort", "min-rating", "min-stars", "min-reviews", "max-center-distance", "max-beach-distance", "min-price", "max-price", "price-currency", "cancellation", "unknown-policy", "beach-line"]) byId(id).addEventListener("change", () => currentSearch && render(currentSearch));
for (const [attribute, inputId, dataKey] of [
  ["sort", "sort", "sort"], ["rating", "min-rating", "rating"], ["stars", "min-stars", "stars"],
  ["cancellation", "cancellation", "cancellation"], ["distance", "max-center-distance", "distance"],
  ["beach-distance", "max-beach-distance", "beachDistance"], ["price", "max-price", "price"],
  ["beach-line", "beach-line", "beachLine"],
]) {
  for (const button of document.querySelectorAll(`[data-${attribute}]`)) button.addEventListener("click", () => {
    byId(inputId).value = button.dataset[dataKey];
    render(currentSearch);
  });
}
// Meal choices use OR within the meal category; selected amenities use AND.
// Both sets survive source refreshes, sorting, map toggles and card updates.
document.addEventListener("click", (event) => {
  const mealButton = event.target.closest("[data-meal]");
  const amenityButton = event.target.closest("[data-amenity]");
  if ((!mealButton && !amenityButton) || !currentSearch) return;
  if (mealButton) {
    const value = mealButton.dataset.meal;
    if (value === "any") selectedMealPlans.clear();
    else selectedMealPlans.has(value) ? selectedMealPlans.delete(value) : selectedMealPlans.add(value);
    byId("meal").value = [...selectedMealPlans].join(",") || "any";
  }
  if (amenityButton) {
    const value = amenityButton.dataset.amenity;
    if (value === "any") selectedAmenities.clear();
    else selectedAmenities.has(value) ? selectedAmenities.delete(value) : selectedAmenities.add(value);
    byId("amenity").value = [...selectedAmenities].join(",") || "any";
  }
  render(currentSearch);
});
for (const button of document.querySelectorAll("[data-room-mode]")) button.addEventListener("click", async () => {
  if (!currentSearch) return;
  const searchId = currentSearch.search_id, generation = searchGeneration;
  const mode = button.dataset.roomMode;
  if (roomModePending?.searchId === searchId && roomModePending.mode === mode) return;
  if (mode === (currentSearch.query.room_mode || "all")) {
    const transition = hotelSearchLifecycle.cancelPendingRoomModeOnCurrentSelection(
      searchId, mode, currentSearch.query.room_mode || "all", roomModePending, roomModeRequestRevision,
    );
    if (transition.cancelled) {
      roomModeRequestRevision = transition.revision;
      roomModePending = transition.pending;
      const resultsMeta = byId("results-meta");
      resultsMeta.textContent = resultsMeta.dataset.baseText || resultsMeta.textContent;
      resultsMeta.removeAttribute("aria-busy");
    }
    return;
  }
  const requestRevision = ++roomModeRequestRevision;
  roomModePending = {searchId, mode, revision: requestRevision};
  const resultsMeta = byId("results-meta");
  resultsMeta.dataset.baseText ||= resultsMeta.textContent;
  resultsMeta.textContent = `${resultsMeta.dataset.baseText} · применяем фильтр «${mode === "private" ? "Личный номер" : "Все варианты"}»`;
  resultsMeta.setAttribute("aria-busy", "true");
  const baseSearch = roomModeBaseSearch?.search_id === searchId ? roomModeBaseSearch : null;
  if (baseSearch) {
    const projected = hotelSearchLifecycle.projectRoomModeSearch(baseSearch, mode);
    if (hotelSearchLifecycle.isCurrentSearch(searchId, generation, currentSearch, searchGeneration)
        && requestRevision === roomModeRequestRevision) {
      render(projected);
      roomModePending = null;
      resultsMeta.textContent = resultsMeta.dataset.baseText || resultsMeta.textContent;
      resultsMeta.removeAttribute("aria-busy");
    }
    return;
  }
  try {
    const result = await requestRoomModeSearch(searchId, mode);
    if (!result.ok) throw new Error(result.error);
    if (!hotelSearchLifecycle.isCurrentSearch(searchId, generation, currentSearch, searchGeneration)
        || requestRevision !== roomModeRequestRevision) return;
    render(result.data);
  } catch (error) {
    if (hotelSearchLifecycle.isCurrentSearch(searchId, generation, currentSearch, searchGeneration)
        && requestRevision === roomModeRequestRevision) showNotice(error.message, true);
  } finally {
    if (requestRevision === roomModeRequestRevision) {
      roomModePending = null;
      const meta = byId("results-meta");
      meta.textContent = meta.dataset.baseText || meta.textContent;
      meta.removeAttribute("aria-busy");
    }
  }
});
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => setViewMode(button.dataset.view));
byId("mobile-map-open").addEventListener("click", () => setViewMode("map"));
byId("mobile-map-close").addEventListener("click", () => closeMapModal());
byId("check-in-trigger").addEventListener("click", () => showDateCalendar("checkin"));
byId("check-out-trigger").addEventListener("click", () => showDateCalendar("checkout"));
byId("calendar-prev").addEventListener("click", () => { calendarMonth.setMonth(calendarMonth.getMonth() - 1); renderCalendar(); });
byId("calendar-next").addEventListener("click", () => { calendarMonth.setMonth(calendarMonth.getMonth() + 1); renderCalendar(); });
byId("calendar-months").addEventListener("pointerleave", () => { calendarHoverDate = null; updateCalendarPreview(); });
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !byId("date-calendar").hidden) closeDateCalendar(); });
document.addEventListener("click", (event) => {
  const eventPath = event.composedPath?.() || [];
  const clickedInside = eventPath.some((node) => node?.id === "date-calendar"
    || node?.classList?.contains("date-field") || node?.classList?.contains("date-trigger"))
    || Boolean(event.target.closest?.("#date-calendar, .date-field, .date-trigger"));
  if (!byId("date-calendar").hidden && !clickedInside) closeDateCalendar();
});
setDateDefaults();
