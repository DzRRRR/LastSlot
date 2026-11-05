/*
 * Client Last Slot : carte MapLibre + liste des créneaux libérés, mise à jour en temps réel (SSE).
 * Module ES (strict par défaut), exécuté après maplibre-gl.js (scripts différés, dans l'ordre).
 * Anti-XSS : aucune donnée venant du serveur ne passe par innerHTML ; tout le DOM est construit
 * avec createElement / textContent (helper el()). Popups MapLibre : setDOMContent, jamais setHTML.
 */
import {
  CATEGORY_COLORS, FALLBACK_COLOR, api, colorOf, dayLabel, debounce, distanceLabel, durationLabel,
  el, euros, fmtTime, setLive, shortWhen, startsInLabel, toast,
} from './common.js';

const PRICE_SLIDER_MAX = 150; // position max du slider = pas de limite de prix
const MOVE_DEBOUNCE_MS = 300;
const SSE_DEBOUNCE_MS = 250;
const FOCUS_ZOOM = 15.5; // tuiles de zoom 15 > clusterMaxZoom (14) : le commerce visé n'est plus regroupé
const REFRESH_EVERY_MS = 60000; // libellés « dans X min » et statuts à jour même sans événement

const state = {
  config: null,
  categories: new Map(), // code -> {code, label, family}
  selected: new Set(), // codes cochés ; vide = toutes les catégories
  maxPrice: null, // euros, null = pas de limite
  withinHours: 48,
  userPos: null, // {lat, lng}
  slots: [],
  merchants: new Map(), // id -> {merchant, slots}
  map: null,
  mapReady: false,
  popup: null,
  popupMerchantId: null,
  userMarker: null,
  fetchController: null,
};

/* ---------- Rendu ---------- */

function slotSummary(slot, { withMerchant }) {
  const start = new Date(slot.starts_at);
  const soon = startsInLabel(start);
  const isEvent = state.categories.get(slot.merchant.category)?.family === 'event';
  const distance = distanceLabel(slot.distance_m);
  return el('span', { class: 'slot-summary' },
    el('span', { class: 'slot-when' },
      el('strong', {}, `${dayLabel(start)} ${fmtTime.format(start)}`),
      soon && el('span', { class: 'soon' }, soon)),
    el('span', { class: 'slot-what' }, `${slot.service_name} · ${durationLabel(slot)}`),
    withMerchant && el('span', { class: 'slot-merchant' },
      el('span', { class: 'dot', style: { background: colorOf(slot.merchant.category) } }),
      slot.merchant.name,
      distance && ` · ${distance}`),
    el('span', { class: 'slot-price' },
      el('s', { class: 'base' }, euros(slot.base_price_cents)),
      el('strong', { class: 'final' }, euros(slot.price_cents)),
      el('span', { class: 'badge' }, `-${slot.discount_pct} %`),
      isEvent && el('span', { class: 'places' },
        `${slot.remaining} place${slot.remaining > 1 ? 's' : ''} restante${slot.remaining > 1 ? 's' : ''}`)),
    // Remise progressive : prochain palier annoncé (l'argument central de l'executive summary).
    slot.next_discount_at && el('span', { class: 'next-tier' },
      `Puis -${slot.next_discount_pct}\u00a0% dès ${shortWhen(new Date(slot.next_discount_at))}`),
  );
}

function renderChips() {
  const box = document.getElementById('category-chips');
  box.replaceChildren(...[...state.categories.values()].map((category) => {
    const chip = el('button', { type: 'button', class: 'chip', 'aria-pressed': 'false' },
      el('span', { class: 'dot', style: { background: colorOf(category.code) } }),
      category.label);
    chip.addEventListener('click', () => {
      if (state.selected.has(category.code)) state.selected.delete(category.code);
      else state.selected.add(category.code);
      chip.setAttribute('aria-pressed', String(state.selected.has(category.code)));
      refresh();
    });
    return chip;
  }));
}

function renderList() {
  const list = document.getElementById('slot-list');
  const count = state.slots.length;
  document.getElementById('results-count').textContent = count === 0
    ? 'Aucun créneau dans la zone'
    : `${count >= 500 ? '500+' : count} créneau${count > 1 ? 'x' : ''} disponible${count > 1 ? 's' : ''} dans la zone`;

  if (count === 0) {
    list.replaceChildren(el('li', { class: 'empty' },
      el('p', {}, 'Aucun créneau ne correspond ici pour le moment.'),
      el('p', { class: 'muted' }, 'Élargissez la carte ou les filtres. Les données de démonstration couvrent Paris.'),
      el('button', { type: 'button', class: 'btn-secondary', onclick: recenter }, 'Recentrer sur Paris')));
    return;
  }
  list.replaceChildren(...state.slots.map((slot) => el('li', {},
    el('button', { type: 'button', class: 'slot-card', onclick: () => focusMerchant(slot.merchant.id) },
      slotSummary(slot, { withMerchant: true })))));
}

function buildPopup({ merchant, slots }) {
  const category = state.categories.get(merchant.category);
  const meta = [category?.label, merchant.address, distanceLabel(slots[0].distance_m)]
    .filter(Boolean).join(' · ');
  return el('div', { class: 'popup' },
    el('div', { class: 'popup-title' },
      el('span', { class: 'dot', style: { background: colorOf(merchant.category) } }),
      merchant.name),
    el('div', { class: 'popup-meta' }, meta),
    el('ul', { class: 'popup-slots' }, ...slots.map((slot) => el('li', {},
      slotSummary(slot, { withMerchant: false }),
      el('button', { type: 'button', class: 'btn-reserve', onclick: () => startBooking(slot) }, 'Réserver')))));
}

function openMerchantPopup(merchantId) {
  const entry = state.merchants.get(merchantId);
  if (!entry) return;
  // Fermer l'ancienne popup déclenche son 'close' : on ne remplace l'état qu'après.
  state.popup?.remove();
  const popup = new maplibregl.Popup({ maxWidth: '340px', offset: 14 })
    .setLngLat([entry.merchant.lng, entry.merchant.lat])
    .setDOMContent(buildPopup(entry))
    .addTo(state.map);
  popup.on('close', () => {
    if (state.popup === popup) {
      state.popup = null;
      state.popupMerchantId = null;
    }
  });
  state.popup = popup;
  state.popupMerchantId = merchantId;
}

function syncPopup() {
  if (state.popupMerchantId == null) return;
  const entry = state.merchants.get(state.popupMerchantId);
  if (entry) state.popup.setDOMContent(buildPopup(entry));
  else state.popup.remove(); // plus aucun créneau chez ce commerce (réservé, filtré, hors zone)
}

function focusMerchant(merchantId) {
  const entry = state.merchants.get(merchantId);
  if (!entry) return;
  state.map.easeTo({
    center: [entry.merchant.lng, entry.merchant.lat],
    zoom: Math.max(state.map.getZoom(), FOCUS_ZOOM),
  });
  openMerchantPopup(merchantId);
}

function recenter() {
  state.map.flyTo({ center: state.config.default_center, zoom: state.config.default_zoom });
}

function startBooking() {
  // Branché à l'étape 3 (hold de 5 min puis paiement simulé).
  toast('La réservation en ligne arrive bientôt.');
}

/* ---------- Données ---------- */

function currentBbox() {
  const bounds = state.map.getBounds();
  const clamp = (value, limit) => Math.min(limit, Math.max(-limit, value));
  // Arrondi vers l'extérieur à 1e-5° (~1 m) : URL courtes sans perdre les commerces au bord.
  const down = (value) => (Math.floor(value * 1e5) / 1e5).toFixed(5);
  const up = (value) => (Math.ceil(value * 1e5) / 1e5).toFixed(5);
  return [
    down(clamp(bounds.getWest(), 180)),
    down(clamp(bounds.getSouth(), 90)),
    up(clamp(bounds.getEast(), 180)),
    up(clamp(bounds.getNorth(), 90)),
  ].join(',');
}

async function refresh() {
  if (!state.mapReady) return;
  const params = new URLSearchParams({ bbox: currentBbox(), within_hours: String(state.withinHours) });
  for (const code of state.selected) params.append('category', code);
  if (state.maxPrice != null) params.set('max_price', String(state.maxPrice));
  if (state.userPos) {
    params.set('lat', state.userPos.lat.toFixed(6));
    params.set('lng', state.userPos.lng.toFixed(6));
  }

  // Une seule requête en vol : la précédente (filtres ou viewport périmés) est annulée.
  state.fetchController?.abort();
  const controller = new AbortController();
  state.fetchController = controller;
  try {
    applySlots(await api(`/api/slots?${params}`, { signal: controller.signal }));
  } catch (err) {
    if (err.name === 'AbortError') return;
    console.error(err);
    toast('Impossible de charger les créneaux.');
  }
}

function applySlots(slots) {
  state.slots = slots;
  state.merchants = new Map();
  for (const slot of slots) {
    let entry = state.merchants.get(slot.merchant.id);
    if (!entry) {
      entry = { merchant: slot.merchant, slots: [] };
      state.merchants.set(slot.merchant.id, entry);
    }
    entry.slots.push(slot);
  }
  state.map.getSource('merchants').setData(toFeatureCollection());
  renderList();
  syncPopup();
}

function toFeatureCollection() {
  // Un point par commerce, avec les agrégats utiles à l'affichage et aux clusters.
  return {
    type: 'FeatureCollection',
    features: [...state.merchants.values()].map(({ merchant, slots }) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [merchant.lng, merchant.lat] },
      properties: {
        merchant_id: merchant.id,
        category: merchant.category,
        slots: slots.length,
        min_price: Math.min(...slots.map((slot) => slot.price_cents)),
        max_discount: Math.max(...slots.map((slot) => slot.discount_pct)),
      },
    })),
  };
}

/* ---------- Carte ---------- */

function categoryColorExpression() {
  const expression = ['match', ['get', 'category']];
  for (const [code, color] of Object.entries(CATEGORY_COLORS)) expression.push(code, color);
  expression.push(FALLBACK_COLOR);
  return expression;
}

function addMerchantLayers(map) {
  map.addSource('merchants', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
    cluster: true,
    clusterRadius: 45,
    // La source GeoJSON sert les tuiles du zoom entier inférieur (15,5 -> 15) : regrouper
    // jusqu'à 14 seulement pour que FOCUS_ZOOM montre chaque commerce.
    clusterMaxZoom: 14,
    // Agrégats calculés par cluster : nombre total de créneaux, meilleure remise.
    clusterProperties: {
      slots: ['+', ['get', 'slots']],
      max_discount: ['max', ['get', 'max_discount']],
    },
  });
  map.addLayer({
    id: 'clusters',
    type: 'circle',
    source: 'merchants',
    filter: ['has', 'point_count'],
    paint: {
      'circle-color': '#1d2a44', // bleu nuit Last Slot
      'circle-opacity': 0.9,
      'circle-radius': ['step', ['get', 'slots'], 15, 10, 19, 30, 24],
      'circle-stroke-width': 3,
      'circle-stroke-color': '#ffffff',
    },
  });
  map.addLayer({
    id: 'cluster-count',
    type: 'symbol',
    source: 'merchants',
    filter: ['has', 'point_count'],
    layout: {
      'text-field': ['to-string', ['get', 'slots']],
      'text-font': ['Noto Sans Bold'],
      'text-size': 13,
      'text-allow-overlap': true,
    },
    paint: { 'text-color': '#ffffff' },
  });
  map.addLayer({
    id: 'merchant-points',
    type: 'circle',
    source: 'merchants',
    filter: ['!', ['has', 'point_count']],
    paint: {
      'circle-color': categoryColorExpression(),
      'circle-radius': 9,
      'circle-stroke-width': 2.5,
      'circle-stroke-color': '#ffffff',
    },
  });
  map.addLayer({
    id: 'merchant-discount',
    type: 'symbol',
    source: 'merchants',
    filter: ['!', ['has', 'point_count']],
    layout: {
      'text-field': ['concat', '-', ['to-string', ['get', 'max_discount']], '%'],
      'text-font': ['Noto Sans Bold'],
      'text-size': 12,
      'text-anchor': 'bottom',
      'text-offset': [0, -0.9],
    },
    paint: { 'text-color': '#c92a2a', 'text-halo-color': '#ffffff', 'text-halo-width': 2 },
  });

  for (const layer of ['clusters', 'merchant-points']) {
    map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
  }
  map.on('click', 'clusters', async (event) => {
    const feature = event.features[0];
    const zoom = await map.getSource('merchants').getClusterExpansionZoom(feature.properties.cluster_id);
    map.easeTo({ center: feature.geometry.coordinates, zoom });
  });
  map.on('click', 'merchant-points', (event) => {
    openMerchantPopup(event.features[0].properties.merchant_id);
  });
}

function initMap() {
  const map = new maplibregl.Map({
    container: 'map',
    style: state.config.map_style_url,
    center: state.config.default_center,
    zoom: state.config.default_zoom,
    renderWorldCopies: false, // longitudes du viewport toujours dans [-180, 180]
    attributionControl: { compact: true },
  });
  state.map = map;
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  map.on('load', () => {
    addMerchantLayers(map);
    state.mapReady = true;
    refresh();
  });
  map.on('moveend', debounce(refresh, MOVE_DEBOUNCE_MS));
  map.on('error', (event) => console.warn('MapLibre :', event.error?.message ?? event));
}

function showUserMarker() {
  if (!state.userMarker) {
    state.userMarker = new maplibregl.Marker({
      element: el('div', { class: 'user-dot', 'aria-label': 'Votre position' }),
    });
  }
  state.userMarker.setLngLat([state.userPos.lng, state.userPos.lat]).addTo(state.map);
}

function locateUser() {
  if (!('geolocation' in navigator)) {
    toast("La géolocalisation n'est pas disponible sur cet appareil.");
    return;
  }
  const button = document.getElementById('locate-btn');
  button.disabled = true;
  navigator.geolocation.getCurrentPosition(
    (position) => {
      button.disabled = false;
      state.userPos = { lat: position.coords.latitude, lng: position.coords.longitude };
      showUserMarker();
      // Le moveend de fin d'animation relance la recherche, avec les distances.
      state.map.flyTo({ center: [state.userPos.lng, state.userPos.lat], zoom: 14 });
    },
    (error) => {
      button.disabled = false;
      toast(error.code === error.PERMISSION_DENIED ? 'Géolocalisation refusée.' : 'Position indisponible.');
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 },
  );
}

/* ---------- Filtres ---------- */

function bindFilters() {
  const price = document.getElementById('max-price');
  const priceOutput = document.getElementById('price-output');
  const debouncedRefresh = debounce(refresh, MOVE_DEBOUNCE_MS);
  const syncPrice = () => {
    const value = Number(price.value);
    state.maxPrice = value >= PRICE_SLIDER_MAX ? null : value;
    priceOutput.textContent = state.maxPrice == null ? 'tous les prix' : `${state.maxPrice} €`;
  };
  syncPrice();
  price.addEventListener('input', () => { syncPrice(); debouncedRefresh(); });

  const within = document.getElementById('within-hours');
  state.withinHours = Number(within.value);
  within.addEventListener('change', () => { state.withinHours = Number(within.value); refresh(); });

  document.getElementById('locate-btn').addEventListener('click', locateUser);
}

/* ---------- Temps réel ---------- */

function connectStream() {
  // EventSource se reconnecte seul (délai fixé par le serveur : retry: 3000).
  const source = new EventSource('/api/stream');
  const onChange = debounce(refresh, SSE_DEBOUNCE_MS);
  source.addEventListener('open', () => {
    setLive(true);
    onChange(); // resynchronise ce qui a pu changer pendant une coupure
  });
  source.addEventListener('error', () => setLive(false));
  source.addEventListener('slot', onChange);
  source.addEventListener('booking', onChange);
}

/* ---------- Démarrage ---------- */

async function init() {
  const [config, categories] = await Promise.all([api('/api/config'), api('/api/categories')]);
  state.config = config;
  for (const category of categories) state.categories.set(category.code, category);
  document.title = `${config.app_name} — créneaux libérés près de chez vous`;
  renderChips();
  bindFilters();
  initMap();
  connectStream();
  // Les paliers franchis arrivent par SSE ; ce rafraîchissement garde « dans X min » à jour.
  setInterval(refresh, REFRESH_EVERY_MS);
}

// Module différé : le DOM est déjà analysé quand ce code s'exécute.
init().catch((err) => {
  console.error(err);
  document.getElementById('results-count').textContent = 'Service indisponible pour le moment.';
  toast("Impossible de joindre l'API.");
});
