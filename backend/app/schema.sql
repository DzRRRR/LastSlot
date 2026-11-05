-- Schéma du prototype Last Slot, version 3 (voir SCHEMA_VERSION dans db.py).
-- Idempotent : exécuté à chaque démarrage de l'API. Pas de migrations pour le proto : en cas de
-- changement de version, `python -m app.seed --reset` supprime et recrée les objets ci-dessous.
-- PostgreSQL 16 + PostGIS 3.4.

CREATE EXTENSION IF NOT EXISTS postgis;
-- btree_gist : permet de mettre un type scalaire (bigint) dans un index GiST,
-- nécessaire pour la contrainte d'exclusion (resource_id WITH =, période WITH &&).
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS schema_version (version int NOT NULL);

-- family = 'appointment' : rendez-vous (1 client, 1 praticien ou 1 terrain)
-- family = 'event'       : séance à capacité N (cours collectifs, prévus en année 2)
CREATE TABLE IF NOT EXISTS category (
    code     text PRIMARY KEY,
    label    text NOT NULL,
    family   text NOT NULL CHECK (family IN ('appointment', 'event')),
    position int  NOT NULL DEFAULT 100  -- ordre d'affichage des filtres
);

CREATE TABLE IF NOT EXISTS merchant (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL,
    category_code text NOT NULL REFERENCES category(code),
    address       text NOT NULL,
    -- geography(Point, 4326) : coordonnées WGS84 (lon/lat), distances en mètres.
    location      geography(Point, 4326) NOT NULL,
    -- On ne stocke que le SHA-256 du token : une fuite de la base ne donne pas les tokens.
    token_hash    bytea NOT NULL UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS merchant_location_gix ON merchant USING gist (location);
-- Index sur la projection geometry : sert au filtre par viewport (GET /api/slots?bbox=).
-- Le viewport d'une carte Web Mercator est un rectangle lon/lat ; en geometry, `&&` teste
-- exactement ce rectangle. En geography, les bords de l'enveloppe seraient des arcs de grand
-- cercle (écart de ~1 km au bord d'une vue régionale, enveloppe dégénérée en vue monde).
CREATE INDEX IF NOT EXISTS merchant_location_geom_gix ON merchant USING gist ((location::geometry));

-- Prestation type (coupe homme, balayage, terrain de padel 1 h 30).
CREATE TABLE IF NOT EXISTS service (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_id      bigint NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    name             text NOT NULL,
    duration_min     int  NOT NULL CHECK (duration_min > 0),
    base_price_cents int  NOT NULL CHECK (base_price_cents > 0)
);

-- Praticien, terrain, court : ce qui ne peut pas servir deux clients à la fois.
CREATE TABLE IF NOT EXISTS resource (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_id bigint NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    name        text NOT NULL
);

CREATE TABLE IF NOT EXISTS slot (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    merchant_id      bigint NOT NULL REFERENCES merchant(id) ON DELETE CASCADE,
    service_id       bigint NOT NULL REFERENCES service(id) ON DELETE CASCADE,
    resource_id      bigint REFERENCES resource(id) ON DELETE SET NULL,
    starts_at        timestamptz NOT NULL,
    ends_at          timestamptz NOT NULL,
    capacity         int NOT NULL CHECK (capacity > 0),
    remaining        int NOT NULL,
    -- Prix de la prestation figé à la publication : modifier le tarif ensuite ne change pas
    -- les créneaux déjà en ligne.
    base_price_cents int NOT NULL CHECK (base_price_cents > 0),
    -- Plafond de remise fixé par le pro ; la remise appliquée monte par paliers jusqu'à lui
    -- (voir discount_at). Le prix courant n'est pas stocké : il dépend de l'heure.
    max_discount_pct int NOT NULL CHECK (max_discount_pct BETWEEN 5 AND 80),
    status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'cancelled')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (ends_at > starts_at),
    -- Garde-fou ultime contre la surréservation : même si le code applicatif bugue,
    -- la base refuse un stock négatif ou supérieur à la capacité.
    CHECK (remaining >= 0 AND remaining <= capacity),
    -- Un même praticien ne peut pas avoir deux créneaux ouverts qui se chevauchent.
    -- tstzrange(a, b) est semi-ouvert [a, b) : 10h-11h et 11h-12h ne se chevauchent pas.
    CONSTRAINT slot_no_resource_overlap
        EXCLUDE USING gist (resource_id WITH =, tstzrange(starts_at, ends_at) WITH &&)
        WHERE (resource_id IS NOT NULL AND status = 'open')
);
CREATE INDEX IF NOT EXISTS slot_open_starts_idx ON slot (starts_at) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS slot_merchant_idx ON slot (merchant_id);

CREATE TABLE IF NOT EXISTS booking (
    -- UUID v4 aléatoire (122 bits) : l'id sert de capability, non énumérable.
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slot_id          bigint NOT NULL REFERENCES slot(id) ON DELETE CASCADE,
    customer_name    text NOT NULL,
    customer_contact text NOT NULL,
    seats            int  NOT NULL DEFAULT 1 CHECK (seats > 0),
    -- Prix figé à la prise de place : la remise progressive change ensuite le prix courant du
    -- créneau, le client paie le prix qu'il a vu. Commission Last Slot calculée au même moment.
    discount_pct     int  NOT NULL CHECK (discount_pct BETWEEN 0 AND 80),
    unit_price_cents int  NOT NULL CHECK (unit_price_cents > 0),
    amount_cents     int  NOT NULL,
    commission_cents int  NOT NULL,
    status           text NOT NULL CHECK (status IN ('held', 'confirmed', 'expired', 'cancelled')),
    expires_at       timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    confirmed_at     timestamptz,
    CHECK (amount_cents = unit_price_cents * seats),
    CHECK (commission_cents BETWEEN 0 AND amount_cents),
    CHECK (status <> 'held' OR expires_at IS NOT NULL),
    CHECK ((status = 'confirmed') = (confirmed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS booking_held_expiry_idx ON booking (expires_at) WHERE status = 'held';
CREATE INDEX IF NOT EXISTS booking_slot_idx ON booking (slot_id);

-- Remise progressive (executive summary) : plus le rendez-vous approche, plus la remise monte,
-- jusqu'au plafond fixé par le pro. Paliers : 1/3 du plafond jusqu'à H-6, 2/3 jusqu'à H-2, puis
-- le plafond ; arrondis à 5 %, entre 5 % et le plafond. Plafond 30 % : -10 % la veille, -20 % à
-- H-6, -30 % à H-2. Pas de demi à arrondir : max/15 et 2·max/15 ne valent jamais k + 0,5 pour un
-- entier, donc round() donne le même résultat ici qu'en JS (discountTiers dans common.js).
-- STRICT : NULL en entrée -> NULL (utile pour « prochain palier : aucun »).
CREATE OR REPLACE FUNCTION discount_at(starts_at timestamptz, max_pct int, at timestamptz)
RETURNS int LANGUAGE sql STABLE STRICT AS $$
    SELECT CASE
        WHEN starts_at - at <= interval '2 hours' THEN max_pct
        WHEN starts_at - at <= interval '6 hours'
            THEN LEAST(max_pct, GREATEST(5, round(max_pct * 2 / 15.0) * 5))::int
        ELSE LEAST(max_pct, GREATEST(5, round(max_pct / 15.0) * 5))::int
    END
$$;

-- Prix remisé au centime le plus proche, demi vers le haut, jamais 0 (même règle en JS :
-- discountedPrice dans common.js, pour que l'aperçu du pro tombe juste au centime).
CREATE OR REPLACE FUNCTION discounted_price_cents(base_cents int, pct int)
RETURNS int LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT GREATEST(1, (base_cents * (100 - pct) + 50) / 100)
$$;

-- Créneaux avec leur prix courant et le prochain palier de remise (NULL s'il n'y en a plus).
-- Les filtres sur les colonnes de slot sont poussés sous la vue : les index restent utilisés.
CREATE OR REPLACE VIEW slot_priced AS
SELECT s.*,
       cur.pct AS discount_pct,
       discounted_price_cents(s.base_price_cents, cur.pct) AS price_cents,
       nxt.at AS next_discount_at,
       discount_at(s.starts_at, s.max_discount_pct, nxt.at) AS next_discount_pct
FROM slot s
CROSS JOIN LATERAL (SELECT discount_at(s.starts_at, s.max_discount_pct, now()) AS pct) cur
CROSS JOIN LATERAL (
    SELECT CASE
        WHEN now() < s.starts_at - interval '6 hours'
             AND discount_at(s.starts_at, s.max_discount_pct, s.starts_at - interval '6 hours') > cur.pct
            THEN s.starts_at - interval '6 hours'
        WHEN now() < s.starts_at - interval '2 hours' AND s.max_discount_pct > cur.pct
            THEN s.starts_at - interval '2 hours'
    END AS at
) nxt;

-- Secteurs de la V1 (executive summary) : coiffure, beauté, sport indoor.
INSERT INTO category (code, label, family, position) VALUES
    ('coiffure',   'Coiffure',           'appointment', 10),
    ('barbier',    'Barbier',            'appointment', 20),
    ('esthetique', 'Institut de beauté', 'appointment', 30),
    ('ongles',     'Onglerie',           'appointment', 40),
    ('padel',      'Padel',              'appointment', 50),
    ('foot5',      'Foot à 5',           'appointment', 60),
    ('tennis',     'Tennis & squash',    'appointment', 70)
ON CONFLICT (code) DO UPDATE
    SET label = EXCLUDED.label, family = EXCLUDED.family, position = EXCLUDED.position;
