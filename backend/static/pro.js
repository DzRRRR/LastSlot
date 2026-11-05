/*
 * Dashboard commerçant Last Slot : connexion par token, remise en vente d'un créneau libéré
 * (prestation, heure, remise maximale), suivi temps réel.
 * Le token du lien personnel (pro.html#t=…) est dans le fragment : le navigateur ne l'envoie
 * jamais au serveur. On le retire aussitôt de la barre d'adresse et de l'historique, puis on le
 * garde en sessionStorage (onglet courant) ; il part uniquement dans l'en-tête X-Merchant-Token.
 */
import {
  ApiError, api, colorOf, dayLabel, debounce, discountSchedule, discountedPrice, el, euros, fmtTime,
  netAfterCommission, parisParts, parisWallTimeToDate, setLive, shortWhen, toast,
} from './common.js';

const TOKEN_KEY = 'lastslot.merchantToken';
const INVALID_LINK = 'Lien invalide ou expiré : les liens changent à chaque réinitialisation des données de démo.';
const QUICK_STARTS = [[30, 'Dans 30 min'], [60, 'Dans 1 h'], [120, 'Dans 2 h']];
const QUICK_MAX_DISCOUNTS = [20, 30, 40, 50]; // 30 % : l'exemple de l'executive summary
const SPORT_CATEGORIES = new Set(['padel', 'foot5', 'tennis']); // ressources = terrains
const SSE_DEBOUNCE_MS = 250;
const MAX_ADVANCE_DAYS = 14; // borne de l'API
const QUARTER_MS = 15 * 60000;

const state = { token: null, me: null, config: null, slots: [], source: null };
const configRequest = api('/api/config'); // commission, nom de l'app

const $ = (id) => document.getElementById(id);

/* ---------- Token ---------- */

function readStoredToken() {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; } // stockage bloqué
}

function storeToken(token) {
  try { sessionStorage.setItem(TOKEN_KEY, token); } catch { /* session limitée à cette page */ }
}

function forgetToken() {
  try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* rien à effacer */ }
}

/** Token du fragment (#t=…) : null sans fragment, '' si le fragment est mal formé. */
function takeTokenFromFragment() {
  if (!location.hash) return null;
  const match = /^#t=([A-Za-z0-9_-]{20,200})$/.exec(location.hash);
  // replaceState ne déclenche pas hashchange : pas de boucle.
  history.replaceState(null, '', location.pathname + location.search);
  return match ? match[1] : '';
}

/** Connexion depuis le fragment ; false s'il n'y en avait pas. */
function loginFromFragment() {
  const token = takeTokenFromFragment();
  if (token === null) return false;
  if (token) login(token);
  else showLogin(INVALID_LINK);
  return true;
}

function tokenFromInput(raw) {
  // Accepte le lien complet comme le token seul.
  const value = raw.trim();
  const index = value.indexOf('#t=');
  return index >= 0 ? value.slice(index + 3) : value;
}

function proApi(url, options = {}) {
  return api(url, { ...options, headers: { 'X-Merchant-Token': state.token } });
}

/* ---------- Session ---------- */

function showLogin(message) {
  $('dashboard').hidden = true;
  $('login-view').hidden = false;
  $('login-error').hidden = !message;
  $('login-error').textContent = message ?? '';
  $('token-input').focus();
}

async function login(token) {
  state.token = token;
  try {
    [state.me, state.config] = await Promise.all([proApi('/api/pro/me'), configRequest]);
  } catch (err) {
    state.token = null;
    forgetToken();
    showLogin(err instanceof ApiError && err.status === 401
      ? INVALID_LINK
      : 'Service indisponible, réessayez dans un instant.');
    return;
  }
  storeToken(token);
  renderHeader();
  setupForm();
  $('login-view').hidden = true;
  $('dashboard').hidden = false;
  connectStream();
  await refreshSlots();
}

function logout(message) {
  state.source?.close();
  Object.assign(state, { token: null, me: null, slots: [], source: null });
  forgetToken();
  showLogin(message);
}

function handleError(err) {
  if (err instanceof ApiError && err.status === 401) {
    logout('Session expirée : reconnectez-vous avec votre lien.');
    return;
  }
  // Erreur métier (4xx) : message de l'API pour le commerçant ; le reste va aussi en console.
  if (!(err instanceof ApiError) || err.status >= 500) console.error(err);
  toast(err instanceof ApiError ? err.message : 'Erreur réseau, réessayez.');
}

/* ---------- En-tête et formulaire ---------- */

const isEvent = () => state.me.merchant.family === 'event';
const isSport = () => SPORT_CATEGORIES.has(state.me.merchant.category);
const commission = () => state.config.commission_pct;

function renderHeader() {
  const { merchant } = state.me;
  $('merchant-name').textContent = merchant.name;
  $('merchant-dot').style.background = colorOf(merchant.category);
  $('merchant-meta').textContent = `${merchant.category_label} · ${merchant.address}`;
  document.title = `${merchant.name} — ${state.config.app_name} Pro`;
}

function roundedFromNow(minutes) {
  // Quart d'heure suivant : les fuseaux de Paris (+1 h / +2 h) préservent l'alignement UTC.
  return new Date(Math.ceil((Date.now() + minutes * 60000) / QUARTER_MS) * QUARTER_MS);
}

function setStart(date) {
  const parts = parisParts(date);
  $('start-date').value = parts.ymd;
  $('start-time').value = parts.hm;
  updatePreview();
}

function setupForm() {
  const { services, resources } = state.me;
  // new Option(texte, valeur) : le texte est posé tel quel, sans interprétation HTML.
  $('service-select').replaceChildren(...services.map((service) => new Option(
    `${service.name} · ${service.duration_min} min · ${euros(service.base_price_cents)}`, String(service.id))));
  $('resource-label').textContent = isSport() ? 'Terrain' : 'Praticien';
  $('resource-select').replaceChildren(
    new Option(isSport() ? 'Premier terrain disponible' : 'Premier praticien disponible', ''),
    ...resources.map((resource) => new Option(resource.name, String(resource.id))));
  $('resource-field').hidden = isEvent() || resources.length === 0;
  $('capacity-field').hidden = !isEvent();

  $('quick-starts').replaceChildren(...QUICK_STARTS.map(([minutes, label]) => el('button',
    { type: 'button', class: 'chip', onclick: () => setStart(roundedFromNow(minutes)) }, label)));
  $('quick-discounts').replaceChildren(...QUICK_MAX_DISCOUNTS.map((pct) => el('button', {
    type: 'button', class: 'chip', 'aria-pressed': 'false', dataset: { pct: String(pct) },
    onclick: () => { $('discount-input').value = String(pct); updatePreview(); },
  }, `-${pct} %`)));
  $('commission-note').textContent = `0 € d'abonnement : ${commission()} % de commission, `
    + 'prélevée uniquement si le créneau est vendu.';

  const now = Date.now();
  $('start-date').min = parisParts(new Date(now)).ymd;
  $('start-date').max = parisParts(new Date(now + (MAX_ADVANCE_DAYS - 1) * 86400000)).ymd;
  setStart(roundedFromNow(60));
}

function formValues() {
  return {
    service: state.me.services.find((s) => String(s.id) === $('service-select').value),
    maxDiscount: Number($('discount-input').value),
    start: parisWallTimeToDate($('start-date').value, $('start-time').value),
  };
}

const validMaxDiscount = (d) => Number.isInteger(d) && d >= 5 && d <= 80;

/** Échéancier de la remise progressive : une ligne par palier restant. */
function scheduleTable(baseCents, start, maxDiscount) {
  const rows = discountSchedule(start, maxDiscount).map(({ pct, from }) => {
    const price = discountedPrice(baseCents, pct);
    return el('tr', {},
      el('th', { scope: 'row' }, from ? `Dès ${shortWhen(from)}` : 'Maintenant'),
      el('td', {}, el('span', { class: 'badge' }, `-${pct} %`)),
      el('td', {}, euros(price)),
      el('td', { class: 'net' }, euros(netAfterCommission(price, commission()))));
  });
  return el('table', { class: 'schedule' },
    el('thead', {}, el('tr', {},
      el('th', { scope: 'col' }, 'Remise progressive'), el('th', { scope: 'col' }, ''),
      el('th', { scope: 'col' }, 'Prix client'), el('th', { scope: 'col' }, 'Vous touchez'))),
    el('tbody', {}, ...rows));
}

function updatePreview() {
  if (!state.me) return;
  const { service, maxDiscount, start } = formValues();
  for (const chip of $('quick-discounts').children) {
    chip.setAttribute('aria-pressed', String(Number(chip.dataset.pct) === maxDiscount));
  }
  const preview = $('price-preview');
  if (!service || !validMaxDiscount(maxDiscount)) {
    preview.replaceChildren('Remise maximale entre 5 et 80 %.');
    return;
  }
  if (!start) {
    preview.replaceChildren('Choisissez la date et l\'heure du créneau.');
    return;
  }
  const end = new Date(start.getTime() + service.duration_min * 60000);
  preview.replaceChildren(
    el('p', { class: 'preview-when' },
      `${dayLabel(start)}, ${fmtTime.format(start)} – ${fmtTime.format(end)} · prix habituel `,
      el('s', {}, euros(service.base_price_cents))),
    scheduleTable(service.base_price_cents, start, maxDiscount));
}

async function publish(event) {
  event.preventDefault();
  const { service, maxDiscount, start } = formValues();
  if (!service) return toast('Choisissez une prestation.');
  if (!start || start.getTime() <= Date.now()) return toast('Choisissez un horaire à venir.');
  if (!validMaxDiscount(maxDiscount)) return toast('La remise maximale doit être comprise entre 5 et 80 %.');

  const body = { service_id: service.id, starts_at: start.toISOString(), max_discount_pct: maxDiscount };
  if (isEvent()) {
    const capacity = Number($('capacity-input').value);
    if (!Number.isInteger(capacity) || capacity < 1 || capacity > 500) {
      return toast('Indiquez le nombre de places libérées (1 à 500).');
    }
    body.capacity = capacity;
  } else if ($('resource-select').value) {
    body.resource_id = Number($('resource-select').value);
  }

  const button = $('publish-btn');
  button.disabled = true;
  try {
    const slot = await proApi('/api/pro/slots', { method: 'POST', body });
    const slotStart = new Date(slot.starts_at);
    toast(`Créneau en vente : ${slot.service_name}, ${dayLabel(slotStart).toLowerCase()} à `
      + `${fmtTime.format(slotStart)}, -${slot.discount_pct} % pour commencer.`);
    await refreshSlots();
  } catch (err) {
    handleError(err);
  } finally {
    button.disabled = false;
  }
  return undefined;
}

/* ---------- Mes créneaux ---------- */

async function refreshSlots() {
  if (!state.token) return;
  try {
    state.slots = await proApi('/api/pro/slots');
    renderSlots();
  } catch (err) {
    handleError(err);
  }
}

function slotState(slot) {
  if (slot.status === 'cancelled') return { label: 'Annulé', cls: 'is-cancelled', cancellable: false };
  if (new Date(slot.starts_at) <= Date.now()) return { label: 'Passé', cls: 'is-past', cancellable: false };
  if (slot.remaining === 0) return { label: 'Complet', cls: 'is-full', cancellable: true };
  return { label: 'En vente', cls: 'is-open', cancellable: true };
}

function bookingItem(booking) {
  const status = booking.status === 'confirmed'
    ? el('span', { class: 'booking-status is-confirmed' }, 'Payée')
    : el('span', { class: 'booking-status is-held' },
      `Paiement en cours, jusqu'à ${fmtTime.format(new Date(booking.expires_at))}`);
  return el('li', {},
    el('strong', {}, booking.customer_name),
    ` · ${booking.customer_contact}`,
    booking.seats > 1 && ` · ${booking.seats} places`,
    ' · ', status);
}

function slotItem(slot) {
  const start = new Date(slot.starts_at);
  const end = new Date(slot.ends_at);
  const current = slotState(slot);
  const onSale = current.cls === 'is-open';
  return el('li', { class: `pro-slot ${current.cls}` },
    el('div', { class: 'pro-slot-head' },
      el('strong', {}, `${dayLabel(start)} · ${fmtTime.format(start)} – ${fmtTime.format(end)}`),
      el('span', { class: `status ${current.cls}` }, current.label)),
    el('div', { class: 'pro-slot-what' }, [slot.service_name, slot.resource_name].filter(Boolean).join(' · ')),
    el('div', { class: 'slot-price' },
      el('s', { class: 'base' }, euros(slot.base_price_cents)),
      el('strong', { class: 'final' }, euros(slot.price_cents)),
      el('span', { class: 'badge' }, `-${slot.discount_pct} %`),
      el('span', { class: 'places' }, `plafond -${slot.max_discount_pct} %`),
      isEvent() && el('span', { class: 'places' }, `${slot.remaining}/${slot.capacity} places libres`)),
    onSale && el('div', { class: 'pro-slot-money' },
      `Vous touchez ${euros(netAfterCommission(slot.price_cents, commission()))} si vendu maintenant`,
      slot.next_discount_at && el('span', { class: 'next-tier' },
        ` · -${slot.next_discount_pct} % dès ${shortWhen(new Date(slot.next_discount_at))}`)),
    slot.bookings.length > 0 && el('ul', { class: 'pro-bookings', 'aria-label': 'Réservations' },
      ...slot.bookings.map(bookingItem)),
    current.cancellable && el('button', { type: 'button', class: 'btn-danger', onclick: () => cancelSlot(slot) },
      'Annuler le créneau'));
}

function renderSlots() {
  const online = state.slots.filter((s) => slotState(s).cancellable).length;
  $('slots-count').textContent = online ? `${online} en vente` : '';
  $('pro-slots').replaceChildren(...(state.slots.length
    ? state.slots.map(slotItem)
    : [el('li', { class: 'empty' }, 'Aucun créneau en vente pour le moment.')]));
}

async function cancelSlot(slot) {
  const start = new Date(slot.starts_at);
  const question = `Annuler « ${slot.service_name} » ${dayLabel(start).toLowerCase()} à ${fmtTime.format(start)} ?\n`
    + 'Les réservations non payées seront annulées.';
  if (!window.confirm(question)) return;
  try {
    await proApi(`/api/pro/slots/${slot.id}`, { method: 'DELETE' });
    toast('Créneau annulé.');
    await refreshSlots();
  } catch (err) {
    handleError(err);
  }
}

/* ---------- Temps réel ---------- */

function connectStream() {
  state.source?.close();
  const source = new EventSource('/api/stream');
  state.source = source;
  const refresh = debounce(refreshSlots, SSE_DEBOUNCE_MS);
  // Le flux est public et commun à tous : on ne garde que les événements de ce commerce.
  const isMine = (event) => {
    try { return JSON.parse(event.data).merchant_id === state.me?.merchant.id; } catch { return false; }
  };
  source.addEventListener('open', () => { setLive(true); refresh(); });
  source.addEventListener('error', () => setLive(false));
  source.addEventListener('slot', (event) => { if (isMine(event)) refresh(); }); // dont paliers de remise
  source.addEventListener('booking', (event) => {
    if (!isMine(event)) return;
    toast('Nouvelle réservation !');
    refresh();
  });
}

/* ---------- Démarrage ---------- */

$('login-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const token = tokenFromInput($('token-input').value);
  $('token-input').value = '';
  if (token) login(token);
});
$('logout-btn').addEventListener('click', () => logout());
$('slot-form').addEventListener('submit', publish);
for (const id of ['service-select', 'start-date', 'start-time', 'discount-input']) {
  $(id).addEventListener('input', updatePreview);
}
// Une fois par minute : statuts « Passé », libellés « Aujourd'hui / Demain » et échéancier à jour.
setInterval(() => {
  if (!state.me) return;
  renderSlots();
  updatePreview();
}, 60000);

// Un autre lien pro.html#t=… ouvert dans le même onglet ne recharge pas la page (même
// document, seul le fragment change) : on bascule sur ce compte via hashchange.
window.addEventListener('hashchange', loginFromFragment);

if (!loginFromFragment()) {
  const stored = readStoredToken();
  if (stored) login(stored);
  else showLogin();
}
