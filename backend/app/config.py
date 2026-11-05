"""Configuration lue depuis les variables d'environnement (12-factor)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Défaut = base de dev Docker de HANDOFF.md (127.0.0.1:5434). Pas 5432 : sur la machine de
    # dev, c'est le PostgreSQL 18 système, sans PostGIS ni rôle creno.
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://creno:creno@127.0.0.1:5434/creno"
    )
    # Durée pendant laquelle une place est bloquée entre "Réserver" et le paiement.
    hold_seconds: int = int(os.getenv("HOLD_SECONDS", "300"))
    # Fréquence des tâches de fond : libération des holds expirés, annonce des paliers de remise.
    expiry_interval_seconds: float = float(os.getenv("EXPIRY_INTERVAL_SECONDS", "5"))
    # Commission Last Slot sur chaque créneau vendu (executive summary : 15 %, sans abonnement).
    commission_pct: int = int(os.getenv("COMMISSION_PCT", "15"))
    # Intervalle des commentaires SSE ": ping" : maintient la connexion ouverte à travers
    # les proxys et permet de détecter les clients partis.
    sse_heartbeat_seconds: float = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))
    # URL publique de l'app : sert au seed pour générer les liens commerçants.
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    # Style MapLibre. OpenFreeMap : gratuit, sans clé. Remplaçable par un PMTiles auto-hébergé.
    map_style_url: str = os.getenv(
        "MAP_STYLE_URL", "https://tiles.openfreemap.org/styles/liberty"
    )
    app_name: str = os.getenv("APP_NAME", "Last Slot")
    # Carte : Paris, ville de lancement (lon, lat). Zoom 12 : tout Paris sur un écran de portable
    # (MapLibre compte le zoom en tuiles de 512 px).
    default_center: tuple[float, float] = (2.3470, 48.8590)
    default_zoom: float = 12.0
    csp_enabled: bool = os.getenv("CSP_ENABLED", "true").lower() == "true"


settings = Settings()
