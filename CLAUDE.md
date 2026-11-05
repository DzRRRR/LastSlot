# Last Slot — prototype

« Une annulation, une opportunité. » Plateforme qui permet aux pros de remettre en vente, moins
cher, les créneaux libérés par des annulations tardives ; un client du quartier les réserve.
V1 (executive summary de l'équipe, `../ExecutiveSummary.pdf`) : Paris ; coiffure, beauté et sport
indoor ; remise progressive avec plafond fixé par le pro ; 15 % de commission, sans abonnement.
Interface client : carte interactive + filtres. Prototype de démo pour le Challenge Entreprendre
(TSP/IMT-BS/ENSIIE) ; le business plan est géré par les coéquipiers, ce dépôt ne couvre que la
technique. L'executive summary fait foi sur le produit (le CDC initial est plus ancien).

L'état d'avancement et le plan détaillé sont dans @HANDOFF.md — le lire avant toute modification.

## Conventions de travail

- Répondre en français ; garder les termes techniques anglais usuels.
- Expliquer toute commande, ligne de code ou config non évidente, avec ses flags importants.
  Ne pas détailler les commandes UNIX basiques.
- Préciser les hypothèses de version quand le comportement en dépend.
- Code complet et exécutable, pas de fragments.
- Jamais de liens symboliques (configs, fichiers, Docker).
- Services accessibles en local uniquement (ou via Tailscale), jamais exposés publiquement ;
  HTTPS en local. Les ports Docker se bindent sur 127.0.0.1.
- Procéder étape par étape ; chaque étape doit rester démontrable seule.

## Stack (figée)

- Python 3.12+ / FastAPI / asyncpg (SQL brut, pas d'ORM pour le proto).
- PostgreSQL 16 + PostGIS 3.4 + btree_gist. Schéma idempotent dans `backend/app/schema.sql`,
  appliqué au démarrage, versionné (`SCHEMA_VERSION` dans `db.py`) ; pas d'Alembic pour le proto :
  un changement incompatible incrémente la version et `python -m app.seed --reset` recrée tout.
- Front sans build : HTML + JS vanilla, MapLibre GL JS 5.24.0 vendorisé dans
  `backend/static/vendor/` (pas de CDN), servi par FastAPI (`StaticFiles`).
- Fond de carte : style OpenFreeMap (`MAP_STYLE_URL`), remplaçable par un PMTiles auto-hébergé.
- Temps réel : SSE (broker en mémoire → 1 seul worker uvicorn).
- Paiement : simulé pour l'instant (endpoint confirm) ; Stripe test mode plus tard.

## Règles de sécurité à respecter

- Jamais d'`innerHTML` avec des données (noms de commerces, clients) : construire le DOM via
  `textContent` / `createElement` (anti-XSS).
- Toute route `/api/pro/*` filtre par `merchant_id` issu du token (anti-IDOR).
- Tokens commerçants : seul le SHA-256 est stocké.
- Données de seed 100 % fictives (pas de vrais commerces) ; bandeau « données de démonstration ».
