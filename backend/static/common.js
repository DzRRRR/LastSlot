/*
 * Utilitaires partagés par la carte client (app.js) et le dashboard commerçant (pro.js).
 * Modules ES servis tels quels (pas de build) ; CSP script-src 'self' : aucun script inline.
 * Anti-XSS : tout le DOM passe par el() (createElement / textContent), jamais par innerHTML.
 */

export const TZ = 'Europe/Paris'; // heure locale des commerces, quel que soit le fuseau de l'appareil

// Secteurs de la V1 : coiffure (violets), beauté (roses), sport indoor (verts / orange).
export const CATEGORY_COLORS = {
  coiffure: '#7048e8',
  barbier: '#4263eb',
  esthetique: '#d6336c',
  ongles: '#ae3ec9',
  padel: '#2f9e44',
  foot5: '#0c8599',
  tennis: '#e67700',
};
export const FALLBACK_COLOR = '#495057';

export function colorOf(code) {
  return CATEGORY_COLORS[code] ?? FALLBACK_COLOR;
}

/* ---------- DOM ---------- */

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'style') Object.assign(node.style, value); // CSSOM : non bloqué par la CSP
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  // append() convertit les chaînes en nœuds texte : jamais interprétées comme du HTML.
  node.append(...children.filter((child) => child != null && child !== false));
  return node;
}

export function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

let toastTimer = null;
export function toast(message) {
  const box = document.getElementById('toast');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 3500);
}

/* ---------- API ---------- */

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function errorMessage(data) {
  // FastAPI : {detail: "message"} ou, pour une erreur de validation, {detail: [{msg, ...}]}.
  const detail = data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail) && detail.length) return detail.map((item) => item.msg).join(' ; ');
  return null;
}

export async function api(url, { method = 'GET', body, headers = {}, signal } = {}) {
  const init = { method, signal, headers: { Accept: 'application/json', ...headers } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, errorMessage(data) ?? `Erreur ${res.status}`);
  return data;
}

/* ---------- Formatage (fr-FR, heure de Paris) ---------- */

export const fmtTime = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit', timeZone: TZ });
const fmtWeekday = new Intl.DateTimeFormat('fr-FR', { weekday: 'long', day: 'numeric', month: 'short', timeZone: TZ });
const fmtParts = new Intl.DateTimeFormat('en-US', {
  timeZone: TZ, hourCycle: 'h23',
  year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
});

/** Composantes de l'heure murale de Paris pour un instant donné. */
export function parisParts(date) {
  const parts = Object.fromEntries(fmtParts.formatToParts(date).map((p) => [p.type, p.value]));
  return {
    year: Number(parts.year), month: Number(parts.month), day: Number(parts.day),
    hour: Number(parts.hour), minute: Number(parts.minute), second: Number(parts.second),
    ymd: `${parts.year}-${parts.month}-${parts.day}`,
    hm: `${parts.hour}:${parts.minute}`,
  };
}

/** Heure murale de Paris ('AAAA-MM-JJ', 'HH:MM') -> instant (Date), ou null si invalide. */
export function parisWallTimeToDate(ymd, hm) {
  const date = /^(\d{4})-(\d{2})-(\d{2})$/.exec(ymd ?? '');
  const time = /^(\d{2}):(\d{2})/.exec(hm ?? '');
  if (!date || !time) return null;
  const wall = Date.UTC(+date[1], +date[2] - 1, +date[3], +time[1], +time[2]);
  // Décalage de Paris à cet instant (+60 ou +120 min), affiné deux fois autour des changements d'heure.
  let instant = wall;
  for (let i = 0; i < 2; i += 1) {
    const p = parisParts(new Date(instant));
    const offset = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - instant;
    instant = wall - offset;
  }
  return new Date(instant);
}

function nextYmd(ymd) {
  const [y, m, d] = ymd.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
}

export function dayLabel(date) {
  const today = parisParts(new Date()).ymd;
  const ymd = parisParts(date).ymd;
  if (ymd === today) return "Aujourd'hui";
  if (ymd === nextYmd(today)) return 'Demain';
  const label = fmtWeekday.format(date);
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** « 09:15 » aujourd'hui, sinon « demain 09:15 » / « lundi 10 nov. 09:15 ». */
export function shortWhen(date) {
  const day = dayLabel(date);
  const time = fmtTime.format(date);
  return day === "Aujourd'hui" ? time : `${day.charAt(0).toLowerCase()}${day.slice(1)} ${time}`;
}

export function startsInLabel(date) {
  const minutes = Math.round((date - Date.now()) / 60000);
  if (minutes < 1) return 'maintenant';
  if (minutes < 60) return `dans ${minutes} min`;
  if (minutes < 180) {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return m ? `dans ${h} h ${String(m).padStart(2, '0')}` : `dans ${h} h`;
  }
  return null;
}

export function durationLabel(slot) {
  const minutes = Math.round((new Date(slot.ends_at) - new Date(slot.starts_at)) / 60000);
  if (minutes < 60) return `${minutes} min`;
  const rest = minutes % 60;
  return `${Math.floor(minutes / 60)} h${rest ? ` ${String(rest).padStart(2, '0')}` : ''}`;
}

export function euros(cents) {
  return new Intl.NumberFormat('fr-FR', {
    style: 'currency',
    currency: 'EUR',
    minimumFractionDigits: cents % 100 ? 2 : 0,
    maximumFractionDigits: 2,
  }).format(cents / 100);
}

export function distanceLabel(meters) {
  if (meters == null) return null;
  if (meters < 1000) return `${Math.max(10, Math.round(meters / 10) * 10)} m`;
  return `${(meters / 1000).toFixed(1).replace('.', ',')} km`;
}

/** Prix remisé, arrondi demi vers le haut : même règle que discounted_price_cents (schema.sql). */
export function discountedPrice(baseCents, discountPct) {
  return Math.max(1, Math.floor((baseCents * (100 - discountPct) + 50) / 100));
}

/* ---------- Remise progressive (miroir de discount_at dans schema.sql) ---------- */

const HOUR_MS = 3600e3;

/** Paliers [jusqu'à H-6, de H-6 à H-2, dès H-2] pour un plafond donné, arrondis à 5 %. */
export function discountTiers(maxPct) {
  const tier = (numerator) => Math.min(maxPct, Math.max(5, Math.round(numerator / 15) * 5));
  return [tier(maxPct), tier(maxPct * 2), maxPct];
}

/** Paliers restants d'un créneau : [{pct, from}], from = null pour la remise en cours. */
export function discountSchedule(start, maxPct, now = new Date()) {
  const [early, mid, max] = discountTiers(maxPct);
  const h6 = new Date(start.getTime() - 6 * HOUR_MS);
  const h2 = new Date(start.getTime() - 2 * HOUR_MS);
  const current = now >= h2 ? max : now >= h6 ? mid : early;
  const steps = [{ pct: current, from: null }];
  for (const [pct, from] of [[mid, h6], [max, h2]]) {
    if (now < from && pct > steps[steps.length - 1].pct) steps.push({ pct, from });
  }
  return steps;
}

/** Ce que touche le pro : prix payé moins la commission (arrondie demi vers le haut). */
export function netAfterCommission(priceCents, commissionPct) {
  return priceCents - Math.floor((priceCents * commissionPct + 50) / 100);
}

/* ---------- Temps réel ---------- */

export function setLive(on) {
  const badge = document.getElementById('live-status');
  badge.dataset.on = String(on);
  badge.textContent = on ? 'Temps réel' : 'Reconnexion…';
}
