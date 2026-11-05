# Last Slot — prototype

> **Une annulation, une opportunité.** Un client annule ? Le pro remet le créneau en vente sur
> Last Slot, moins cher. Un client du quartier le réserve. Le pro ne perd plus d'argent, le client
> paie moins cher.

Prototype de démonstration du projet Last Slot (Challenge Entreprendre 2026, IMT-BS, Télécom
SudParis, ENSIIE). Il illustre la V1 décrite dans l'executive summary : **Paris**, **coiffure,
beauté et sport indoor**, **remise progressive** plafonnée par le pro, **15 % de commission**
sans abonnement.

> ⚠️ Toutes les données sont **fictives** (commerces, adresses, prix) et générées pour la démo.

---

## Sommaire

1. [Ce que fait le prototype](#ce-que-fait-le-prototype)
2. [Démarrage rapide avec Docker](#démarrage-rapide-avec-docker)
3. [Montrer la démo sur un téléphone (HTTPS)](#montrer-la-démo-sur-un-téléphone-https)
4. [Naviguer dans l'application](#naviguer-dans-lapplication)
5. [Script de démo (2 minutes)](#script-de-démo-2-minutes)
6. [La remise progressive](#la-remise-progressive)
7. [Dépannage](#dépannage)
8. [Pour les développeurs](#pour-les-développeurs)
9. [Feuille de route](#feuille-de-route)

---

## Ce que fait le prototype

| Côté | Aujourd'hui | Pas encore |
|---|---|---|
| **Client** (carte) | Voir les créneaux libérés autour de soi sur une carte de Paris, filtrer par secteur, prix et délai, voir la remise du moment et la suivante, **réserver** (place bloquée 5 min au prix affiché, paiement simulé), mises à jour en direct | Vrai paiement (Stripe), compte client, alertes |
| **Commerçant** (espace pro) | Remettre un créneau en vente en 3 actions, voir l'échéancier de la remise et ce qu'il touche, suivre et annuler ses créneaux, **voir les réservations en direct** et ses « créneaux sauvés » du mois | Connexion à son agenda, vrais comptes |

55 commerces fictifs : coiffeurs, barbiers, instituts de beauté, onglerie (dans les quartiers),
padel, foot à 5, tennis et squash (près des portes de Paris), avec une centaine de créneaux dans
les 36 prochaines heures.

---

## Démarrage rapide avec Docker

Il faut seulement **git**, **Docker** (Docker Desktop sur Windows et macOS) et une connexion
internet (le fond de carte vient d'OpenFreeMap). Pas besoin d'installer Python.

```bash
git clone https://github.com/DzRRRR/LastSlot.git
cd LastSlot
docker compose up -d --build
```

Le premier lancement télécharge les images et construit l'application (quelques minutes).
`docker compose ps` doit ensuite afficher `api` et `db` à l'état **healthy**. Au premier
démarrage, l'application crée seule les données de démo.

| Page | Adresse |
|---|---|
| Carte client | http://localhost:8000 |
| Espace commerçant | http://localhost:8000/pro.html (avec un lien commerçant, voir ci-dessous) |
| Documentation de l'API | http://localhost:8000/docs |

**Avant chaque démo**, régénérer les données : les créneaux sont calculés à partir de l'heure
actuelle. La commande affiche les liens de connexion des 55 commerçants.

```bash
docker compose exec api python -m app.seed --reset
```

Pour garder les liens dans un fichier (`-T` : sortie brute, sans terminal) :

```bash
docker compose exec -T api cat /tmp/demo-tokens.csv > demo-tokens.csv
```

Autres commandes utiles :

| Besoin | Commande |
|---|---|
| Voir les logs (et les liens du premier démarrage) | `docker compose logs api` |
| Arrêter (les données sont gardées) | `docker compose down` |
| Tout effacer, base comprise | `docker compose down -v` |
| Mettre à jour après un `git pull` | `docker compose up -d --build` |

Réglages (port, commission, durée de blocage, mots de passe de la base) : copier `.env.example`
en `.env` et l'adapter. Tout fonctionne sans `.env`.

Sécurité : l'application n'est accessible que depuis votre ordinateur (127.0.0.1). La base n'est
pas accessible hors de Docker. L'application tourne sans droits d'administration, sur un
système de fichiers en lecture seule.

---

## Montrer la démo sur un téléphone (HTTPS)

Sur un téléphone, l'application doit être servie en **HTTPS** (indispensable pour « Autour de
moi »), sans jamais l'exposer sur Internet. Le plus simple : **Tailscale**, installé sur
l'ordinateur et sur le téléphone, connectés au même compte.

```bash
tailscale serve --bg 8000
```

L'application est alors disponible sur `https://<nom-de-la-machine>.<tailnet>.ts.net`, pour les
appareils de votre tailnet uniquement. Tailscale propose d'activer HTTPS au premier lancement.
Pour que les liens commerçants utilisent cette adresse, ajouter dans `.env` :
`PUBLIC_BASE_URL=https://<nom-de-la-machine>.<tailnet>.ts.net`, puis
`docker compose up -d` et `docker compose exec api python -m app.seed --reset`. Pour arrêter le
partage : `tailscale serve reset`.

> N'utilisez pas `tailscale funnel` : il publierait l'application sur Internet.

Variante pour un homelab avec **Traefik** : `docker-compose.traefik.yml` ajoute les labels
nécessaires. Il faut renseigner `LASTSLOT_HOST` (et au besoin `TRAEFIK_NETWORK`,
`TRAEFIK_ENTRYPOINT`) dans `.env`, puis :
`docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d --build`.

---

## Naviguer dans l'application

### Carte client — http://localhost:8000

- **Carte** : les ronds bleu nuit regroupent plusieurs commerces (le chiffre = nombre de
  créneaux) ; cliquez dessus pour zoomer. Chaque commerce est un point coloré selon son secteur,
  avec sa meilleure remise du moment (« -30% »).
- **Liste** (à gauche, ou sous la carte sur mobile) : tous les créneaux de la zone affichée,
  triés par heure, avec prix barré, prix remisé, remise du moment et prochain palier
  (« Puis -30 % dès 16:45 »). Cliquez sur un créneau pour centrer la carte sur le commerce.
- **Filtres** :
  - secteurs (plusieurs possibles ; aucun coché = tous) ;
  - **Prix max** (sur le prix actuel ; tout à droite = tous les prix) ;
  - **Quand** : dans les 2, 6, 24 ou 48 heures.
- **Autour de moi** : centre la carte sur votre position et affiche les distances (le
  navigateur demande l'autorisation).
- **Fiche commerce** (clic sur un point) : ses créneaux et le bouton « Réserver ».
- **Réserver** :
  1. nom et e-mail (ou téléphone), puis « Bloquer ma place » ;
  2. la place est **bloquée 5 minutes** au prix affiché (garanti même si la remise change
     entre-temps) ; un compte à rebours s'affiche, et le créneau disparaît aussitôt de la carte
     des autres clients ;
  3. « Payer » (paiement **simulé**, aucune donnée bancaire) : réservation confirmée, avec
     l'économie réalisée et une référence.

  « Annuler » ou fermer la fenêtre rend la place tout de suite ; passé 5 minutes sans payer, elle
  est libérée automatiquement. Si quelqu'un a réservé juste avant vous : « Plus de place ».
- **Temps réel** (pastille verte en haut) : nouveaux créneaux, annulations et baisses de prix
  apparaissent sans recharger la page.
- Pour voir **toutes les disponibilités** : dézoomez sur tout Paris, aucun secteur coché, prix
  max tout à droite, « Dans les 48 h ».

### Espace commerçant — http://localhost:8000/pro.html

**Se connecter.** Pas de mot de passe dans le prototype : chaque commerce fictif a un **lien
personnel**, régénéré à chaque `seed --reset`. La commande de reset les affiche ; avec Docker, on
peut aussi les récupérer dans un fichier (`docker compose exec -T api cat /tmp/demo-tokens.csv >
demo-tokens.csv`), sans Docker ils sont dans `backend/demo-tokens.csv` (une ligne par commerce :
id, nom, secteur, lien). Au choix :

- ouvrir le lien dans le navigateur ;
- ou aller sur la carte, cliquer « Vous êtes un pro ? Remettez un créneau en vente → », et
  coller le lien (ou seulement ce qui suit `#t=`) dans le champ.

Pour lister les liens dans un terminal (Linux / macOS) :

```bash
column -s, -t < demo-tokens.csv | less -S
```

Sous Windows : ouvrir `demo-tokens.csv` dans un éditeur de texte ou Excel.

**Remettre un créneau en vente**, en 3 actions :

1. **Prestation** (ex. « Coupe homme · 30 min · 35 € »).
2. **Début** : « Dans 30 min », « Dans 1 h », « Dans 2 h », ou date et heure précises. Le
   praticien (ou le terrain, pour le sport) est choisi automatiquement parmi ceux qui sont libres,
   ou imposé dans la liste.
3. **Remise maximale** : -20 %, -30 %, -40 %, -50 % ou une valeur entre 5 et 80 %.

L'aperçu affiche l'**échéancier de la remise**, le **prix client** et **ce que vous touchez**
(après 15 % de commission) à chaque palier. « Publier le créneau » : il apparaît aussitôt dans
« Mes créneaux » et sur la carte client.

**Mes créneaux** : statut (En vente, Complet, Vendu, Passé, Annulé), remise du moment,
plafond, prochain palier, montant touché, réservations (nom, contact, « paiement en cours » puis
« payée », montant payé et ce que vous touchez), bouton « Annuler le créneau » (absent une fois
une réservation payée ; une place seulement bloquée est rendue au client).

**En direct** : « Un client est en train de réserver… » puis « Nouvelle réservation payée ! »,
et sous le nom du commerce, le résultat du mois : « Ce mois-ci : 1 créneau sauvé · 26,77 €
récupérés » (montant touché, commission déduite).

Changer de commerce : « Se déconnecter », ou ouvrir un autre lien dans le même onglet.

---

## Script de démo (2 minutes)

Préparation : `docker compose exec api python -m app.seed --reset` le jour même, **deux fenêtres
côte à côte** : la carte client et l'espace d'un commerçant (par exemple un barbier). Une troisième
fenêtre client, ou un téléphone via [Tailscale](#montrer-la-démo-sur-un-téléphone-https), permet
de montrer qu'un créneau réservé disparaît chez les autres.

1. **Le problème** (carte client) : « Près de 8 rendez-vous sur 100 sont annulés au dernier
   moment. Voici les créneaux libérés à Paris, coiffure, beauté et sport, dans une seule appli. »
   Montrer les filtres et un point sur la carte.
2. **Un client vient d'annuler** (espace commerçant) : prestation, « Dans 2 h », remise maximale
   -30 %. « Le pro remet le créneau en vente en 10 secondes. » Montrer l'échéancier : -20 %
   maintenant, -30 % à H-2, et ce qu'il touche.
3. **Publier** : le créneau apparaît **instantanément** sur la carte client, sans recharger.
4. **La remise monte toute seule** : sur la carte, « Puis -30 % dès … ». « Zéro gestion pour le
   pro, il fixe seulement le plafond. »
5. **Un client du quartier réserve** (carte client) : clic sur le point, « Réserver », nom et
   e-mail, « Bloquer ma place » : compte à rebours de 5 min, prix garanti. Le créneau disparaît
   des autres cartes ; côté pro, « Un client est en train de réserver… ».
6. **Il paie** (simulation) : « Réservation confirmée », avec l'économie réalisée. Côté pro :
   « Nouvelle réservation payée ! », le nom du client, ce que le pro touche, et « Ce mois-ci :
   1 créneau sauvé · … € récupérés ».
7. **Le modèle** : « 0 € d'abonnement, 15 % de commission uniquement si le créneau est vendu »
   (affiché sous le bouton « Publier » et dans chaque montant touché).

---

## La remise progressive

Le pro fixe un **plafond** de remise. La remise appliquée augmente à l'approche du rendez-vous :

| Moment | Remise appliquée | Exemple, plafond 30 % | Prix d'une coupe à 35 € |
|---|---|---|---|
| Plus de 6 h avant | 1/3 du plafond | -10 % | 31,50 € |
| Entre 6 h et 2 h avant | 2/3 du plafond | -20 % | 28 € |
| Moins de 2 h avant | le plafond | -30 % | 24,50 € |

Les paliers sont arrondis à 5 %, avec un minimum de 5 %. Le pro touche le prix payé moins 15 %
de commission ; il ne paie rien si le créneau n'est pas vendu.

---

## Dépannage

| Problème | Solution |
|---|---|
| `docker compose ps` : `api` n'est pas *healthy* | Lire `docker compose logs api` : le message d'erreur y figure (voir les lignes suivantes). |
| « Schéma de base en version N » (après une mise à jour) | La structure de la base a changé. Docker : `docker compose run --rm api python -m app.seed --reset` puis `docker compose up -d`. Sans Docker : arrêter le serveur, `python -m app.seed --reset`, relancer. |
| `port is already allocated` ou `address already in use` | Un autre serveur occupe le port 8000 (par exemple uvicorn lancé à la main) : l'arrêter, ou mettre `API_PORT=8001` dans `.env` (puis http://localhost:8001). |
| `seed : connexion impossible à postgresql://creno:***@127.0.0.1:5434/creno` (sans Docker) | La base de développement ne tourne pas : `docker start creno-db` (ou la créer, voir [Lancer sans Docker](#lancer-sans-docker-rechargement-à-chaud)). |
| Espace commerçant : « Lien invalide ou expiré » | Les liens changent à chaque `seed --reset` : prendre un lien dans le `demo-tokens.csv` actuel. |
| Réservation : « Plus de place » | Quelqu'un a bloqué ou réservé ce créneau juste avant : en choisir un autre. |
| Réservation : « Délai de paiement dépassé » | Plus de 5 minutes sans payer : la place a été rendue, recommencer la réservation. |
| Aucun créneau sur la carte | Régénérer les données (`docker compose exec api python -m app.seed --reset`) : les créneaux passés disparaissent. Dézoomer, vider les filtres. |
| La carte reste grise | Le fond de carte vient d'internet (OpenFreeMap) : vérifier la connexion. |
| La page n'a pas changé après une mise à jour du code | Recharger avec `Ctrl+Shift+R`. |
| « Autour de moi » ne fait rien | Autoriser la géolocalisation dans le navigateur. Les données couvrent Paris : ailleurs, la zone est vide (« Recentrer sur Paris »). |

---

## Pour les développeurs

### Lancer sans Docker (rechargement à chaud)

Pour modifier le code : l'API tourne sur la machine (Python 3.12+), la base dans un conteneur.
À faire une fois :

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
docker run -d --name creno-db \
  -e POSTGRES_USER=creno -e POSTGRES_PASSWORD=creno -e POSTGRES_DB=creno \
  -p 127.0.0.1:5434:5432 -v creno-pgdata:/var/lib/postgresql/data \
  postgis/postgis:16-3.4
```

Sous Windows (PowerShell), écrire la commande `docker run` sur une seule ligne, sans les `\`
(et si l'activation est refusée : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`). Mac à
puce Apple : si Docker signale une architecture incompatible, ajouter `--platform linux/amd64`.

À chaque session, environnement activé, depuis `backend/` (arrêter d'abord le conteneur `api`
de Docker Compose s'il occupe le port 8000) :

```bash
cd backend
python -m app.seed --reset         # liens commerçants dans backend/demo-tokens.csv
uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 2
```

Après un redémarrage de l'ordinateur : `docker start creno-db`.

**Stack** : Python 3.12+, FastAPI, asyncpg (SQL brut), PostgreSQL 16 + PostGIS ; front sans build
(HTML + JavaScript, modules ES), carte MapLibre GL JS ; temps réel par Server-Sent Events.

```
backend/
├── app/
│   ├── main.py          application : démarrage, en-têtes de sécurité, fichiers statiques
│   ├── api_public.py    API client : config, catégories, créneaux, flux temps réel
│   ├── api_pro.py       API commerçant : profil, créneaux (publier, annuler), stats
│   ├── api_bookings.py  API réservation : bloquer une place, payer (simulé), annuler
│   ├── jobs.py          tâches de fond : places bloquées expirées, paliers de remise
│   ├── seed.py          données fictives de démo (Paris)
│   ├── schema.sql       schéma de la base, calcul de la remise progressive
│   └── config.py, db.py, security.py, broker.py
├── static/              index.html + app.js + booking.js (client), pro.html + pro.js
│                        (commerçant), common.js, CSS, MapLibre
└── tests/               tests automatisés (pytest)
docker-compose.yml       API + base PostGIS (docker-compose.traefik.yml : option HTTPS)
backend/Dockerfile       image de l'API (Python 3.13, utilisateur non privilégié)
HANDOFF.md               état détaillé, choix techniques, prochaines étapes
CLAUDE.md                conventions et règles de sécurité du projet
```

**Tests**, depuis `backend/` avec l'environnement activé. Ils utilisent une base séparée
`creno_test`, que les tests effacent :

```bash
docker exec creno-db createdb -U creno creno_test    # une seule fois
TEST_DATABASE_URL=postgresql://creno:creno@127.0.0.1:5434/creno_test pytest
```

Sous Windows (PowerShell) :
`$env:TEST_DATABASE_URL="postgresql://creno:creno@127.0.0.1:5434/creno_test"; pytest`.
Sans `TEST_DATABASE_URL`, seuls les tests qui n'ont pas besoin de base tournent.

**Configuration** (variables d'environnement, toutes facultatives) :

| Variable | Défaut | Rôle |
|---|---|---|
| `DATABASE_URL` | `postgresql://creno:creno@127.0.0.1:5434/creno` | Base de données |
| `COMMISSION_PCT` | `15` | Commission affichée aux pros |
| `HOLD_SECONDS` | `300` | Durée de blocage d'une place avant paiement |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Adresse utilisée dans les liens commerçants |
| `MAP_STYLE_URL` | style OpenFreeMap « liberty » | Fond de carte |
| `APP_NAME` | `Last Slot` | Nom affiché |
| `CSP_ENABLED` | `true` | En-tête de sécurité Content-Security-Policy |
| `DEMO_TOKENS_FILE` | `demo-tokens.csv` (`/tmp/demo-tokens.csv` dans l'image) | Fichier des liens commerçants écrit par le seed |

Avec Docker Compose, les variables se règlent dans `.env` (voir `.env.example`) : `API_PORT`,
`PUBLIC_BASE_URL`, `HOLD_SECONDS`, `COMMISSION_PCT`, `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`.
L'API s'y connecte avec un rôle PostgreSQL sans droits de superutilisateur
(`docker/db-init/20-app-role.sh`).

**Avant de pousser** : lancer les tests, et respecter les règles de `CLAUDE.md` : jamais
d'`innerHTML` avec des données, routes `/api/pro/*` toujours filtrées par le commerce du token,
données de démo 100 % fictives. Le détail des choix techniques est dans `HANDOFF.md`.

---

## Feuille de route

- [x] **Étape 1** : carte client, API publique, données de démo
- [x] **Étape 2** : espace commerçant (publication, annulation, temps réel)
- [x] Alignement sur l'executive summary : Paris, secteurs de la V1, remise progressive, commission
- [x] **Étape 3** : réservation (place bloquée 5 min, paiement simulé, prix figé, aucune
      double réservation même avec 20 clients simultanés), statistiques « créneaux sauvés »
- [x] **Étape 4** : lancement en un seul `docker compose up`, conteneurs durcis, HTTPS pour
      la démo sur téléphone (Tailscale, ou Traefik)
- [ ] Plus tard : alertes aux clients du quartier, paiement Stripe, appli mobile, connexion aux
      agendas des pros
