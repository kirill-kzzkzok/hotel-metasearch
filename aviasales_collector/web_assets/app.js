const PAGE_SIZE = 100;
const REFRESH_MS = 20000;
const PRICE_DROP_BADGE_PERCENT = 6.5;
const state = {origin: "ALL", query: "", activeOnly: false, page: 0, deepDestination: "", deepScans: new Map(), deepModalScan: null, originLabels: {ALL:"Все вылеты",MOW:"Москва",LED:"Санкт-Петербург"}};
let resultsRequestId = 0;
let resultsController = null;
let destinationsRequestId = 0;
let notificationsRequestId = 0;
let searchTimer;
let citySearchTimer;
let tripCompareRequestId = 0;
let tripCompareActiveOfferId = "";
const tripCompareStates = new Map();

function esc(value) {
  return String(value).replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
}
function values(value) { return value == null || value === "" ? [] : Array.isArray(value) ? value : [value]; }
function originLabel(code) { return code === "ALL" ? "Все вылеты" : state.originLabels[code] || code || ""; }
function originHeading(code) { return `${originLabel(code)} (${code})`; }
function notificationReadKey(origin) { return `tickets-notifications-read-${origin}`; }
function notificationReadId(origin) {
  const own = Number(localStorage.getItem(notificationReadKey(origin)) || 0);
  const legacyAll = Number(localStorage.getItem(notificationReadKey("ALL")) || 0);
  return Math.max(own, legacyAll);
}
function offerWord(count) { const mod100 = count % 100; const mod10 = count % 10; return mod100 >= 11 && mod100 <= 14 ? "билетов" : mod10 === 1 ? "билет" : mod10 >= 2 && mod10 <= 4 ? "билета" : "билетов"; }
function safeUrl(value, image = false) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const allowed = host === "avs.io" || host.endsWith(".avs.io") || host === "aviasales.com" || host.endsWith(".aviasales.com") || host === "aviasales.ru" || host.endsWith(".aviasales.ru");
    return url.protocol === "https:" && !url.username && !url.password
      && (!url.port || url.port === "443") && (!image || allowed) ? url.href : null;
  } catch { return null; }
}
function safeTripcomFlightUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const allowed = host === "trip.com" || host.endsWith(".trip.com");
    return url.protocol === "https:" && allowed && !url.username && !url.password
      && (!url.port || url.port === "443") && url.pathname.toLowerCase().startsWith("/flights")
      ? url.href : null;
  } catch { return null; }
}
const FALLBACK_AIRLINE_IATA = new Map([
  ["hainan airlines", "HU"],
  ["hainan airline", "HU"],
  ["etihad airways", "EY"],
  ["etihad airway", "EY"],
  ["etihad", "EY"],
  ["emirates", "EK"],
]);
const FALLBACK_AIRLINE_NAME = new Map([
  ["HU", "Hainan Airlines"],
  ["EY", "Etihad Airways"],
  ["EK", "Emirates"],
]);
function airlinesFromTicketSource(source) {
  try {
    const url = new URL(source);
    const host = url.hostname.toLowerCase();
    if (!(host === "aviasales.ru" || host.endsWith(".aviasales.ru") || host === "aviasales.com" || host.endsWith(".aviasales.com"))) return [];
    const token = url.searchParams.get("t") || "";
    // Aviasales' `t` token contains a carrier code immediately before each
    // 10-digit departure timestamp; a codeshare itinerary may contain several.
    return [...new Set(Array.from(token.matchAll(/(?<![A-Z0-9])([A-Z0-9]{2})(?=\d{10})/g), match => match[1]))];
  } catch { return []; }
}
function airlineLogoUrl(name) {
  const directCode = /^[A-Z0-9]{2}$/.test(name) ? name : null;
  const iata = directCode || FALLBACK_AIRLINE_IATA.get(String(name).normalize("NFKC").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim());
  return iata ? `https://img.avs.io/pics/al_square/${iata}@avif?rs=fit:120:120` : null;
}
function formattedPrice(offer) {
  if (offer.price == null) return "";
  const amount = Number(offer.price);
  return `${Number.isFinite(amount) ? new Intl.NumberFormat("ru-RU").format(amount) : offer.price} ${offer.currency === "RUB" ? "₽" : esc(offer.currency || "")}`.trim();
}
function carrierMarkup(offer, legIndex) {
  const legNames = values(offer.airline_by_leg?.[legIndex]);
  const names = values(legNames.length ? legNames : offer.airline).map(item => typeof item === "string" ? item : item?.name).filter(Boolean).map(String);
  if (!names.length) {
    const inferredNames = airlinesFromTicketSource(offer.source_url).map(code => FALLBACK_AIRLINE_NAME.get(code) || code);
    names.push(...inferredNames);
  }
  const logos = new Map(values(offer.airline_logos).filter(item => item?.name && item?.url).map(item => [item.name, item.url]));
  if (!names.length) return "";
  return `<div class="flight-carriers" aria-label="Авиакомпании этого рейса">${names.map(name => {
    const url = safeUrl(logos.get(name), true) || safeUrl(airlineLogoUrl(name), true);
    const initials = /^[A-Z0-9]{2}$/.test(name) ? name : name.split(/\s+/).slice(0, 2).map(part => part[0] || "").join("").toUpperCase();
    return `<span class="airline-logo" title="${esc(name)}" aria-label="${esc(name)}"><span>${esc(initials)}</span>${url ? `<img loading="lazy" data-src="${esc(url)}" alt="">` : ""}</span>`;
  }).join("")}</div>`;
}
function stopsForLeg(offer, legIndex, tripType) {
  const raw = offer.stops;
  if (Array.isArray(raw)) return raw[legIndex];
  return tripType === "OW" || legIndex === 0 ? raw : null;
}
function legTimes(offer, legIndex, tripType, stopCounts) {
  const times = values(offer.flight_times);
  if (!times.length) return [null, null];
  if (tripType === "OW") return times.length >= 2 ? [times[0], times.at(-1)] : [null, null];
  if (times.length === 4 && stopCounts.some(value => value == null || value === "")) return [times[legIndex * 2], times[legIndex * 2 + 1]];
  if (!stopCounts.every(value => value != null && Number.isInteger(Number(value)))) {
    return times.length >= 4 ? (legIndex === 0 ? [times[0], times[1]] : [times.at(-2), times.at(-1)]) : [null, null];
  }
  const expected = stopCounts.map(value => 2 * (Number(value) + 1));
  if (times.length !== expected.reduce((sum, count) => sum + count, 0)) {
    return times.length >= 4 ? (legIndex === 0 ? [times[0], times[1]] : [times.at(-2), times.at(-1)]) : [null, null];
  }
  const start = expected.slice(0, legIndex).reduce((sum, count) => sum + count, 0);
  return [times[start], times[start + expected[legIndex] - 1]];
}
function transferGroups(offer, stopCounts, tripType) {
  const cities = values(offer.transfers).map(String);
  const durations = values(offer.transfer_durations).map(String);
  const counts = tripType === "RT"
    ? stopCounts.map(value => value == null || value === "" ? NaN : Number(value))
    : [stopCounts[0] == null || stopCounts[0] === "" ? cities.length : Number(stopCounts[0])];
  const unknown = counts.map((value, index) => Number.isInteger(value) ? -1 : index).filter(index => index >= 0);
  if (unknown.length === 1) {
    counts[unknown[0]] = Math.max(0, cities.length - counts.reduce((sum, value) => sum + (Number.isInteger(value) ? value : 0), 0));
  } else if (unknown.length === counts.length && cities.length && cities.length % counts.length === 0) {
    counts.fill(cities.length / counts.length);
  }
  if (!counts.every(Number.isInteger)) return counts.map(() => []);
  const unassignedCities = cities.length - counts.reduce((sum, count) => sum + count, 0);
  if (unassignedCities > 0 && counts.length) counts[0] += unassignedCities;
  let offset = 0;
  return counts.map(count => {
    const group = cities.slice(offset, offset + count).map((city, index) => ({city, duration: durations[offset + index] || null}));
    offset += count;
    return group;
  });
}
function airportsForLeg(offer, legIndex) {
  return values(offer.airports_by_leg?.[legIndex]).filter(item => item && typeof item === "object");
}
function arrivalDateLabel(offer, legIndex, tripType, stopCounts) {
  const offsets = values(offer.arrival_day_offsets);
  if (!offsets.length) return "";
  let arrivalIndex;
  if (stopCounts.every(value => value != null && Number.isInteger(Number(value)))) {
    const precedingSegments = stopCounts.slice(0, legIndex).reduce((sum, value) => sum + Number(value) + 1, 0);
    arrivalIndex = precedingSegments + Number(stopCounts[legIndex]);
  } else if (offsets.length === (tripType === "RT" ? 2 : 1)) arrivalIndex = legIndex;
  else arrivalIndex = offsets.length - 1;
  const days = Number(offsets[arrivalIndex]);
  if (!Number.isInteger(days) || days < 1 || days > 2) return "";
  const base = legIndex === 0 ? offer.departure_date : (offer.return_date || offer.return_date_raw);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(base || ""))) return `+${days}`;
  const date = new Date(`${base}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toLocaleDateString("ru-RU", {day:"numeric",month:"short",timeZone:"UTC"}).replace(/\.$/, "");
}
function routeTrack(offer, transfers, legIndex, tripType, rawStopCount) {
  const stopCounts = Array.from({length:tripType === "RT" ? 2 : 1}, (_, index) => stopsForLeg(offer, index, tripType));
  const airports = airportsForLeg(offer, legIndex);
  const outbound = legIndex === 0;
  const first = airports[0];
  const last = airports.at(-1);
  const point = (airport, fallback) => {
    const code = String(airport?.code || "").trim().toUpperCase();
    if (/^[A-Z]{3}$/.test(code)) return code;
    const fallbackCode = String(fallback || "").trim().toUpperCase();
    return /^[A-Z]{3}$/.test(fallbackCode) ? fallbackCode : "—";
  };
  const outboundAirports = tripType === "RT" ? airportsForLeg(offer, 0) : [];
  const returnAirports = tripType === "RT" ? airportsForLeg(offer, 1) : [];
  const destinationCode = outboundAirports.at(-1)?.code || returnAirports[0]?.code || offer.destination;
  const originCode = offer.origin;
  const reportedStops = rawStopCount == null || rawStopCount === "" ? 0 : Math.max(0, Number(rawStopCount) || 0);
  const stopCount = Math.max(reportedStops, transfers.length);
  const markerCount = Math.max(stopCount, transfers.length, airports.length - 2);
  const markers = Array.from({length: markerCount}, (_, index) => ({
    ...(transfers[index] || {}),
    airport: airports[index + 1],
  }));
  const stopMarkup = markers.map(stop => {
    const label = stop.city || stop.airport?.name || stop.airport?.code;
    return `<span class="route-stop" title="${esc(label || "Пересадка")}${stop.duration ? ` · ${esc(stop.duration)}` : ""}"><i></i></span>`;
  }).join("");
  const stopLabel = stopCount ? `${stopCount} ${stopCount === 1 ? "пересадка" : "пересадки"}` : "Прямой рейс";
  const duration = values(offer.flight_duration)[legIndex];
  const cleanDuration = duration ? String(duration).replace(/\s+в пути.*$/i, "").trim() : "";
  const transferLabel = stopCount
    ? `${stopLabel}${transfers.length ? ` · ${transfers.map(stop => `${stop.city}${stop.duration ? `, ${stop.duration}` : ""}`).join(" · ")}` : ""}`
    : "Без пересадок";
  const requiresReRegistration = values(offer.repeat_registration_by_leg)[legIndex] === true;
  const transferClass = requiresReRegistration ? " route-details--repeat-registration" : "";
  return `<div class="route-wrap"><div class="route-label">${cleanDuration ? `${esc(cleanDuration)} в пути` : "Время в пути не указано"}</div><div class="route-track"><span class="route-end"></span><span class="route-code">${esc(point(first, outbound ? originCode : destinationCode))}</span><span class="route-line">${stopMarkup}</span><span class="route-code">${esc(point(last, outbound ? destinationCode : originCode))}</span><span class="route-end"></span></div><div class="route-details${transferClass}" title="${esc(transferLabel)}">${esc(transferLabel)}</div></div>`;
}
function legMarkup(offer, tripType, legIndex, stopCounts, transfers) {
  const outbound = legIndex === 0;
  const from = outbound ? offer.origin : offer.destination;
  const to = outbound ? offer.destination : offer.origin;
  const date = outbound ? (offer.departure_date_raw || offer.departure_date) : (offer.return_date_raw || offer.return_date);
  const [departureTime, arrivalTime] = legTimes(offer, legIndex, tripType, stopCounts);
  const arrivalDate = arrivalDateLabel(offer, legIndex, tripType, stopCounts);
  const tripDays = tripType === "RT" && outbound && Number(offer.trip_duration_days) > 0 ? `<em class="trip-days">${Number(offer.trip_duration_days)} дн.</em>` : "";
  const legName = tripType === "RT" ? (outbound ? "Туда" : "Обратно") : "Перелёт";
  const airline = carrierMarkup(offer, legIndex);
  const originName = value => originLabel(value);
  return `<section class="flight-leg"><div class="flight-leg__from"><div class="flight-leg__heading">${legName}${date ? `<span>${esc(date)}</span>` : ""}${tripDays}</div>${airline}<div class="flight-leg__time">${departureTime ? esc(departureTime) : "&nbsp;"}</div><div class="flight-leg__place">${esc(originName(from))}</div></div>${routeTrack(offer, transfers, legIndex, tripType, stopsForLeg(offer, legIndex, tripType))}<div class="flight-leg__to"><div class="flight-leg__heading">&nbsp;</div><div class="flight-leg__time">${arrivalTime ? esc(arrivalTime) : "&nbsp;"}${arrivalTime && arrivalDate ? `<sup class="arrival-day">${esc(arrivalDate)}</sup>` : ""}</div><div class="flight-leg__place">${esc(originName(to))}</div></div></section>`;
}
function warningsFor(value) {
  if (Array.isArray(value)) return value.map(item => typeof item === "string" ? item : JSON.stringify(item)).filter(Boolean);
  if (value && typeof value === "object") return Array.isArray(value.warnings) ? value.warnings : [];
  return value ? [String(value)] : [];
}
function renderCard(offer, tripType, deepScanId = 0) {
  const legs = tripType === "RT" ? 2 : 1;
  const stopCounts = Array.from({length: legs}, (_, index) => stopsForLeg(offer, index, tripType));
  const transfers = transferGroups(offer, stopCounts, tripType);
  const rows = Array.from({length: legs}, (_, index) => legMarkup(offer, tripType, index, stopCounts, transfers[index] || [])).join("");
  const url = safeUrl(offer.source_url);
  const warnings = warningsFor(offer.warnings);
  const destinationId = Number(offer.destination_id) || 0;
  const offerOrigin = String(offer.origin || "").toUpperCase();
  const previousPrice = Number(offer.previous_price);
  const currentPrice = Number(offer.price);
  const priceDropPercent = previousPrice > currentPrice && previousPrice > 0
    ? (previousPrice - currentPrice) / previousPrice * 100
    : 0;
  const showPreviousPrice = Number.isFinite(priceDropPercent)
    && priceDropPercent >= PRICE_DROP_BADGE_PERCENT
    && (!offer.previous_currency || !offer.currency || offer.previous_currency === offer.currency);
  const previousPriceMarkup = showPreviousPrice
    ? `<del class="previous-price">${esc(formattedPrice({price:offer.previous_price,currency:offer.previous_currency || offer.currency}))}</del>`
    : "";
  const archiveButton = destinationId ? `<button class="card-archive-button" type="button" data-origin="${esc(offerOrigin)}" data-destination-id="${destinationId}" aria-label="Убрать ${esc(offer.destination || "направление")} в архив">В архив</button>` : "";
  const deepButton = deepScanId && offer.active && offer.destination ? `<button class="card-deep-button" type="button" data-scan-id="${deepScanId}" data-route-key="${esc(offer.route_group_key || "")}" data-destination="${esc(offer.destination)}" aria-label="Больше вариантов для направления ${esc(offer.destination)}">Больше вариантов</button>` : "";
  const tripCompareButton = Number(offer.offer_id) > 0 && Number.isFinite(currentPrice) && currentPrice > 0
    ? `<button class="trip-compare-button" type="button" data-offer-id="${Number(offer.offer_id)}" data-card-price="${currentPrice}" data-currency="${esc(offer.currency || "")}" aria-label="Подобрать тур для ${esc(offer.destination || "этого билета")}">Подобрать тур</button>`
    : "";
  const favoriteButton = destinationId ? `<button class="favorite-button${offer.active ? " is-active" : ""}" type="button" data-origin="${esc(offerOrigin)}" data-destination-id="${destinationId}" data-active="${offer.active ? "true" : "false"}" aria-pressed="${offer.active ? "true" : "false"}" aria-label="${offer.active ? "Отключить" : "Включить"} направление ${esc(offer.destination || "")}" title="${offer.active ? "Активное направление" : "Отслеживать направление"}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z"/></svg></button>` : "";
  return `<article class="flight-card${url ? " flight-card--linked" : ""}"${url ? ` tabindex="0" role="link" data-ticket-url="${esc(url)}" aria-label="Открыть билет ${esc(offer.destination || "")}"` : ""}><div class="flight-card__top"><div class="flight-card__route">${rows}</div><aside class="flight-card__side"><div class="flight-card__price-wrap">${previousPriceMarkup}<div class="flight-card__price">${esc(formattedPrice(offer))}</div></div><button class="price-history-button" type="button" data-origin="${esc(offerOrigin)}" data-offer-id="${Number(offer.offer_id) || 0}" aria-label="График цены" title="График цены"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V5M4 19h16M7 15l4-4 3 3 5-7"/></svg></button>${tripCompareButton}${favoriteButton}</aside></div>${warnings.length ? `<div class="warnings">${warnings.map(esc).join(" · ")}</div>` : ""}${archiveButton || deepButton ? `<div class="card-actions">${archiveButton}${deepButton}</div>` : ""}</article>`;
}
function hydrateLogos() {
  document.querySelectorAll(".airline-logo img").forEach(image => {
    image.addEventListener("error", () => { image.hidden = true; }, {once:true});
    image.src = image.dataset.src;
    if (image.complete && image.naturalWidth === 0) image.hidden = true;
  });
}
function sortedOffers(offers) {
  const sort = document.querySelector("#sort-order").value;
  if (sort === "default") return [...offers];
  return [...offers].sort((a, b) => {
    const left = sort === "price" ? Number(a.price) : sort === "date" ? Date.parse(`${a.departure_date || ""}T00:00:00Z`) : sort === "days" ? Number(a.trip_duration_days) : Number(a.offer_id);
    const right = sort === "price" ? Number(b.price) : sort === "date" ? Date.parse(`${b.departure_date || ""}T00:00:00Z`) : sort === "days" ? Number(b.trip_duration_days) : Number(b.offer_id);
    const leftValid = Number.isFinite(left) && left > 0;
    const rightValid = Number.isFinite(right) && right > 0;
    if (leftValid !== rightValid) return leftValid ? -1 : 1;
    return leftValid && rightValid ? left - right : 0;
  });
}
function deepScanStatus(scan) {
  const warnings = warningsFor(scan.warnings);
  if (!scan.partial) {
    return {label: "Не завершена", title: warnings.join(" · ") || "Deep-проверка не завершилась полностью."};
  }
  const progress = warnings
    .map(warning => warning.match(/deep scan completed partially:\s*(\d+)\/(\d+)\s*destinations/i))
    .find(Boolean);
  const reasons = warnings.filter(warning => !/deep scan completed partially:/i.test(warning));
  const label = progress ? `${progress[1]} из ${progress[2]}` : "Есть пропуски";
  const title = [
    progress ? `Проверено направлений: ${progress[1]} из ${progress[2]}.` : "Не все направления проверены.",
    ...reasons,
  ].join(" ");
  return {label, title};
}
function renderScans(data, target) {
  state.deepScans.clear();
  if (!data.scans.length) { target.innerHTML = '<div class="empty">Подходящих билетов пока нет.</div>'; return; }
  const regularByOrigin = new Map();
  const deepByOrigin = new Map();
  for (const scan of data.scans) {
    if (scan.scan_kind === "deep") {
      state.deepScans.set(Number(scan.scan_id), scan);
      const offers = new Map();
      for (const offer of scan.offers || []) {
        if (!offer.destination) continue;
        const key = offer.route_group_key || String(offer.destination).trim().toLocaleLowerCase("ru-RU");
        const current = offers.get(key);
        const price = Number(offer.price);
        const currentPrice = Number(current?.price);
        if (!current || (Number.isFinite(price) && (!Number.isFinite(currentPrice) || price < currentPrice))) offers.set(key, offer);
      }
      deepByOrigin.set(scan.origin, {scan, offers});
    } else {
      regularByOrigin.set(scan.origin, scan);
    }
  }
  const origins = new Set([...regularByOrigin.keys(), ...deepByOrigin.keys()]);
  const groups = [...origins].map(origin => {
    const regular = regularByOrigin.get(origin);
    const deep = deepByOrigin.get(origin);
    const mainScan = regular || deep.scan;
    // A deep scan is an additional snapshot, not a historical price feed.
    // Never let an older deep result replace a newer regular card with stale
    // dates or price (for example, yesterday's cheap Dubai fare).
    const deepIsCurrent = Boolean(deep)
      && (!regular || Date.parse(deep.scan.finished_at || "") >= Date.parse(regular.finished_at || ""));
    const available = new Map();
    for (const offer of regular?.offers || []) {
      if (!offer.destination) continue;
      const key = offer.route_group_key || String(offer.destination).trim().toLocaleLowerCase("ru-RU");
      const deepOffer = deep?.offers.get(key);
      const sameCurrencyDeepOffer = deepIsCurrent && deepOffer
        && (!offer.currency || !deepOffer.currency || offer.currency === deepOffer.currency);
      const regularPrice = Number(offer.price);
      const deepPrice = Number(deepOffer?.price);
      const displayedOffer = sameCurrencyDeepOffer
        && Number.isFinite(deepPrice)
        && (!Number.isFinite(regularPrice) || deepPrice < regularPrice)
        ? {...deepOffer, active:offer.active, destination_id:offer.destination_id ?? deepOffer.destination_id}
        : offer;
      available.set(key, {
        offer:displayedOffer,
        deepScanId: offer.active && deepOffer ? Number(deep.scan.scan_id) : 0,
      });
    }
    for (const [key, offer] of deep?.offers || []) {
      if (deepIsCurrent && offer.active && !available.has(key)) {
        available.set(key, {offer, deepScanId: Number(deep.scan.scan_id)});
      }
    }
    const timestampValue = [regular?.last_checked_at, deep?.scan.last_checked_at, regular?.finished_at, deep?.scan.finished_at].filter(Boolean).sort().at(-1) || mainScan.finished_at;
    const timestamp = new Date(timestampValue).toLocaleString("ru-RU", {dateStyle:"medium", timeStyle:"short"});
    const cards = sortedOffers([...available.values()].map(item => item.offer))
      .map(offer => {
        const key = offer.route_group_key || String(offer.destination || "").trim().toLocaleLowerCase("ru-RU");
        return renderCard(offer, regular?.trip_type || deep.scan.trip_type, available.get(key)?.deepScanId || 0);
      }).join("");
    const groupHeading = state.origin === "ALL" ? `<strong class="scan-origin">${esc(originHeading(origin))}</strong>` : "";
    return {origin, timestampValue, html:`<section class="scan-record"><div class="scan-record-head">${groupHeading}<time>Последняя проверка: ${esc(timestamp)}</time></div><div class="cards-grid">${cards || '<div class="empty">Подходящих билетов пока нет.</div>'}</div></section>`};
  }).sort((left, right) => right.timestampValue.localeCompare(left.timestampValue));
  target.innerHTML = groups.map(group => group.html).join("");
  hydrateLogos();
}
function renderResults(data) {
  document.querySelector("#result-count").textContent = data.checks_recorded
    ? `Всего проверок: ${new Intl.NumberFormat("ru-RU").format(data.checks_recorded)}`
    : "";
  renderScans(data, document.querySelector("#scan-list"));
  const pager = document.querySelector("#scan-pager");
  const pages = Math.ceil(data.total / PAGE_SIZE);
  pager.hidden = pages < 2;
  pager.querySelector("span").textContent = pages ? `${state.page + 1} / ${pages}` : "";
  pager.querySelector('[data-page="previous"]').disabled = state.page === 0;
  pager.querySelector('[data-page="next"]').disabled = (state.page + 1) * PAGE_SIZE >= data.total;
}
async function loadResults(page = state.page, automatic = false) {
  state.page = Math.max(0, page);
  const requestId = ++resultsRequestId;
  resultsController?.abort();
  const controller = new AbortController();
  resultsController = controller;
  const list = document.querySelector("#scan-list");
  if (!automatic) list.innerHTML = '<div class="empty">Загружаем билеты…</div>';
  const query = new URLSearchParams({origin:state.origin, kind:"all", view:"current", q:state.query, active_only:String(state.activeOnly), limit:String(PAGE_SIZE), offset:String(state.page * PAGE_SIZE)});
  try {
    const response = await fetch(`/api/scans?${query}`, {cache:"no-store", signal:controller.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (requestId !== resultsRequestId) return;
    renderResults(data);
    document.querySelector("#refresh-state").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}`;
  } catch (error) {
    if (error.name === "AbortError" || requestId !== resultsRequestId) return;
    if (!automatic) list.innerHTML = '<div class="error">Не удалось загрузить результаты. Проверьте соединение с локальной базой.</div>';
    document.querySelector("#refresh-state").textContent = "Нет связи";
  } finally {
    if (requestId === resultsRequestId) resultsController = null;
  }
}
async function loadDestinations() {
  const requestId = ++destinationsRequestId;
  const origin = state.origin;
  try {
    const response = await fetch(`/api/destinations?origin=${origin}`, {cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (requestId !== destinationsRequestId || origin !== state.origin) return;
    const target = document.querySelector("#destination-list");
    target.innerHTML = data.destinations.length ? data.destinations.map(item => `<button class="destination-chip${item.active ? " active" : ""}" type="button" data-name="${esc(item.destination)}"><span><strong>${esc(item.destination)}</strong>${origin === "ALL" ? `<small>из ${esc(originLabel(item.origin))}</small>` : ""}</span><b>${item.last_known_price == null ? "" : `${new Intl.NumberFormat("ru-RU").format(item.last_known_price)} ${item.last_known_currency === "RUB" ? "₽" : esc(item.last_known_currency || "")}`}</b></button>`).join("") : "<p>Пока нет сохранённых направлений.</p>";
    target.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
      document.querySelector("#destination-search").value = button.dataset.name;
      state.query = button.dataset.name;
      state.page = 0;
      document.querySelector("#current-destinations").open = false;
      loadResults(0);
    }));
  } catch {
    if (requestId === destinationsRequestId && origin === state.origin) document.querySelector("#destination-list").innerHTML = "<p>Не удалось загрузить направления.</p>";
  }
}
async function loadNotifications() {
  const requestId = ++notificationsRequestId;
  const origin = state.origin;
  try {
    const response = await fetch(`/api/notifications?origin=${origin}`, {cache:"no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (requestId !== notificationsRequestId || origin !== state.origin) return;
    const items = data.items || [];
    const unread = items.filter(item => Number(item.id) > notificationReadId(item.origin));
    const badge = document.querySelector("#notification-count");
    badge.textContent = unread.length ? String(unread.length) : "";
    badge.hidden = unread.length === 0;
    document.querySelector("#notifications").classList.toggle("has-unread", unread.length > 0);
    document.querySelector("#notification-list").innerHTML = items.length ? items.map(item => {
      const time = new Date(item.finished_at).toLocaleString("ru-RU", {dateStyle:"medium", timeStyle:"short"});
      const heading = item.scan_kind === "deep" ? "Глубокая проверка" : "Проверка цен";
      const newNames = (item.new_destinations || []).map(esc);
      const returnedNames = (item.returned_destinations || []).map(esc);
      const names = [
        ...(newNames.length ? [`Новые направления: ${newNames.join(", ")}`] : []),
        ...(returnedNames.length ? [`Вернулись: ${returnedNames.join(", ")}`] : []),
      ].join(" · ") || "Новых или вернувшихся направлений нет";
      const stale = Number(item.outdated_offers || 0);
      const disappeared = item.disappeared_destinations?.length
        ? `<span>Пропали с горящих: <del>${item.disappeared_destinations.map(esc).join(", ")}</del></span>`
        : "";
      const isNew = Number(item.id) > notificationReadId(item.origin);
      const progress = item.deep_progress && Number.isFinite(Number(item.deep_progress.scanned))
        ? ` · проверено ${Number(item.deep_progress.scanned)} из ${Number(item.deep_progress.requested)}`
        : item.partial ? " · есть пропуски" : "";
      const issues = Array.isArray(item.deep_issues) && item.deep_issues.length
        ? `<span>Пропуски: ${item.deep_issues.map(issue => `${esc(issue.destination)} — ${esc(issue.reason)}`).join(", ")}</span>`
        : "";
      return `<article class="notification-item${isNew ? " notification-item--new" : ""}" data-id="${Number(item.id) || 0}" data-origin="${esc(item.origin)}"><time>${esc(time)}</time><strong>${heading} · ${esc(originLabel(item.origin))}${progress}</strong><span>${esc(names)}</span>${issues}${disappeared}<span>Устаревших предложений убрано: ${stale}</span></article>`;
    }).join("") : "<p>Пока нет событий.</p>";
  } catch {
    if (requestId === notificationsRequestId && origin === state.origin) document.querySelector("#notification-list").innerHTML = "<p>Не удалось загрузить уведомления.</p>";
  }
}
async function loadOriginSettings() {
  const response = await fetch("/api/settings", {cache:"no-store"});
  if (!response.ok) throw new Error("Настройки origins недоступны");
  const data = await response.json();
  const origins = data.origins || [];
  for (const item of origins) state.originLabels[item.origin] = item.city_name || item.origin;
  const codes = new Set(["MOW", "LED", ...origins.map(item => item.origin)]);
  document.querySelector(".origin-switch").innerHTML = [`<button class="origin-tab${state.origin === "ALL" ? " selected" : ""}" type="button" role="tab" aria-selected="${state.origin === "ALL"}" data-origin="ALL"><span>Все вылеты</span></button>`, ...[...codes].map(code => {
    const selected = code === state.origin;
    return `<button class="origin-tab${selected ? " selected" : ""}" type="button" role="tab" aria-selected="${selected}" data-origin="${esc(code)}"><span>${esc(originLabel(code))}</span><b>${esc(code)}</b></button>`;
  })].join("");
}
function selectOrigin(origin) {
  state.origin = origin; state.page = 0; state.deepScans.clear();
  document.querySelectorAll(".origin-tab").forEach(button => {
    const selected = button.dataset.origin === origin;
    button.classList.toggle("selected", selected); button.setAttribute("aria-selected", String(selected));
  });
  document.querySelector("#results-title").textContent = origin === "ALL" ? originLabel(origin) : originHeading(origin);
  document.querySelector("#deep-scan-button").hidden = origin === "ALL";
  loadResults(0); loadDestinations(); loadNotifications();
}
document.querySelector(".origin-switch").addEventListener("click", event => {
  const button = event.target.closest(".origin-tab");
  if (button) selectOrigin(button.dataset.origin);
});
document.querySelector("#sort-order").addEventListener("change", () => loadResults(state.page));
document.querySelector("#active-only").addEventListener("change", event => { state.activeOnly = event.target.checked; loadResults(0); });
document.querySelector("#destination-search").addEventListener("input", event => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.query = event.target.value.trim(); loadResults(0); }, 120);
});
document.querySelector("#current-destinations").addEventListener("toggle", event => {
  if (event.currentTarget.open) document.querySelector("#notifications").open = false;
});
document.querySelector("#notifications").addEventListener("toggle", event => {
  if (event.currentTarget.open) {
    document.querySelector("#current-destinations").open = false;
    const latestByOrigin = new Map();
    for (const item of document.querySelectorAll(".notification-item[data-id][data-origin]")) {
      const origin = item.dataset.origin;
      const id = Number(item.dataset.id) || 0;
      latestByOrigin.set(origin, Math.max(latestByOrigin.get(origin) || 0, id));
    }
    for (const [origin, latest] of latestByOrigin) {
      localStorage.setItem(notificationReadKey(origin), String(latest));
    }
    if (state.origin === "ALL" && latestByOrigin.size) {
      localStorage.setItem(notificationReadKey("ALL"), String(Math.max(...latestByOrigin.values())));
    }
    document.querySelectorAll(".notification-item--new").forEach(item => item.classList.remove("notification-item--new"));
    const badge = document.querySelector("#notification-count");
    badge.textContent = ""; badge.hidden = true;
    event.currentTarget.classList.remove("has-unread");
  }
});
document.addEventListener("click", event => {
  const notifications = document.querySelector("#notifications");
  const destinations = document.querySelector("#current-destinations");
  if (!notifications.contains(event.target)) notifications.open = false;
  if (!destinations.contains(event.target)) destinations.open = false;
});
document.querySelector("#scan-list").addEventListener("click", event => {
  const tripCompareButton = event.target.closest(".trip-compare-button");
  if (tripCompareButton) { event.preventDefault(); event.stopPropagation(); openTripComparison(tripCompareButton); return; }
  const deepButton = event.target.closest(".card-deep-button");
  if (deepButton) { event.stopPropagation(); openDeepScan(Number(deepButton.dataset.scanId), deepButton.dataset.destination, deepButton.dataset.routeKey); return; }
  const archiveButton = event.target.closest(".card-archive-button");
  if (archiveButton) { event.stopPropagation(); setArchived(Number(archiveButton.dataset.destinationId), true, archiveButton, archiveButton.dataset.origin); return; }
  const favoriteButton = event.target.closest(".favorite-button");
  if (favoriteButton) { event.stopPropagation(); setDestinationActive(favoriteButton); return; }
  const historyButton = event.target.closest(".price-history-button");
  if (historyButton) { event.stopPropagation(); openPriceHistory(Number(historyButton.dataset.offerId), historyButton, historyButton.dataset.origin); return; }
  const card = event.target.closest(".flight-card[data-ticket-url]");
  if (card) window.open(card.dataset.ticketUrl, "_blank", "noopener,noreferrer");
});
document.querySelector("#scan-list").addEventListener("keydown", event => {
  if ((event.key === "Enter" || event.key === " ") && event.target.matches(".flight-card[data-ticket-url]")) {
    event.preventDefault(); window.open(event.target.dataset.ticketUrl, "_blank", "noopener,noreferrer");
  }
});
document.querySelector("#scan-pager").addEventListener("click", event => {
  const direction = event.target.closest("button")?.dataset.page;
  if (direction) loadResults(state.page + (direction === "next" ? 1 : -1));
});
document.querySelector("#deep-detail-content").addEventListener("click", event => {
  const tripCompareButton = event.target.closest(".trip-compare-button");
  if (tripCompareButton) { event.preventDefault(); event.stopPropagation(); openTripComparison(tripCompareButton); return; }
  const archiveButton = event.target.closest(".card-archive-button");
  if (archiveButton) { event.stopPropagation(); setArchived(Number(archiveButton.dataset.destinationId), true, archiveButton, archiveButton.dataset.origin); return; }
  const favoriteButton = event.target.closest(".favorite-button");
  if (favoriteButton) { event.stopPropagation(); setDestinationActive(favoriteButton); return; }
  const historyButton = event.target.closest(".price-history-button");
  if (historyButton) { event.stopPropagation(); openPriceHistory(Number(historyButton.dataset.offerId), historyButton, historyButton.dataset.origin); return; }
  const card = event.target.closest(".flight-card[data-ticket-url]");
  if (card) window.open(card.dataset.ticketUrl, "_blank", "noopener,noreferrer");
});

function tripComparePrice(price, currency = "RUB") {
  const amount = Number(price);
  if (!Number.isFinite(amount)) return "—";
  return `${new Intl.NumberFormat("ru-RU", {maximumFractionDigits:0}).format(amount)} ${currency === "RUB" ? "₽" : esc(currency)}`;
}
function tripCompareLeg(leg) {
  if (!leg) return "";
  const route = Array.isArray(leg.airport_chain) ? leg.airport_chain.join(" → ") : "";
  const time = [leg.departure_time, leg.arrival_time].filter(Boolean).join("–");
  const stopCities = Array.isArray(leg.stopover_cities) ? leg.stopover_cities.filter(Boolean) : [];
  const stops = Number.isInteger(leg.stops)
    ? (leg.stops ? `${leg.stops} перес.${stopCities.length ? ` (${stopCities.join(", ")})` : ""}` : "без пересадок")
    : "";
  return [leg.departure_date, time, route, stops, leg.airline].filter(Boolean).map(esc).join(" · ");
}
function tripCompareState({offerId = null, cardPrice, cardCurrency, tripPrice = null, tripCurrency = "RUB", trip = null, elapsed = null, error = null, loading = false}) {
  const content = document.querySelector("#trip-compare-content");
  if (offerId != null) content.dataset.renderOfferId = String(offerId);
  let comparison = "";
  if (error) comparison = `<p class="trip-compare-error">${esc(error)}</p><button class="trip-compare-retry" id="trip-compare-retry" type="button">Повторить</button>`;
  else if (loading) comparison = `<p class="trip-compare-wait">⏳ Ищем подходящий билет на Trip.com…</p>`;
  else {
    const difference = Number(tripPrice) - Number(cardPrice);
    if (difference < 0) comparison = `<p class="trip-compare-difference trip-compare-difference--cheaper">💰 На Trip.com дешевле на ${tripComparePrice(Math.abs(difference), tripCurrency)}</p>`;
    else if (difference > 0) comparison = `<p class="trip-compare-difference trip-compare-difference--pricier">На Trip.com дороже на ${tripComparePrice(difference, tripCurrency)}</p>`;
    else comparison = `<p class="trip-compare-difference">Цена одинаковая</p>`;
  }
  const legs = trip ? [tripCompareLeg(trip.outbound), tripCompareLeg(trip.return)].filter(Boolean) : [];
  const tripcomUrl = !loading && !error ? safeTripcomFlightUrl(trip?.tripcom_url) : null;
  const tripPriceLabel = error ? "Не удалось получить цену" : loading ? "⏳ ищем…" : tripComparePrice(tripPrice, tripCurrency);
  const tripcomLink = tripcomUrl ? `<a class="trip-compare-link" href="${esc(tripcomUrl)}" target="_blank" rel="noopener noreferrer">Открыть найденный тариф на Trip.com ↗</a>` : "";
  content.innerHTML = `<button class="dialog-close" type="button" aria-label="Закрыть">×</button><h2 id="trip-compare-title">Сравнение цен</h2><p class="trip-compare-route">${esc(originLabel(content.dataset.origin || ""))} → ${esc(content.dataset.destination || "")}</p><p class="trip-compare-search">Все аэропорты · 1 взрослый · эконом</p><div class="trip-compare-prices"><section><h3>🔥 Горящие билеты</h3><strong>${tripComparePrice(cardPrice, cardCurrency)}</strong></section><section><h3>✈️ Самый дешёвый подходящий тариф Trip.com</h3><strong>${tripPriceLabel}</strong></section></div>${legs.length ? `<div class="trip-compare-legs">${legs.map(line => `<p>${line}</p>`).join("")}</div>` : ""}${comparison}${tripcomLink}${!loading && !error && Number.isFinite(Number(elapsed)) ? `<p class="trip-compare-latency">Поиск Trip.com: ${esc(elapsed)} сек.</p>` : ""}`;
  content.querySelector(".dialog-close").addEventListener("click", () => document.querySelector("#trip-compare-dialog").close());
  const retry = content.querySelector("#trip-compare-retry");
  if (retry) retry.addEventListener("click", () => {
    const offerId = Number(content.dataset.offerId);
    const savedCardPrice = Number(content.dataset.cardPrice);
    const savedCurrency = content.dataset.cardCurrency || "RUB";
    if (offerId > 0 && Number.isFinite(savedCardPrice) && savedCardPrice > 0) {
      requestTripComparison(offerId, savedCardPrice, savedCurrency);
    }
  });
}
function requestTripComparison(offerId, cardPrice, cardCurrency) {
  const dialog = document.querySelector("#trip-compare-dialog");
  const content = document.querySelector("#trip-compare-content");
  content.dataset.offerId = String(offerId);
  content.dataset.cardPrice = String(cardPrice);
  content.dataset.cardCurrency = cardCurrency || "RUB";
  const requestId = ++tripCompareRequestId;
  const requestOfferId = String(offerId);
  tripCompareActiveOfferId = requestOfferId;
  tripCompareStates.set(requestOfferId, {cardPrice, cardCurrency, loading:true});
  tripCompareState({offerId, cardPrice, cardCurrency, loading:true});
  fetch("/api/flights/compare-trip", {method:"POST", cache:"no-store", headers:{"Content-Type":"application/json"}, body:JSON.stringify({offer_id:offerId})})
    .then(async response => {
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "Не удалось получить цену Trip.com.");
      return result;
    })
    .then(result => {
      if (requestId !== tripCompareRequestId || tripCompareActiveOfferId !== requestOfferId
          || content.dataset.offerId !== requestOfferId) return;
      const trip = result.trip || {};
      content.dataset.origin = result.origin || content.dataset.origin || "";
      content.dataset.destination = result.destination || content.dataset.destination || "";
      tripCompareStates.set(requestOfferId, {cardPrice:result.card_price, cardCurrency:result.card_currency,
        tripPrice:trip.price, tripCurrency:trip.currency, trip, elapsed:trip.elapsed_seconds});
      tripCompareState({offerId, cardPrice:result.card_price, cardCurrency:result.card_currency, tripPrice:trip.price,
        tripCurrency:trip.currency, trip, elapsed:trip.elapsed_seconds});
    })
    .catch(error => {
      if (requestId !== tripCompareRequestId || tripCompareActiveOfferId !== requestOfferId
          || content.dataset.offerId !== requestOfferId) return;
      const message = error.message || "Не удалось получить цену Trip.com.";
      tripCompareStates.set(requestOfferId, {cardPrice, cardCurrency, error:message});
      tripCompareState({offerId, cardPrice, cardCurrency, error:message});
    });
  if (!dialog.open) dialog.showModal();
}
function openTripComparison(button) {
  const offerId = Number(button.dataset.offerId) || 0;
  const cardPrice = Number(button.dataset.cardPrice);
  const cardCurrency = button.dataset.currency || "RUB";
  if (!offerId || !Number.isFinite(cardPrice) || cardPrice <= 0) return;
  const content = document.querySelector("#trip-compare-content");
  const card = button.closest(".flight-card");
  const route = card?.querySelector(".flight-leg__from .flight-leg__place")?.textContent?.trim() || "";
  const destination = card?.querySelectorAll(".flight-leg__to .flight-leg__place");
  content.dataset.origin = route;
  content.dataset.destination = destination?.[0]?.textContent?.trim() || "";
  requestTripComparison(offerId, cardPrice, cardCurrency);
}

async function setArchived(destinationId, archived, button, origin = state.origin) {
  if (!destinationId) return;
  if (!/^[A-Z]{3}$/.test(origin || "")) { button.title = "Не удалось определить город вылета."; return; }
  button.disabled = true;
  const deepDialog = document.querySelector("#deep-history-dialog");
  const modalScan = archived && deepDialog.open ? state.deepModalScan : null;
  try {
    const response = await fetch(`/api/destinations/archive?origin=${origin}&destination_id=${destinationId}`, {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({archived}),
    });
    if (!response.ok) throw new Error("Не удалось изменить архив направления.");
    if (modalScan) modalScan.offers = (modalScan.offers || []).filter(offer => Number(offer.destination_id) !== destinationId);
    await loadResults(0, true); await loadDestinations();
    if (document.querySelector("#archive-dialog").open) loadArchive();
    if (deepDialog.open && modalScan) {
      renderDeepCards();
    }
  } catch (error) { button.disabled = false; button.title = error.message; }
}

async function setDestinationActive(button) {
  const destinationId = Number(button.dataset.destinationId);
  const origin = button.dataset.origin;
  const active = button.dataset.active !== "true";
  if (!destinationId || !/^[A-Z]{3}$/.test(origin || "")) return;
  button.disabled = true;
  try {
    const response = await fetch(`/api/destinations/active?origin=${origin}&destination_id=${destinationId}`, {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({active}),
    });
    if (!response.ok) throw new Error("Не удалось изменить активность направления.");
    for (const scan of state.deepScans.values()) {
      for (const offer of scan.offers || []) {
        if (Number(offer.destination_id) === destinationId && offer.origin === origin) offer.active = active;
      }
    }
    if (state.deepModalScan) renderDeepCards();
    await loadResults(0, true);
    await loadDestinations();
  } catch (error) {
    button.disabled = false;
    button.title = error.message;
  }
}

function openDeepScan(scanId, destination, routeKey) {
  const scan = state.deepScans.get(scanId);
  if (!scan) return;
  state.deepModalScan = scan;
  state.deepDestination = String(destination || "");
  state.deepRouteKey = routeKey || "";
  document.querySelector("#deep-sort-order").value = "price";
  const date = new Date(scan.finished_at).toLocaleString("ru-RU", {dateStyle:"medium", timeStyle:"short"});
  document.querySelector("#deep-history-title").textContent = `${state.deepDestination} · ${date} · ${scan.trip_type}`;
  renderDeepCards();
  document.querySelector("#deep-history-dialog").showModal();
}
function renderDeepCards() {
  const scan = state.deepModalScan;
  if (!scan) return;
  const key = state.deepDestination.trim().toLocaleLowerCase("ru-RU");
  const sort = document.querySelector("#deep-sort-order").value;
  const offers = (scan.offers || [])
    .filter(offer => state.deepRouteKey ? offer.route_group_key === state.deepRouteKey : String(offer.destination || "").trim().toLocaleLowerCase("ru-RU") === key)
    .sort((left, right) => {
      const leftPrice = Number(left.price), rightPrice = Number(right.price);
      const leftDays = Number(left.trip_duration_days), rightDays = Number(right.trip_duration_days);
      const leftValue = sort === "duration" ? leftDays : leftPrice;
      const rightValue = sort === "duration" ? rightDays : rightPrice;
      const leftValid = Number.isFinite(leftValue) && leftValue > 0;
      const rightValid = Number.isFinite(rightValue) && rightValue > 0;
      if (leftValid !== rightValid) return leftValid ? -1 : 1;
      if (leftValid && leftValue !== rightValue) return leftValue - rightValue;
      if (sort === "duration" && Number.isFinite(leftPrice) && Number.isFinite(rightPrice)) {
        return leftPrice - rightPrice;
      }
      return 0;
    });
  const target = document.querySelector("#deep-detail-content");
  target.innerHTML = offers.length
    ? `<div class="deep-detail-cards">${offers.map(offer => renderCard(offer, scan.trip_type)).join("")}</div>`
    : '<div class="empty">В этой проверке билетов не найдено.</div>';
  hydrateLogos();
}
document.querySelector("#deep-sort-order").addEventListener("change", renderDeepCards);

async function loadArchive() {
  const target = document.querySelector("#archive-list");
  target.innerHTML = '<p class="empty">Загружаем архив…</p>';
  try {
    const response = await fetch(`/api/destinations?origin=${state.origin}&archived=true`, {cache:"no-store"});
    if (!response.ok) throw new Error();
    const data = await response.json();
    target.innerHTML = data.destinations.length ? data.destinations.map(item => `<article class="archive-item"><div><strong>${esc(item.destination)}</strong><span>${state.origin === "ALL" ? `${esc(originLabel(item.origin))} · ` : ""}${item.last_known_price == null ? "Цена не сохранена" : `${new Intl.NumberFormat("ru-RU").format(item.last_known_price)} ${item.last_known_currency === "RUB" ? "₽" : esc(item.last_known_currency || "")}`}</span></div><button type="button" data-origin="${esc(item.origin)}" data-destination-id="${Number(item.destination_id)}">Вернуть</button></article>`).join("") : '<p class="empty">Архив пуст.</p>';
  } catch { target.innerHTML = '<p class="error">Не удалось загрузить архив.</p>'; }
}
document.querySelector("#archive-open-button").addEventListener("click", async () => {
  const dialog = document.querySelector("#archive-dialog"); dialog.showModal(); await loadArchive();
});
document.querySelector("#archive-list").addEventListener("click", event => {
  const button = event.target.closest("button[data-destination-id]");
  if (button) setArchived(Number(button.dataset.destinationId), false, button, button.dataset.origin);
});
document.querySelectorAll(".list-dialog .dialog-close").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); }));
document.querySelector("#deep-history-dialog").addEventListener("close", () => { state.deepModalScan = null; state.deepDestination = ""; });
async function openPriceHistory(offerId, button, origin = state.origin) {
  if (!offerId) return;
  button.disabled = true;
  button.classList.add("loading");
  const dialog = document.querySelector("#price-history-dialog");
  const body = dialog.querySelector(".price-history-content");
  body.innerHTML = '<p class="empty">Загружаем историю цены…</p>';
  dialog.showModal();
  try {
    const response = await fetch(`/api/price-history?origin=${origin}&offer_id=${offerId}`, {cache:"no-store"});
    if (!response.ok) throw new Error("История цены недоступна.");
    const data = await response.json();
    body.innerHTML = priceChartMarkup(data);
  } catch { body.innerHTML = '<p class="error">Не удалось загрузить историю цены.</p>'; }
  finally { button.disabled = false; button.classList.remove("loading"); }
}
function priceChartMarkup(data) {
  const points = data.points || [];
  if (!points.length) return `<h2>${esc(data.destination || "История цены")}</h2><p class="empty">Истории проверок для этого направления пока нет.</p>`;
  const width = 680, height = 300, left = 82, right = 20, top = 20, bottom = 42;
  const prices = points.map(point => Number(point.price)).filter(Number.isFinite);
  if (!prices.length) return `<h2>${esc(data.destination || "История цены")}</h2><p class="empty">Истории проверок для этого направления пока нет.</p>`;
  const dataMin = Math.min(...prices), dataMax = Math.max(...prices);
  const paddedRange = Math.max(dataMax - dataMin, Math.abs(dataMax) * .05, 1);
  const rawStep = paddedRange / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const normalizedStep = rawStep / magnitude;
  const step = (normalizedStep <= 1 ? 1 : normalizedStep <= 2 ? 2 : normalizedStep <= 5 ? 5 : 10) * magnitude;
  const min = Math.floor((dataMin - paddedRange * .1) / step) * step;
  const max = Math.ceil((dataMax + paddedRange * .1) / step) * step;
  const plotHeight = height - top - bottom, plotWidth = width - left - right;
  const ticks = [];
  for (let value = min, guard = 0; value <= max + step * .01 && guard < 12; value += step, guard++) ticks.push(value);
  const grid = ticks.map(value => {
    const y = top + (max - value) / (max - min) * plotHeight;
    const label = new Intl.NumberFormat("ru-RU", {maximumFractionDigits:0}).format(value);
    return `<line class="chart-gridline" x1="${left}" y1="${y}" x2="${width-right}" y2="${y}"/><text class="chart-y-label" x="${left-10}" y="${y+4}" text-anchor="end">${esc(label)}</text>`;
  }).join("");
  const coords = points.map((point, index) => ({...point, x:left + (points.length === 1 ? plotWidth / 2 : index * plotWidth / (points.length - 1)), y:top + (max - Number(point.price)) / (max - min) * plotHeight}));
  const line = coords.map(point => `${point.x},${point.y}`).join(" ");
  const dots = coords.map(point => `<circle cx="${point.x}" cy="${point.y}" r="5" tabindex="0"><title>${esc(new Intl.NumberFormat("ru-RU").format(point.price))} ${data.currency === "RUB" ? "₽" : esc(data.currency || "")} · ${esc(new Date(point.checked_at).toLocaleString("ru-RU", {dateStyle:"medium", timeStyle:"short"}))}</title></circle>`).join("");
  const newest = coords.at(-1);
  const axis = `<line class="chart-axis" x1="${left}" y1="${top}" x2="${left}" y2="${height-bottom}"/><line class="chart-axis" x1="${left}" y1="${height-bottom}" x2="${width-right}" y2="${height-bottom}"/>`;
  const xLabels = points.length > 1 ? `<text class="chart-x-label" x="${left}" y="${height-12}">${esc(new Date(points[0].checked_at).toLocaleDateString("ru-RU"))}</text><text class="chart-x-label" x="${width-right}" y="${height-12}" text-anchor="end">${esc(new Date(newest.checked_at).toLocaleDateString("ru-RU"))}</text>` : "";
  const currency = data.currency === "RUB" ? "₽" : esc(data.currency || "");
  return `<h2 id="price-history-title">${esc(data.destination || "История цены")}</h2><p>Последняя цена: <strong>${esc(new Intl.NumberFormat("ru-RU").format(newest.price))} ${currency}</strong></p><svg class="price-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="График изменения цены"><g>${grid}${axis}${xLabels}</g><polyline points="${line}"/>${dots}</svg><p class="chart-hint">Показана минимальная цена направления в каждом скане; даты вылета могут различаться. Наведите на точку, чтобы увидеть цену и время проверки.</p>`;
}
document.querySelector("#price-history-dialog .dialog-close").addEventListener("click", () => document.querySelector("#price-history-dialog").close());
document.querySelector("#price-history-dialog").addEventListener("click", event => { if (event.target === event.currentTarget) event.currentTarget.close(); });
document.addEventListener("keydown", event => {
  if (event.key === "Escape") {
    document.querySelector("#current-destinations").open = false;
    document.querySelector("#notifications").open = false;
    document.querySelector("#price-history-dialog").close();
    document.querySelector("#trip-compare-dialog").close();
  }
});
document.querySelector("#trip-compare-dialog").addEventListener("click", event => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
document.querySelector("#add-origin-button").addEventListener("click", () => {
  const dialog = document.querySelector("#origin-dialog");
  document.querySelector("#origin-form-error").hidden = true;
  dialog.showModal();
  document.querySelector("#origin-city-input").focus();
});
document.querySelector("#origin-dialog .dialog-close").addEventListener("click", () => document.querySelector("#origin-dialog").close());
document.querySelector("#origin-dialog").addEventListener("click", event => { if (event.target === event.currentTarget) event.currentTarget.close(); });
document.querySelectorAll("input[name='origin-trip-type']").forEach(input => input.addEventListener("change", () => {
  document.querySelector("#origin-duration-fields").hidden = document.querySelector("input[name='origin-trip-type']:checked").value !== "RT";
}));
document.querySelector("#origin-city-input").addEventListener("input", event => {
  clearTimeout(citySearchTimer);
  document.querySelector("#origin-code").value = "";
  document.querySelector("#city-selection-hint").hidden = true;
  const query = event.target.value.trim();
  const list = document.querySelector("#city-suggestions");
  if (query.length < 2) { list.hidden = true; list.replaceChildren(); return; }
  citySearchTimer = setTimeout(async () => {
    try {
      const response = await fetch(`/api/cities?q=${encodeURIComponent(query)}`, {cache:"no-store"});
      if (!response.ok) throw new Error();
      const cities = (await response.json()).cities || [];
      list.innerHTML = cities.length ? cities.map((city,index) => `<button type="button" role="option" data-index="${index}"><span>${esc(city.name)}${city.country ? ` · ${esc(city.country)}` : ""}</span><b>${esc(city.code)}</b></button>`).join("") : '<p>Город не найден.</p>';
      list.hidden = false;
      list.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
        const city = cities[Number(button.dataset.index)];
        document.querySelector("#origin-city-input").value = city.name;
        document.querySelector("#origin-code").value = city.code;
        const hint = document.querySelector("#city-selection-hint");
        hint.textContent = `${city.name} · ${city.code}`; hint.hidden = false;
        list.hidden = true;
      }));
    } catch { list.innerHTML = '<p>Не удалось проверить город. Попробуйте ещё раз.</p>'; list.hidden = false; }
  }, 180);
});
document.querySelector("#origin-form").addEventListener("submit", async event => {
  event.preventDefault();
  const error = document.querySelector("#origin-form-error");
  const code = document.querySelector("#origin-code").value;
  if (!code) { error.textContent = "Выберите город из списка, чтобы проверить его код."; error.hidden = false; return; }
  const button = document.querySelector("#save-origin-button");
  button.disabled = true; button.textContent = "Сохраняю настройки…";
  const tripType = document.querySelector("input[name='origin-trip-type']:checked").value;
  const payload = {
    origin: code,
    city_name: document.querySelector("#origin-city-input").value,
    trip_type: tripType,
    interval_minutes: Number(document.querySelector("#origin-interval").value),
    duration_min_days: Number(document.querySelector("#origin-duration-min").value),
    duration_max_days: Number(document.querySelector("#origin-duration-max").value),
  };
  try {
    const response = await fetch("/api/settings", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Не удалось сохранить настройки.");
    await loadOriginSettings();
    document.querySelector("#origin-dialog").close();
    selectOrigin(code);
  } catch (saveError) { error.textContent = saveError.message; error.hidden = false; }
  finally { button.disabled = false; button.textContent = "Сохранить и начать мониторинг"; }
});
document.querySelector("#deep-scan-button").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true; button.textContent = "Сканирование запущено…";
  try {
    const response = await fetch(`/api/deep-scan?origin=${state.origin}`, {method:"POST"});
    if (!response.ok) throw new Error(response.status === 409 ? "Проверка уже идёт или нет активных направлений." : "Не удалось запустить глубокий скан.");
    button.textContent = "Глубокий скан запущен";
    setTimeout(() => { loadResults(0); loadNotifications(); }, 1500);
  } catch (error) { button.textContent = error.message; }
  setTimeout(() => { button.disabled = false; button.textContent = "Начать глубокий скан"; }, 4000);
});
loadOriginSettings().then(() => {
  document.querySelector("#results-title").textContent = state.origin === "ALL" ? originLabel(state.origin) : originHeading(state.origin);
}).catch(() => {});
loadResults(0); loadDestinations(); loadNotifications();
setInterval(() => {
  if (document.visibilityState === "visible") {
    if (state.page === 0) loadResults(0, true);
    loadDestinations(); loadNotifications();
  }
}, REFRESH_MS);
