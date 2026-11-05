"""Jeu de données 100 % fictif pour la démo : Paris, secteurs de la V1 (coiffure, beauté, sport
indoor), comme dans l'executive summary.

    cd backend && python -m app.seed --reset

`--reset` supprime et recrée le schéma (tables, vue, fonctions), puis peuple la base.
Les créneaux sont générés relativement à maintenant : relancer avant chaque démo.
Écrit `demo-tokens.csv` (gitignoré, droits 0600) : un lien de connexion par commerçant.
Le token est dans le fragment (#t=...) : le navigateur ne l'envoie jamais au serveur,
il n'apparaît donc ni dans les logs d'accès ni dans un en-tête Referer.
"""
import argparse
import asyncio
import csv
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

from .config import settings
from .db import DatabaseUnavailableError, apply_schema, create_pool, reset_schema
from .security import new_token

PARIS = ZoneInfo("Europe/Paris")  # fournie par le paquet tzdata si l'OS n'a pas de base IANA

# Deux générateurs distincts : le catalogue (noms, lieux, prestations) ne dépend que de la
# graine, les créneaux dépendent en plus de l'heure. Avec un seul générateur, le nombre de
# tirages consommés par les créneaux (qui varie avec l'heure) décalerait les noms d'un run à l'autre.
CATALOG_SEED = 42
SLOTS_SEED = 43

HORIZON = timedelta(hours=36)
# Pas de créneau qui commence dans moins de 30 min : il faut le temps de réserver et de venir.
MIN_LEAD = timedelta(minutes=30)
# Plafonds de remise choisis par les pros (la remise appliquée monte par paliers jusqu'à eux).
MAX_DISCOUNTS = (20, 25, 30, 35, 40, 50)

# Zones de tirage : (quartier, lat, lng, code postal et ville, jitter lat ±, jitter lng ±).
# Boîtes vérifiées, avec 25 m de marge, contre l'eau (Seine, canaux), les parcs et bois, les
# cimetières, les emprises ferroviaires et militaires (polygones OpenStreetMap, tuiles
# OpenFreeMap z14). Certains centres ont été décalés de 300 m au plus pour s'en écarter.
CITY_ANCHORS = [
    ("Marais", 48.861, 2.364, "75003 Paris", 0.00200, 0.00350),
    ("Bastille", 48.853, 2.373, "75011 Paris", 0.00250, 0.00225),
    ("Oberkampf", 48.865, 2.377, "75011 Paris", 0.00225, 0.00350),
    ("Canal Saint-Martin", 48.8705, 2.359, "75010 Paris", 0.00250, 0.00350),
    ("Abbesses", 48.882, 2.3385, "75018 Paris", 0.00225, 0.00350),
    ("Batignolles", 48.887, 2.322, "75017 Paris", 0.00250, 0.00250),
    ("Opéra", 48.8735, 2.332, "75009 Paris", 0.00250, 0.00350),
    ("Saint-Germain", 48.8535, 2.3335, "75006 Paris", 0.00125, 0.00350),
    ("Montparnasse", 48.844, 2.323, "75014 Paris", 0.00250, 0.00350),
    ("Butte-aux-Cailles", 48.8255, 2.353, "75013 Paris", 0.00250, 0.00350),
    ("Passy", 48.8595, 2.2805, "75016 Paris", 0.00250, 0.00350),
    ("Commerce", 48.848, 2.2935, "75015 Paris", 0.00200, 0.00350),
    ("Belleville", 48.8715, 2.38, "75020 Paris", 0.00250, 0.00225),
    ("Nation", 48.849, 2.396, "75012 Paris", 0.00250, 0.00325),
    ("Ternes", 48.879, 2.297, "75017 Paris", 0.00250, 0.00350),
]
# Sport indoor : près des portes, là où se trouvent les centres (terrains couverts).
SPORT_ANCHORS = [
    ("Porte de la Villette", 48.899, 2.3855, "75019 Paris", 0.00050, 0.00200),
    ("Porte d'Ivry", 48.82, 2.3695, "75013 Paris", 0.00250, 0.00350),
    ("Porte de Saint-Cloud", 48.8375, 2.2595, "75016 Paris", 0.00250, 0.00300),
    ("Porte de Bagnolet", 48.866, 2.4065, "75020 Paris", 0.00100, 0.00200),
    ("Porte de Clichy", 48.896, 2.31, "75017 Paris", 0.00200, 0.00225),
    ("Porte de Versailles", 48.8295, 2.288, "75015 Paris", 0.00250, 0.00350),
    ("Bercy", 48.8345, 2.3875, "75012 Paris", 0.00125, 0.00075),
]


@dataclass(frozen=True)
class CategorySpec:
    merchants: int
    services: tuple[tuple[str, int, int], ...]  # (nom, durée en min, prix de base en €)
    opening: tuple[time, time]  # heures d'ouverture, heure de Paris
    step_min: int  # granularité des débuts de créneau
    anchors: tuple  # zones de tirage
    resource_names: tuple[str, ...]  # praticiens (1 à 3 tirés au hasard) ou terrains (1 à 3 premiers)
    names: tuple[str, ...]  # noms de commerces inventés (au moins `merchants`)

    @property
    def numbered_resources(self) -> bool:
        return self.resource_names in (COURTS, TENNIS_COURTS)


FIRST_NAMES = ("Camille", "Inès", "Léa", "Hugo", "Sarah", "Yanis", "Chloé", "Nora", "Lucas",
               "Manon", "Adam", "Jade", "Malik", "Zoé", "Théo", "Lina")
COURTS = ("Terrain 1", "Terrain 2", "Terrain 3")
TENNIS_COURTS = ("Court 1", "Court 2", "Court 3")


def _resources(spec: "CategorySpec", rng: random.Random) -> list[str]:
    count = rng.randint(1, 3)
    if spec.numbered_resources:
        return list(spec.resource_names[:count])  # Terrain 1, Terrain 2… dans l'ordre
    return rng.sample(spec.resource_names, count)

# Répartition calquée sur le marché visé (8 635 coiffeurs, 3 027 instituts, ~360 terrains) en
# gardant assez de sport pour la démo. 55 commerces au total.
CATEGORIES: dict[str, CategorySpec] = {
    "coiffure": CategorySpec(
        16, (("Coupe femme", 45, 65), ("Coupe homme", 30, 35), ("Balayage", 120, 150),
             ("Brushing", 30, 35)),
        (time(9), time(19)), 15, tuple(CITY_ANCHORS), FIRST_NAMES,
        ("Atelier Garance", "Salon Opaline", "Studio Mistral", "Maison Cèdre", "Les Ciseaux de Lou",
         "Atelier Comète", "Salon Velours", "Studio Bergamote", "Maison Alizé", "L'Atelier d'Iris",
         "Salon Ondine", "Studio Saphir", "Coiff' & Thé", "Maison Pivoine", "L'Épi Rebelle",
         "Atelier Faubourg", "Studio Lumen", "Salon des Abbesses")),
    "barbier": CategorySpec(
        6, (("Coupe + barbe", 45, 45), ("Taille de barbe", 20, 22), ("Dégradé", 30, 32)),
        (time(10), time(20)), 15, tuple(CITY_ANCHORS), FIRST_NAMES,
        ("Barbier Sauvage", "Le Coupe-Chou", "Lame & Blaireau", "Barbier du Canal",
         "Le Rasoir Bleu", "Moustache & Cie", "Barbe Noire", "Le Barbier Gaulois")),
    "esthetique": CategorySpec(
        11, (("Soin visage", 60, 75), ("Épilation jambes", 30, 35),
             ("Rehaussement de cils", 60, 65), ("Massage relaxant", 60, 80)),
        (time(10), time(19)), 15, tuple(CITY_ANCHORS), FIRST_NAMES,
        ("Institut Eden", "Peau de Pêche", "Institut Camélia", "Belle Saison", "Institut Aurore",
         "Écrin Beauté", "Institut Lotus", "Cocon d'Ambre", "Bulle de Soie", "Maison Ayur",
         "L'Instant Pur", "Institut Magnolia", "Institut Rive Droite")),
    "ongles": CategorySpec(
        8, (("Semi-permanent", 45, 40), ("Manucure", 30, 30), ("Pose gel", 75, 65)),
        (time(10), time(20)), 15, tuple(CITY_ANCHORS), FIRST_NAMES,
        ("Ongle & Sens", "Vernis Rouge", "Studio Nacre", "Atelier Paillettes", "La Lime d'Or",
         "Nail Bar Opale", "Manucure & Cie", "Studio Corail", "Le Petit Vernis", "Ongles de Paris")),
    "padel": CategorySpec(
        5, (("Terrain padel (4 joueurs)", 90, 72), ("Terrain padel (4 joueurs)", 60, 52)),
        (time(8), time(23)), 30, tuple(SPORT_ANCHORS), COURTS,
        ("Padel Club Rive Gauche", "Le Padel Vertical", "Smash Padel", "Padel Factory Est",
         "Bandeja Club", "Víbora Padel")),
    "foot5": CategorySpec(
        4, (("Terrain foot à 5 (10 joueurs)", 60, 110), ("Terrain foot à 5 (10 joueurs)", 90, 150)),
        (time(10), time(23)), 30, tuple(SPORT_ANCHORS), COURTS,
        ("Five du Canal", "Le Terrain Bleu", "Foot Indoor Paris Est", "Goal Center Nord",
         "Five Arena Sud")),
    "tennis": CategorySpec(
        5, (("Court de tennis couvert", 60, 38), ("Court de squash", 45, 24)),
        (time(8), time(22)), 30, tuple(SPORT_ANCHORS), TENNIS_COURTS,
        ("Tennis Club des Buttes", "Squash Station", "Court Central Paris", "Le Revers",
         "Tie-Break Club", "Smash Indoor")),
}

STREETS = ("rue des Artisans", "passage des Tisserands", "rue du Lavoir", "rue des Glycines",
           "impasse des Tilleuls", "rue de la Fontaine-Vive", "rue des Vergers", "cour de l'Horloge",
           "rue des Ateliers", "passage du Marché-Vert")


class SeedError(Exception):
    pass


@dataclass
class SlotPlan:
    service_idx: int
    resource_idx: int
    starts_at: datetime
    ends_at: datetime
    max_discount_pct: int


@dataclass
class MerchantPlan:
    name: str
    category: str
    address: str
    lat: float
    lng: float
    services: list[tuple[str, int, int]]  # (nom, durée min, prix de base en centimes)
    resources: list[str]
    slots: list[SlotPlan] = field(default_factory=list)


def plan_catalog(rng: random.Random) -> list[MerchantPlan]:
    """Commerces, adresses, prestations et ressources : déterministe pour une graine donnée."""
    # Répartition équilibrée sur les zones de chaque groupe (quartiers / portes), ordre aléatoire.
    slots_by_group: dict[tuple, list] = {}
    for spec in CATEGORIES.values():
        slots_by_group.setdefault(spec.anchors, [])
    for anchors in slots_by_group:
        total = sum(s.merchants for s in CATEGORIES.values() if s.anchors == anchors)
        cycle = [anchors[i % len(anchors)] for i in range(total)]
        rng.shuffle(cycle)
        slots_by_group[anchors] = cycle

    merchants = []
    for category, spec in CATEGORIES.items():
        for name in rng.sample(spec.names, spec.merchants):
            _, lat, lng, city, lat_jitter, lng_jitter = slots_by_group[spec.anchors].pop()
            merchants.append(MerchantPlan(
                name=name,
                category=category,
                address=f"{rng.randint(1, 120)} {rng.choice(STREETS)}, {city}",
                lat=round(lat + rng.uniform(-lat_jitter, lat_jitter), 6),
                lng=round(lng + rng.uniform(-lng_jitter, lng_jitter), 6),
                services=[(n, d, euros * 100) for n, d, euros in spec.services],
                resources=_resources(spec, rng),
            ))
    return merchants


def slot_starts(now: datetime, duration: timedelta, spec: CategorySpec) -> list[datetime]:
    """Débuts possibles au pas de la catégorie, dans [now + 30 min, now + 36 h], pendant les
    heures d'ouverture (heure de Paris), prestation terminée avant la fermeture."""
    earliest, latest = now + MIN_LEAD, now + HORIZON
    step = timedelta(minutes=spec.step_min)
    starts = []
    day = earliest.astimezone(PARIS).date()
    while datetime.combine(day, spec.opening[0], tzinfo=PARIS) <= latest:
        start = datetime.combine(day, spec.opening[0], tzinfo=PARIS)
        closing = datetime.combine(day, spec.opening[1], tzinfo=PARIS)
        while start + duration <= closing:
            if earliest <= start <= latest:
                starts.append(start)
            start += step
        day += timedelta(days=1)
    return starts


def _overlaps(start: datetime, end: datetime, taken: list[tuple[datetime, datetime]]) -> bool:
    # Intervalles semi-ouverts [début, fin), comme tstzrange dans la contrainte d'exclusion.
    return any(start < t_end and t_start < end for t_start, t_end in taken)


def plan_slots(merchants: list[MerchantPlan], now: datetime, rng: random.Random) -> None:
    """0 à 2 créneaux par praticien ou terrain, sans chevauchement."""
    for m in merchants:
        spec = CATEGORIES[m.category]
        for resource_idx in range(len(m.resources)):
            taken: list[tuple[datetime, datetime]] = []
            for _ in range(rng.randint(0, 2)):
                service_idx = rng.randrange(len(m.services))
                duration = timedelta(minutes=m.services[service_idx][1])
                free = [s for s in slot_starts(now, duration, spec)
                        if not _overlaps(s, s + duration, taken)]
                if not free:
                    continue
                start = rng.choice(free)
                taken.append((start, start + duration))
                m.slots.append(SlotPlan(service_idx, resource_idx, start, start + duration,
                                        rng.choice(MAX_DISCOUNTS)))


def build_plan(now: datetime) -> list[MerchantPlan]:
    merchants = plan_catalog(random.Random(CATALOG_SEED))
    plan_slots(merchants, now, random.Random(SLOTS_SEED))
    return merchants


async def _insert(conn: asyncpg.Connection, merchants: list[MerchantPlan],
                  base_url: str) -> list[tuple[int, str, str, str]]:
    links = []
    for m in merchants:
        token, token_hash = new_token()
        merchant_id = await conn.fetchval(
            """INSERT INTO merchant (name, category_code, address, location, token_hash)
               VALUES ($1, $2, $3, ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $6)
               RETURNING id""",
            m.name, m.category, m.address, m.lng, m.lat, token_hash,
        )
        service_ids = [
            await conn.fetchval(
                """INSERT INTO service (merchant_id, name, duration_min, base_price_cents)
                   VALUES ($1, $2, $3, $4) RETURNING id""",
                merchant_id, name, duration, base_cents,
            )
            for name, duration, base_cents in m.services
        ]
        resource_ids = [
            await conn.fetchval(
                "INSERT INTO resource (merchant_id, name) VALUES ($1, $2) RETURNING id",
                merchant_id, name,
            )
            for name in m.resources
        ]
        await conn.executemany(
            """INSERT INTO slot (merchant_id, service_id, resource_id, starts_at, ends_at,
                                 capacity, remaining, base_price_cents, max_discount_pct)
               VALUES ($1, $2, $3, $4, $5, 1, 1, $6, $7)""",
            [
                (merchant_id, service_ids[s.service_idx], resource_ids[s.resource_idx],
                 s.starts_at, s.ends_at, m.services[s.service_idx][2], s.max_discount_pct)
                for s in m.slots
            ],
        )
        links.append((merchant_id, m.name, m.category, f"{base_url}/pro.html#t={token}"))
    return links


async def seed(dsn: str, reset: bool, base_url: str,
               now: datetime | None = None) -> tuple[list[tuple[int, str, str, str]], int]:
    """Peuple la base ; renvoie (liens commerçants, nombre de créneaux)."""
    merchants = build_plan(now or datetime.now(timezone.utc))
    pool = await create_pool(dsn)
    try:
        if reset:
            await reset_schema(pool)
        else:
            await apply_schema(pool)
        # Une seule transaction : en cas d'erreur, la base reste dans son état précédent.
        async with pool.acquire() as conn, conn.transaction():
            if await conn.fetchval("SELECT EXISTS (SELECT FROM merchant)"):
                raise SeedError("la base contient déjà des commerces : relancer avec --reset")
            codes = {r["code"] for r in await conn.fetch("SELECT code FROM category")}
            if missing := set(CATEGORIES) - codes:
                raise SeedError(f"catégories absentes de schema.sql : {sorted(missing)}")
            links = await _insert(conn, merchants, base_url)
    finally:
        await pool.close()
    return links, sum(len(m.slots) for m in merchants)


def write_tokens(path: Path, links: list[tuple[int, str, str, str]]) -> None:
    # Le fichier contient des secrets (liens de connexion) : lisible par son seul propriétaire.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # O_CREAT n'applique le mode qu'à la création d'un nouveau fichier
    with open(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "nom", "categorie", "lien"])
        writer.writerows(links)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed",
        description="Peuple la base avec des données fictives de démonstration (Paris).",
    )
    parser.add_argument("--reset", action="store_true",
                        help="supprime et recrée tout le schéma (données comprises) avant")
    parser.add_argument("--tokens-out", type=Path, default=Path("demo-tokens.csv"),
                        help="CSV des liens commerçants (défaut : ./demo-tokens.csv)")
    parser.add_argument("--base-url", default=settings.public_base_url,
                        help="URL de l'app dans les liens (défaut : $PUBLIC_BASE_URL "
                             "ou http://localhost:8000)")
    args = parser.parse_args(argv)

    try:
        links, n_slots = asyncio.run(
            seed(settings.database_url, args.reset, args.base_url.rstrip("/"))
        )
    except (SeedError, DatabaseUnavailableError) as exc:
        print(f"seed : {exc}", file=sys.stderr)
        return 1

    for merchant_id, name, category, link in links:
        print(f"{merchant_id:>3}  {category:<10}  {name:<28}  {link}")
    write_tokens(args.tokens_out, links)
    print(f"\n{len(links)} commerces, {n_slots} créneaux. "
          f"Liens écrits dans {args.tokens_out} (droits 0600).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
