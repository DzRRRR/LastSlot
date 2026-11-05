# Passation — état au 2026-11-05 (étapes 1 à 4 terminées, alignées sur l'executive summary)

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
3. Un client filtre (secteur, prix max, « dans les X h »), clique le pin, réserve.
4. La place est bloquée 5 min (hold) au prix affiché → « Payer » (simulé) → confirmée.
5. Le créneau disparaît de la carte des autres en temps réel ; le commerçant voit la réservation
   payée, ce qu'il touche et ses « créneaux sauvés » du mois.

Les 5 étapes sont vérifiées de bout en bout dans Chromium (trois navigateurs : commerçant, client
qui réserve, client qui observe).

## Fait

### Socle

- `backend/app/schema.sql` (version 3, voir `SCHEMA_VERSION` dans `db.py`) — catégories, merchant,
  service, resource, slot, booking, avec :
  - `CHECK (remaining >= 0 AND remaining <= capacity)` : garde-fou anti-surréservation.
  - Contrainte d'exclusion `slot_no_resource_overlap` (btree_gist) : un praticien ou un terrain
    ne peut pas avoir deux créneaux ouverts qui se chevauchent.
  - `booking.id` en UUID v4 (capability non énumérable) ; prix unitaire, remise, montant et
    commission figés à la prise de place, `confirmed_at` (cohérence vérifiée par des CHECK).
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

### Étape 3 — Réservation sans double-booking

- `app/api_bookings.py` — sans compte client : l'id de réservation est la clé (capability).
  - `POST /api/bookings` (201) : 404 créneau inconnu, 422 validation (nom 1..80, contact 3..120,
    1..10 places, une seule pour un rendez-vous), 409 avec la raison (plus de place, annulé, déjà
    commencé). Décrément atomique `UPDATE slot … WHERE remaining >= $seats` (verrou de ligne +
    EvalPlanQual) qui lit le prix courant sous le même verrou ; la réservation fige prix, remise,
    montant et commission (15 %) ; publie `slot` change "held" et `booking` status "held".
  - `POST /api/bookings/{id}/confirm` : paiement simulé, `UPDATE … WHERE status = 'held' AND
    expires_at > now()` ; idempotent si déjà payée (double clic) ; 409 expirée / annulée ;
    publie `booking` status "confirmed".
  - `DELETE /api/bookings/{id}` : rend les places (une instruction, réservation puis créneau) ;
    idempotent si expirée ou annulée ; 409 si payée ; publie `slot` "released" + `booking` "cancelled".
  - `GET /api/bookings/{id}` : statut (un hold hors délai est montré `expired`), prix figé,
    `expires_in_s` calculé par le serveur.
- `GET /api/pro/stats` — réservations payées et montant touché (commission déduite) du mois en
  cours, heure de Paris : « Ce mois-ci : N créneaux sauvés · X € récupérés » dans le dashboard.
- `db.retry_on_deadlock` — rejoue une transaction abandonnée pour interblocage (annulation par le
  pro : créneau puis réservations ; job d'expiration et annulation client : réservations puis créneau).
- `static/booking.js` + `<dialog>` dans `index.html` — fenêtre de réservation : coordonnées →
  place bloquée (compte à rebours fondé sur `expires_in_s`, prix figé) → « Payer (simulation) » →
  confirmation (économie, référence). Fermer la fenêtre, ou l'onglet (`pagehide` + `keepalive`),
  rend la place aussitôt.
- Dashboard : toasts « Un client est en train de réserver… » puis « Nouvelle réservation payée ! »,
  montant payé et net par réservation, statut « Vendu » sans bouton d'annulation une fois payé.

### Étape 4 — Packaging

- `backend/Dockerfile` — `python:3.13-slim`, dépendances en couche séparée, code et front
  appartenant à root, utilisateur `lastslot` (uid 10001) sans shell, `HEALTHCHECK` en Python
  (pas de curl dans l'image), `CMD` : `python -m app.seed --if-empty && exec uvicorn …
  --workers 1 --timeout-graceful-shutdown 2` (uvicorn PID 1, reçoit SIGTERM). `.dockerignore` :
  ni tests, ni caches, ni `demo-tokens.csv`.
- `docker-compose.yml` (projet `lastslot`) — `db` : `postgis/postgis:16-3.4`, volume nommé,
  healthcheck `pg_isready -h 127.0.0.1` (le TCP ne répond qu'après l'initialisation), aucun port
  publié ; `api` : `127.0.0.1:${API_PORT:-8000}:8000`, `depends_on: service_healthy`,
  `read_only` + tmpfs `/tmp`, `cap_drop: ALL`, `no-new-privileges`.
- `docker/db-init/20-app-role.sh` — à l'initialisation du volume : rôle `lastslot_app` sans
  privilège de superutilisateur, propriétaire de la base (PostGIS installé par le superutilisateur
  `lastslot_admin` via le script de l'image ; btree_gist est « trusted »). Mot de passe passé par
  variable psql (`:'app_password'`), aucune interpolation shell.
- `docker-compose.traefik.yml` — option HTTPS derrière un Traefik existant (labels, réseau externe,
  `LASTSLOT_HOST` obligatoire). `.env.example` — toutes les variables, valeurs par défaut.
- Seed : `--if-empty` (premier démarrage du conteneur, puis plus rien), `DEMO_TOKENS_FILE`
  (`/tmp/demo-tokens.csv` dans l'image, seul dossier inscriptible), CSV en fins de ligne Unix.
- README : démarrage en une commande, reseed avant démo, récupération des liens
  (`docker compose exec -T api cat /tmp/demo-tokens.csv`), démo sur téléphone via
  `tailscale serve --bg 8000` (HTTPS limité au tailnet ; jamais `funnel`).
- **Validé sans Docker** (le sandbox de Claude Code n'a pas accès au démon) :
  `docker compose config` (base, port surchargé, surcouche Traefik avec et sans `LASTSLOT_HOST`) ;
  script d'init sur un PostgreSQL 16 neuf en scram, avec un mot de passe contenant une
  apostrophe ; `CMD` de l'image rejoué depuis une copie du code en lecture seule, connecté avec
  le rôle non superutilisateur : seed au premier démarrage, `--if-empty` au redémarrage, sonde
  de santé, réservation, connexion pro, `seed --reset` API en marche, aucune écriture dans le
  code, arrêt en ~2 s avec un flux SSE ouvert, `COPY … TO PROGRAM` refusé au rôle ; suite de
  tests sous Python 3.13 (version de l'image). **Reste à valider : un vrai `docker compose up
  --build`.**

### Tests

- 89 tests pytest (`backend/tests/`) : API publique (filtres, validation, prix courants et
  prochain palier, en-têtes), API pro (401, IDOR, attribution, chevauchements, tarif figé,
  validation, annulation, SSE), paliers SQL (bornes H-6 / H-2 incluses, exemple 30 % → 10/20/30),
  refus d'un schéma ancien, annonce temps réel d'un palier franchi, générateur SSE, seed (Paris,
  horaires par secteur, pas de chevauchement, tokens hachés, `--reset`), réservation (prix figé,
  validation, raisons des refus, confirm idempotent, délai dépassé et place rendue par le job,
  annulation, annulation du créneau par le pro, ids invalides, événements SSE, stats) et
  **concurrence** : 20 clients simultanés sur un rendez-vous → exactement 1 succès ; 12 sur un
  cours de 5 places → 5 succès ; invariant places restantes + bloquées = capacité.
- Vérifié dans Chromium headless (scripts hors dépôt) : parcours de démo complet en 5 étapes,
  annulation d'un hold (la place réapparaît chez les autres), conflit entre deux clients
  (« Plus de place »), padel (terrains), nom piège `<img onerror=…>` affiché littéralement
  (liste, popup, fenêtre de réservation), aucune violation CSP, mobile.

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
   (autre lien pro dans le même onglet) ; `[hidden] { display: none !important }`.
7. **Packaging** : rôle PostgreSQL de l'API séparé dès maintenant (le plan le reportait) ;
   conteneur en lecture seule sans capabilities ; seed automatique au premier démarrage
   (`--if-empty`) pour un `docker compose up` directement utilisable ; HTTPS pour la démo via
   Tailscale (aucune configuration) plutôt qu'un Traefik imposé, proposé en option.
8. **Réservation** : confirm idempotent (double clic) au lieu d'un 409 ; raisons de refus
   explicites (plus de place / annulé / déjà commencé / délai dépassé) ; prix figé à la prise de
   place (le client paie ce qu'il a vu, même si un palier tombe pendant les 5 min) ; événements
   `booking` porteurs du statut (held / confirmed / cancelled) pour des notifications précises ;
   statistiques du mois ajoutées dès maintenant (elles n'ont de sens qu'avec les réservations).
9. **Console navigateur** : « Expected value to be of type number, but found null » et quelques
   « Image … could not be loaded » viennent du style OpenFreeMap (présents sans nos couches).

## Points d'attention pour la suite

- Uvicorn en un seul worker : le broker SSE est en mémoire du processus. Plusieurs instances
  exigeraient LISTEN/NOTIFY ou Redis (voir « Plus tard »).
- Schéma versionné (v3), sans migrations : un changement incompatible incrémente
  `SCHEMA_VERSION` et impose `seed --reset` (avec Docker :
  `docker compose run --rm api python -m app.seed --reset`).
- Les mots de passe de `.env` ne sont lus qu'à la création du volume `pgdata`.
- Infra de test réutilisable : `tests/dbutil.py`, fixture `server` (vrai uvicorn, tâches de fond
  toutes les 0,2 s). `HEAD /api/*` répond 404 (FastAPI ne mappe pas HEAD sur GET) : utiliser GET.

## À faire

### Plus tard (hors proto)

- Alertes : recherches sauvegardées + Web Push (exige HTTPS ou localhost ; iOS ≥ 16.4 seulement
  si la PWA est ajoutée à l'écran d'accueil). Appli mobile (l'executive summary parle d'« appli »).
- Connexion aux agendas des pros (année 2), cours collectifs (famille `event`, année 2).
- Stripe test mode (PaymentIntent + webhook via `stripe listen --forward-to localhost:8000/...`),
  commission de 7,5 % pendant 3 mois pour les premiers pros.
- Broker multi-instances (LISTEN/NOTIFY), Alembic, comptes commerçants réels, tuiles PMTiles locales.

## Lancer avec Docker (démo, équipe)

```bash
docker compose up -d --build                         # API + base, données de démo au 1er démarrage
docker compose exec api python -m app.seed --reset   # avant chaque démo (affiche les liens pro)
docker compose down                                  # arrêt (données gardées) ; -v pour tout effacer
```

→ http://localhost:8000. Détails, HTTPS et dépannage : README.

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

Si l'API refuse de démarrer avec « Schéma de base en version N, le code attend la version 3 » :
arrêter uvicorn, lancer `python -m app.seed --reset`, relancer uvicorn.

Tests (depuis `backend/`, base **jetable** : le seed y recrée tout le schéma) :

```bash
TEST_DATABASE_URL=postgresql://creno:creno@127.0.0.1:5434/creno_test ../.venv/bin/pytest
# sans TEST_DATABASE_URL : seuls les tests unitaires tournent, les autres sont ignorés
```

Note pour Claude Code : son sandbox bloque les sockets Unix et isole le réseau de chaque commande,
il ne peut donc pas joindre `creno-db`. Pour tester, il lance un cluster PostgreSQL 16 jetable
(binaires système + PostGIS 3.7) en TCP, dans la même commande que les tests.
