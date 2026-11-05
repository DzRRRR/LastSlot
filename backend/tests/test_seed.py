"""Seed : règles de génération (sans base), puis CLI complète contre une base de test."""
import csv
import hashlib
import os
import stat
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import seed
from app.db import SCHEMA_VERSION, masked_dsn
from tests.dbutil import TEST_DATABASE_URL, db_call, requires_db

PARIS = ZoneInfo("Europe/Paris")
BACKEND_DIR = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)  # 16 h à Paris


def catalog_key(plan: list[seed.MerchantPlan]) -> list[tuple]:
    return [(m.name, m.category, m.address, m.lat, m.lng, m.services, m.resources) for m in plan]


def test_catalog_does_not_depend_on_time() -> None:
    later = NOW + timedelta(hours=7, minutes=13)
    assert catalog_key(seed.build_plan(NOW)) == catalog_key(seed.build_plan(later))


def test_plan_is_reproducible() -> None:
    assert seed.build_plan(NOW) == seed.build_plan(NOW)


def test_catalog_shape() -> None:
    plan = seed.build_plan(NOW)
    assert len(plan) == 55
    assert Counter(m.category for m in plan) == {c: s.merchants for c, s in seed.CATEGORIES.items()}
    assert len({m.name for m in plan}) == 55
    for m in plan:
        spec = seed.CATEGORIES[m.category]
        # Dans la zone de tirage d'une ancre du bon groupe (quartiers ou portes), à Paris.
        assert any(abs(m.lat - lat) <= lat_j and abs(m.lng - lng) <= lng_j
                   for _, lat, lng, _, lat_j, lng_j in spec.anchors), m.name
        assert m.address.endswith("Paris")
        assert 1 <= len(m.resources) <= 3
        if spec.numbered_resources:
            assert m.resources == list(spec.resource_names[:len(m.resources)])


@pytest.mark.parametrize("now", [
    NOW,
    datetime(2026, 11, 4, 21, 30, tzinfo=timezone.utc),  # 22 h 30 : seul le sport ouvre encore
    datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),  # nuit du passage à l'heure d'été
])
def test_slots_follow_rules(now: datetime) -> None:
    plan = seed.build_plan(now)
    assert sum(len(m.slots) for m in plan) > 40
    for m in plan:
        spec = seed.CATEGORIES[m.category]
        per_resource = defaultdict(list)
        for s in m.slots:
            name, duration, base_cents = m.services[s.service_idx]
            assert now + seed.MIN_LEAD <= s.starts_at <= now + seed.HORIZON
            assert s.ends_at - s.starts_at == timedelta(minutes=duration)
            assert s.max_discount_pct in seed.MAX_DISCOUNTS
            start, end = s.starts_at.astimezone(PARIS), s.ends_at.astimezone(PARIS)
            assert start.time() >= spec.opening[0] and end.time() <= spec.opening[1], m.name
            assert end.date() == start.date() and start.minute % spec.step_min == 0
            per_resource[s.resource_idx].append((s.starts_at, s.ends_at))
        # Pas de chevauchement par praticien ou par terrain.
        for intervals in per_resource.values():
            intervals.sort()
            assert all(end <= nxt for (_, end), (nxt, _) in zip(intervals, intervals[1:]))


def test_masked_dsn_hides_password() -> None:
    assert masked_dsn("postgresql://creno:s3cret@127.0.0.1:5434/creno") == \
        "postgresql://creno:***@127.0.0.1:5434/creno"
    assert masked_dsn("postgresql://127.0.0.1/creno") == "postgresql://127.0.0.1/creno"


def test_seed_reports_unreachable_database_without_password() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "app.seed", "--reset"],
        cwd=BACKEND_DIR, env={**os.environ, "DATABASE_URL": "postgresql://creno:s3cret@127.0.0.1:1/creno"},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr
    assert "DATABASE_URL" in proc.stderr and "127.0.0.1:1" in proc.stderr
    assert "s3cret" not in proc.stderr and "***" in proc.stderr


@requires_db
def test_seed_cli_recreates_schema_and_writes_tokens(seeded: dict) -> None:
    tokens_file: Path = seeded["tokens"]
    assert stat.S_IMODE(tokens_file.stat().st_mode) == 0o600
    with tokens_file.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 55
    assert all(row["lien"].startswith("http://demo.test/pro.html#t=") for row in rows)
    tokens = {int(row["id"]): row["lien"].split("#t=", 1)[1] for row in rows}

    async def fetch(conn):
        hashes = await conn.fetch(
            "SELECT id, token_hash FROM merchant WHERE id = ANY($1::bigint[])", list(tokens))
        slots = await conn.fetch(
            """SELECT s.base_price_cents, sv.base_price_cents AS service_price, s.max_discount_pct
               FROM slot s JOIN service sv ON sv.id = s.service_id
               WHERE s.merchant_id = ANY($1::bigint[])""", list(tokens))
        version = await conn.fetchval("SELECT version FROM schema_version")
        return {r["id"]: bytes(r["token_hash"]) for r in hashes}, slots, version

    stored, slots, version = db_call(fetch)
    assert version == SCHEMA_VERSION
    # Seul le SHA-256 du token est en base, et il correspond au lien distribué.
    assert stored == {i: hashlib.sha256(t.encode()).digest() for i, t in tokens.items()}
    assert len(slots) > 40
    assert all(s["base_price_cents"] == s["service_price"] for s in slots)  # prix figé = tarif
    assert {s["max_discount_pct"] for s in slots} <= set(seed.MAX_DISCOUNTS)
    assert f"55 commerces, {len(slots)} créneaux" in seeded["stdout"]


@requires_db
def test_seed_refuses_populated_database_without_reset(seeded: dict, tmp_path: Path) -> None:
    out = tmp_path / "tokens.csv"
    proc = subprocess.run(
        [sys.executable, "-m", "app.seed", "--tokens-out", str(out)],
        cwd=BACKEND_DIR, env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "--reset" in proc.stderr
    assert not out.exists()
