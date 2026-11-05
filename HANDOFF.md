# Passation — état au 2026-11-05 (étapes 1 et 2 terminées, alignées sur l'executive summary)

## Produit (executive summary de l'équipe, `ExecutiveSummary.pdf` dans le dossier parent)

- **Last Slot** — « Une annulation, une opportunité. » Un client annule : le pro remet le créneau
  en vente, moins cher ; un client du quartier le réserve.
- **V1 : Paris**, secteurs **coiffure, beauté** (instituts, onglerie) **et sport indoor** (padel,
  foot à 5, tennis/squash). Année 2 : bien-être et cours de sport ; année 3 : culture et loisirs.
- **Remise progressive** : le pro fixe un **plafond** ; la remise appliquée monte toute seule à
  l'approche du rendez-vous : 1/3 du plafond jusqu'à H-6, 2/3 jusqu'à H-2, puis le plafond
  (arrondis à 5 %). Exemple de l'executive summary, plafond 30 % : -10 % la veille, -20 % à H-6,
  -30 % à H-2.
- **0 € d'abonnement, 15 % de commission** uniquement sur les créneaux vendus.

## Parcours de démo visé (ce que le jury doit voir en 2 min)

1. Le commerçant remet un créneau en vente depuis son dashboard (3 actions : prestation, heure,
   remise maximale) ; il voit l'échéancier de la remise et ce qu'il touchera.
2. Le créneau apparaît instantanément sur la carte client (SSE), avec la remise du moment et le
   prochain palier.
3. Un client filtre (secteur, prix max, « dans les X h »), clique le pin, réserve. *(étape 3)*
4. La place est bloquée 5 min (hold) → « Payer » (simulé) → confirmée. *(étape 3)*
5. Le créneau disparaît de la carte des autres en temps réel ; le commerçant voit la réservation.
   *(étape 3)*

## Fait

### Socle

- `backend/app/schema.sql` (version 2, voir `SCHEMA_VERSION` dans `db.py`) — catégories, merchant,
  service, resource, slot, booking, avec :
  - `CHECK (remaining >= 0 AND remaining <= capacity)` : garde-fou anti-surréservation.
  - Contrainte d'exclusion `slot_no_resource_overlap` (btree_gist) : un praticien ou un terrain
    ne peut pas avoir deux créneaux ouverts qui se chevauchent.
  - `booking.id` en UUID v4 (capability non énumérable).
  - Index `merchant_location_geom_gix` sur `(location::geometry)` (filtre par viewport).
  - Créneau : `base_price_cents` (tarif figé à la publication) et `max_discount_pct` (plafond) ;
    le prix courant n'est pas stocké, il dépend de l'heure.
  - Fonctions `discount_at(starts_at, max_pct, at)` (paliers) et `discounted_price_cents(base, pct)`
    (arrondi demi vers le haut, jamais 0), vue `slot_priced` (prix courant + prochain palier).
- `backend/app/db.py` — pool asyncpg, application du schéma sous `pg_advisory_lock`, contrôle de
  version (refus explicite d'une base v1 : « recréez la base avec `python -m app.seed --reset` »),
  `reset_schema` (supprime et recrée tout, utilisé par `seed --reset`).
- `backend/app/config.py` — settings par variables d'environnement : `DATABASE_URL`, `APP_NAME`
  (Last Slot), `COMMISSION_PCT` (15), `HOLD_SECONDS`, `EXPIRY_INTERVAL_SECONDS`,
  `SSE_HEARTBEAT_SECONDS`, `PUBLIC_BASE_URL`, `MAP_STYLE_URL`, `CSP_ENABLED` ; centre et zoom de
  la carte : Paris, zoom 12.
- `backend/app/broker.py` — broker SSE en mémoire (queue par abonné, désabonnement si plein).
- `backend/app/security.py` — tokens commerçants (`secrets.token_urlsafe(32)`, SHA-256 en base),
  dépendance `current_merchant_id` (header `X-Merchant-Token`, 401 si absent ou invalide).
- `backend/static/vendor/` — MapLibre GL JS 5.24.0 (js, css, LICENSE).
- Testé sur PostgreSQL 16.15 + PostGIS 3.7.0 (Ubuntu 24.04, PGDG) et, chez Thomas, sur l'image
  `postgis/postgis:16-3.4` (celle de l'étape 4).

### Étape 1 — API publique + seed + carte

- `app/main.py` — lifespan (pool → schéma → tâches de fond ; arrêt propre), middleware ASGI pur
  d'en-têtes (CSP, `nosniff`, `no-referrer` ; CSP absente sur `/docs` et `/redoc`), fichiers
  statiques en `Cache-Control: no-cache` (`RevalidatedStaticFiles`), montés en dernier.
- `app/api_public.py` — `GET /api/config` (nom, style de carte, durée du hold, centre et zoom,
  commission), `/api/categories` (ordre d'affichage), `/api/slots` (bbox, secteurs, prix max sur
  le prix courant, horizon, distance ; renvoie prix de base, plafond, remise et prix courants,
  prochain palier), `/api/stream` (SSE).
- `app/jobs.py` — boucle de tâches de fond : expiration des holds (publie `slot`
  change "released") et annonce des paliers de remise franchis (publie `slot` change "discount",
  les cartes ouvertes rechargent le nouveau prix).
- `app/seed.py` — `python -m app.seed --reset [--tokens-out F] [--base-url URL]` : 55 commerces
  fictifs à Paris (16 coiffeurs, 6 barbiers, 11 instituts, 8 onglerie, 5 padel, 4 foot à 5,
  5 tennis/squash), ~100 créneaux dans les 36 h aux heures d'ouverture de chaque secteur,
  plafonds de remise 20 à 50 %, `demo-tokens.csv` en 0600.
- `static/index.html` + `app.js` + `style.css` — carte et liste, filtres, « Autour de moi »,
  clusters, popup, prochain palier affiché, temps réel, mobile, bandeau démo.

### Étape 2 — Dashboard commerçant

- `app/api_pro.py` — `/api/pro/*`, toutes filtrées par le merchant_id du token (anti-IDOR) :
  `GET /me`, `GET /slots` (depuis now - 2 h, réservations held/confirmed sans leur id),
  `POST /slots` (plafond 5..80, tarif figé, praticien ou terrain attribué si non choisi, 409 en cas
  de chevauchement ; publie `slot` change "created"), `DELETE /slots/{id}` (409 si réservation
  payée, sinon holds annulés ; publie `slot` change "cancelled").
- `static/pro.html` + `pro.js` + `pro.css` — connexion par lien personnel (`#t=`, retiré de l'URL,
  `sessionStorage`) ou saisie ; formulaire en 3 actions ; aperçu = échéancier de la remise
  progressive avec prix client et montant net pour le pro (après 15 %) ; « Mes créneaux » (remise
  du moment, plafond, prochain palier, net, réservations, annulation) ; toast « Nouvelle
  réservation » sur les événements `booking` de ce commerce ; libellé « Terrain » pour le sport.
- `static/common.js` — utilitaires partagés (DOM sans innerHTML, API, formats, heure de Paris,
  miroir JS des paliers `discountTiers` / `discountSchedule`, commission).

### Tests

- 69 tests pytest (`backend/tests/`) : API publique (filtres, validation, prix courants et
  prochain palier, en-têtes), API pro (401, IDOR, attribution, chevauchements, tarif figé,
  validation, annulation, SSE), paliers SQL (bornes H-6 / H-2 incluses, exemple 30 % → 10/20/30),
  refus d'un schéma v1, annonce temps réel d'un palier franchi, générateur SSE, seed (Paris,
  horaires par secteur, pas de chevauchement, tokens hachés, `--reset`).
- Vérifié dans Chromium headless (scripts hors dépôt) : parcours de démo 1-2 (publication →
  visible côté client en ~0,4 s → annulation → retirée), padel (terrains), nom piège
  `<img onerror=…>` affiché littéralement, aucune violation CSP, mobile.

### Écarts par rapport au plan initial (et pourquoi)

1. **Executive summary** (2026-11-05) : nom Last Slot, Paris, secteurs de la V1, remise
   progressive avec plafond (au lieu d'une remise fixe), commission affichée côté pro. Le seed
   n'a plus de catégorie de famille `event` (cours collectifs = année 2) ; l'API la gère toujours
   et les tests créent leur propre catégorie `cours-test`.
2. **Filtre bbox en geometry** (`&&` + index d'expression) plutôt qu'en geography : le viewport
   Web Mercator est un rectangle lon/lat ; en geography, les bords deviennent des arcs de grand
   cercle (~1 km d'erreur en vue régionale, enveloppe dégénérée en vue monde).
3. **SSE** : `asyncio.wait` + tâche de lecture persistante au lieu de `wait_for` (pas de message
   perdu au timeout), `request.is_disconnected()` à chaque heartbeat, `EventStreamResponse`
   (fin propre quand uvicorn coupe les flux à l'arrêt). Lancer uvicorn avec
   `--timeout-graceful-shutdown 2`, sinon l'arrêt et chaque `--reload` attendent les onglets.
4. **Seed** : deux générateurs (catalogue graine 42, créneaux graine 43) ; zones de tirage par
   quartier vérifiées hors Seine, canaux, parcs, bois, cimetières et voies ferrées (polygones
   OpenStreetMap, marge 25 m) ; sport près des portes ; `--reset` recrée tout le schéma (pas de
   migrations pour le proto).
5. **Pro** : praticien ou terrain attribué automatiquement ; arrondi des prix demi vers le haut
   identique en SQL et en JS ; ids de réservation jamais renvoyés au commerçant ; `DELETE`
   verrouille le créneau puis ses réservations (un paiement concurrent ne laisse pas de
   réservation payée sur un créneau annulé ; interblocage avec le job d'expiration → rejeu).
6. **Front** : zoom initial fourni par `/api/config` ; `clusterMaxZoom: 14` (la source GeoJSON
   sert les tuiles du zoom entier inférieur) ; heures en heure de Paris ; `hashchange` écouté
   (autre lien pro dans le même onglet) ; `[hidden] { display: none !important }` ; « Réserver »
   affiche un toast en attendant l'étape 3 (`startBooking(slot)` dans `app.js`).
7. **Console navigateur** : « Expected value to be of type number, but found null » et quelques
   « Image … could not be loaded » viennent du style OpenFreeMap (présents sans nos couches).

## Points d'attention pour l'étape 3

- **Figer le prix à la réservation** : le prix baisse avec l'heure. Le hold doit enregistrer le
  prix payé (`price_cents` de `slot_priced` au moment du hold), la remise appliquée et la
  commission (`COMMISSION_PCT`) dans `booking` (nouvelles colonnes → `SCHEMA_VERSION = 3`,
  `seed --reset`). Le confirm facture le prix enregistré, pas le prix courant ; la modale client
  affiche ce prix.
- Publier `booking` {slot_id, merchant_id} à la création du hold et à la confirmation (le
  dashboard affiche « Nouvelle réservation »), et `slot` avec le nouveau `remaining`.
- Le confirm doit rester `UPDATE booking … WHERE status = 'held' AND expires_at > now()` : c'est
  ce sur quoi repose l'exclusion avec `DELETE` et avec le job d'expiration.
- Front client : brancher la modale sur `startBooking(slot)` dans `app.js`.
- Infra de test réutilisable : `tests/dbutil.py` (`insert_merchant` avec token connu,
  `insert_resource`, `insert_slot` avec plafond, `insert_booking`, `db_call`, `EVENT_CATEGORY`),
  fixture `server` (vrai uvicorn, tâches de fond toutes les 0,2 s).
- `HEAD /api/*` répond 404 (FastAPI ne mappe pas HEAD sur les routes GET) : utiliser GET.

## À faire, dans l'ordre

### Étape 3 — Réservation sans double-booking

- `POST /api/bookings` {slot_id, customer_name 1..80, customer_contact 3..120, seats 1..10}.
  Transaction :
  1. `UPDATE slot SET remaining = remaining - $seats WHERE id = $1 AND status = 'open'
     AND remaining >= $seats AND starts_at > now() RETURNING merchant_id, remaining`
     → aucune ligne ⇒ 409 « plus de place ».
  2. `INSERT INTO booking (..., price_cents, discount_pct, commission_cents, status, expires_at)
     VALUES (..., <prix courant>, ..., 'held', now() + hold)`.
  Pourquoi c'est sûr : l'UPDATE pose un verrou de ligne ; une transaction concurrente attend,
  puis en READ COMMITTED PostgreSQL ré-évalue le WHERE sur la nouvelle version de la ligne
  (EvalPlanQual) ⇒ `remaining >= $seats` devient faux ⇒ pas de surréservation.
- `POST /api/bookings/{id}/confirm` (paiement simulé) :
  `UPDATE booking SET status='confirmed', expires_at=NULL WHERE id=$1 AND status='held'
  AND expires_at > now() RETURNING slot_id` → sinon 409 (expiré / déjà confirmé) ou 404.
- `DELETE /api/bookings/{id}` : annule un hold et restitue les places (transaction).
- `GET /api/bookings/{id}` : statut, prix figé.
- Job d'expiration : **fait** (`app/jobs.py`). Confirm et expiration s'excluent mutuellement :
  tous deux exigent `status='held'` sur la même ligne.
- Front : modale (nom, contact, places si séance collective) → prix figé et compte à rebours →
  « Payer (simulation) ».
- Test de concurrence à écrire : N requêtes simultanées sur un slot de capacité 1 ⇒ exactement 1 succès.

### Étape 4 — Packaging

- `backend/Dockerfile` (python:3.13-slim, user non-root,
  `uvicorn app.main:app --workers 1 --timeout-graceful-shutdown 2`).
- `docker-compose.yml` : `postgis/postgis:16-3.4` (healthcheck `pg_isready`, volume nommé,
  DB non exposée) + api (`127.0.0.1:8000:8000`, `depends_on: condition: service_healthy`).
  Note : l'image crée l'utilisateur `POSTGRES_USER` superuser → acceptable en proto, à séparer ensuite.
- `README.md` : lancement, reseed avant démo, liens pro, intégration Traefik (HTTPS local) / Tailscale.

### Plus tard (hors proto)

- Statistiques pro « créneaux sauvés / € récupérés ce mois » (l'argument « ≈ 625 € net par mois »
  de l'executive summary), à partir des réservations confirmées.
- Alertes : recherches sauvegardées + Web Push (exige HTTPS ou localhost ; iOS ≥ 16.4 seulement
  si la PWA est ajoutée à l'écran d'accueil). Appli mobile (l'executive summary parle d'« appli »).
- Connexion aux agendas des pros (année 2), cours collectifs (famille `event`, année 2).
- Stripe test mode (PaymentIntent + webhook via `stripe listen --forward-to localhost:8000/...`),
  commission de 7,5 % pendant 3 mois pour les premiers pros.
- Broker multi-instances (LISTEN/NOTIFY), Alembic, comptes commerçants réels, tuiles PMTiles locales.

## Lancer en local (API hors Docker, pour dev)

Base de dev : conteneur PostGIS à la version de la stack (celle de l'étape 4), publié sur
127.0.0.1:5434 uniquement. Les clusters système ne sont pas utilisés : sur cette machine, 5432 est
PostgreSQL 18 (sans PostGIS, `postgres` protégé par mot de passe) et 5433 l'ancien cluster 16
(issu de `pg_upgradecluster`, démarrage manuel, arrêté). Les noms techniques (`creno-db`, rôle et
base `creno`) datent du nom provisoire et restent tels quels.

Une fois (le `.venv` existe déjà) :

```bash
docker run -d --name creno-db \
  -e POSTGRES_USER=creno -e POSTGRES_PASSWORD=creno -e POSTGRES_DB=creno \
  -p 127.0.0.1:5434:5432 -v creno-pgdata:/var/lib/postgresql/data \
  postgis/postgis:16-3.4
docker exec creno-db sh -c 'until pg_isready -q -h 127.0.0.1 -U creno; do sleep 1; done; createdb -U creno creno_test'
# si le .venv n'existe pas : python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
```

Après un redémarrage de la machine : `docker start creno-db`. L'image crée `creno` en superuser :
`apply_schema` crée lui-même les extensions postgis et btree_gist.

Démo (depuis `backend/`) :

```bash
# DATABASE_URL vaut par défaut postgresql://creno:creno@127.0.0.1:5434/creno (la base Docker) ;
# l'exporter seulement pour viser une autre base.
../.venv/bin/python -m app.seed --reset        # recrée le schéma et les données ; avant chaque démo
../.venv/bin/uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 2
# → http://localhost:8000 (client) ; liens commerçants dans demo-tokens.csv
```

Si l'API refuse de démarrer avec « Schéma de base en version 1 » : arrêter uvicorn, lancer
`python -m app.seed --reset`, relancer uvicorn.

Tests (depuis `backend/`, base **jetable** : le seed y recrée tout le schéma) :

```bash
TEST_DATABASE_URL=postgresql://creno:creno@127.0.0.1:5434/creno_test ../.venv/bin/pytest
# sans TEST_DATABASE_URL : seuls les tests unitaires tournent, les autres sont ignorés
```

Note pour Claude Code : son sandbox bloque les sockets Unix et isole le réseau de chaque commande,
il ne peut donc pas joindre `creno-db`. Pour tester, il lance un cluster PostgreSQL 16 jetable
(binaires système + PostGIS 3.7) en TCP, dans la même commande que les tests.
