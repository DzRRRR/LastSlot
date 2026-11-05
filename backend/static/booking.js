/*
 * Réservation côté client : coordonnées → place bloquée HOLD_SECONDS au prix figé → paiement
 * simulé → confirmation. Fenêtre <dialog> native (modale, Échap pour fermer).
 * L'id de réservation renvoyé par l'API est une capability : il ne sort pas de cette page.
 * Anti-XSS : DOM construit avec el() (textContent), comme le reste du client.
 */
import { ApiError, api, colorOf, dayLabel, durationLabel, el, euros, fmtTime } from './common.js';

const dialog = document.getElementById('booking-dialog');
const flow = { slot: null, isEvent: false, holdSeconds: 300, booking: null, timer: null };

export function openBooking(slot, { isEvent, holdSeconds }) {
  Object.assign(flow, { slot, isEvent, holdSeconds, booking: null });
  renderForm();
  if (!dialog.open) dialog.showModal();
}

function release(bookingId) {
  // keepalive : la requête part même si la page est en train de se fermer.
  fetch(`/api/bookings/${bookingId}`, { method: 'DELETE', keepalive: true }).catch(() => {});
}

// Fermeture (bouton, Échap) : une place encore bloquée est rendue aussitôt aux autres clients.
dialog.addEventListener('close', () => {
  stopTimer();
  if (flow.booking?.status === 'held') release(flow.booking.id);
  flow.booking = null;
});
// Onglet fermé ou rechargé pendant le hold.
window.addEventListener('pagehide', () => {
  if (flow.booking?.status === 'held') release(flow.booking.id);
});

function stopTimer() {
  clearInterval(flow.timer);
  flow.timer = null;
}

function show(...children) {
  dialog.replaceChildren(
    el('button', { type: 'button', class: 'dialog-close', 'aria-label': 'Fermer', onclick: () => dialog.close() }, '×'),
    ...children);
}

function errorLine() {
  const line = el('p', { class: 'form-error', role: 'alert', hidden: true });
  line.fail = (message) => {
    line.textContent = message;
    line.hidden = false;
  };
  return line;
}

function summary() {
  const { slot } = flow;
  const start = new Date(slot.starts_at);
  return el('div', { class: 'booking-summary' },
    el('span', { class: 'dot', style: { background: colorOf(slot.merchant.category) } }),
    el('div', {},
      el('strong', {}, slot.merchant.name),
      el('div', {}, `${slot.service_name} · ${durationLabel(slot)}`),
      el('div', { class: 'muted' }, `${dayLabel(start)} à ${fmtTime.format(start)} · ${slot.merchant.address}`)));
}

function closeButton(label, primary = false) {
  return el('button', { type: 'button', class: primary ? 'btn-primary' : 'btn-secondary', onclick: () => dialog.close() }, label);
}

/* ---------- 1. Coordonnées ---------- */

function renderForm() {
  const { slot, isEvent } = flow;
  const error = errorLine();
  const submit = el('button', { type: 'submit', class: 'btn-primary' }, 'Bloquer ma place');
  const form = el('form', { class: 'booking-form', novalidate: true, onsubmit: (event) => submitHold(event, error, submit) },
    el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Votre nom'),
      el('input', { name: 'name', required: true, maxlength: 80, autocomplete: 'name' })),
    el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'E-mail ou téléphone'),
      el('input', { name: 'contact', required: true, minlength: 3, maxlength: 120, autocomplete: 'email' })),
    isEvent && el('label', { class: 'field' }, el('span', { class: 'field-label' }, 'Nombre de places'),
      el('input', { name: 'seats', type: 'number', min: 1, max: Math.min(10, slot.remaining), value: 1 })),
    el('p', { class: 'booking-price' },
      el('s', {}, euros(slot.base_price_cents)),
      el('strong', {}, euros(slot.price_cents)),
      el('span', { class: 'badge' }, `-${slot.discount_pct} %`),
      isEvent && el('span', { class: 'muted' }, 'la place')),
    error,
    el('div', { class: 'dialog-actions' }, closeButton('Annuler'), submit),
    el('p', { class: 'hint' },
      `Votre place est bloquée ${Math.round(flow.holdSeconds / 60)} min, le temps de payer. Le prix affiché est garanti.`));
  show(el('h2', { id: 'booking-title' }, 'Réserver ce créneau'), summary(), form);
  form.elements.name.focus();
}

async function submitHold(event, error, submit) {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  const name = fields.name.value.trim();
  const contact = fields.contact.value.trim();
  const seats = flow.isEvent ? Number(fields.seats.value) : 1;
  if (!name) return error.fail('Indiquez votre nom.');
  if (contact.length < 3) return error.fail('Indiquez un e-mail ou un numéro de téléphone.');
  if (!Number.isInteger(seats) || seats < 1 || seats > Math.min(10, flow.slot.remaining)) {
    return error.fail(`Entre 1 et ${Math.min(10, flow.slot.remaining)} places.`);
  }
  submit.disabled = true;
  try {
    flow.booking = await api('/api/bookings', {
      method: 'POST',
      body: { slot_id: flow.slot.id, customer_name: name, customer_contact: contact, seats },
    });
    renderHeld();
  } catch (err) {
    submit.disabled = false;
    if (err instanceof ApiError && err.status === 409) return renderUnavailable(err.message);
    error.fail(err instanceof ApiError ? err.message : 'Erreur réseau, réessayez.');
  }
  return undefined;
}

/* ---------- 2. Place bloquée, paiement ---------- */

function renderHeld() {
  const { booking } = flow;
  // Compte à rebours à partir de la durée donnée par le serveur : indépendant de l'heure de l'appareil.
  const deadline = Date.now() + booking.expires_in_s * 1000;
  const countdown = el('strong', { class: 'countdown' });
  const error = errorLine();
  const pay = el('button', { type: 'button', class: 'btn-primary' }, `Payer ${euros(booking.amount_cents)}`);
  pay.addEventListener('click', () => confirmPayment(pay, error));
  const tick = () => {
    const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
    countdown.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
    if (left === 0) {
      stopTimer();
      flow.booking = { ...flow.booking, status: 'expired' };
      renderUnavailable('Délai dépassé : la place a été libérée pour les autres clients.');
    }
  };
  show(el('h2', { id: 'booking-title' }, 'Place bloquée pour vous'), summary(),
    el('p', { class: 'hold-timer' }, 'Il vous reste ', countdown, ' pour payer.'),
    el('p', { class: 'booking-price' },
      el('s', {}, euros(booking.base_price_cents * booking.seats)),
      el('strong', {}, euros(booking.amount_cents)),
      el('span', { class: 'badge' }, `-${booking.discount_pct} %`),
      booking.seats > 1 && el('span', { class: 'muted' }, `${booking.seats} places`)),
    error,
    el('div', { class: 'dialog-actions' }, closeButton('Annuler'), pay),
    el('p', { class: 'hint' }, 'Paiement simulé pour la démo : aucune donnée bancaire n\'est demandée.'));
  tick();
  flow.timer = setInterval(tick, 1000);
  pay.focus();
}

async function confirmPayment(button, error) {
  button.disabled = true;
  try {
    flow.booking = await api(`/api/bookings/${flow.booking.id}/confirm`, { method: 'POST' });
    stopTimer();
    renderConfirmed();
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      stopTimer();
      flow.booking = { ...flow.booking, status: 'expired' }; // plus rien à rendre
      renderUnavailable(err.message);
      return;
    }
    button.disabled = false;
    error.fail(err instanceof ApiError ? err.message : 'Erreur réseau, réessayez.');
  }
}

/* ---------- 3. Issue ---------- */

function renderConfirmed() {
  const { booking } = flow;
  const saved = booking.base_price_cents * booking.seats - booking.amount_cents;
  show(
    el('div', { class: 'success-mark', 'aria-hidden': 'true' }, '✓'),
    el('h2', { id: 'booking-title' }, 'Réservation confirmée'),
    summary(),
    el('p', { class: 'booking-paid' }, `Payé ${euros(booking.amount_cents)}`,
      saved > 0 && el('strong', {}, ` · ${euros(saved)} d'économie`)),
    el('p', { class: 'muted' },
      `Référence ${booking.id.slice(0, 8).toUpperCase()} · ${booking.slot.merchant_name} a été prévenu.`),
    el('div', { class: 'dialog-actions' }, closeButton('Fermer', true)));
}

function renderUnavailable(message) {
  show(el('h2', { id: 'booking-title' }, 'Créneau indisponible'), summary(),
    el('p', {}, message),
    el('div', { class: 'dialog-actions' }, closeButton('Voir les autres créneaux', true)));
}
