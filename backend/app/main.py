"""Application ASGI.

Lancement (depuis backend/) :
    uvicorn app.main:app --workers 1 --timeout-graceful-shutdown 2

Un seul worker : le broker SSE vit dans la mémoire du processus (voir broker.py).
--timeout-graceful-shutdown : sans lui, l'arrêt (et chaque --reload) attend indéfiniment
la fermeture des flux SSE ouverts par les navigateurs.
"""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import api_bookings, api_pro, api_public
from .config import settings
from .db import apply_schema, create_pool
from .jobs import run_jobs_loop

# Les loggers "uvicorn.*" ont leurs propres handlers ; celui-ci affiche ceux de l'app ("app.*").
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# MapLibre crée ses web workers depuis des URL blob: (worker-src, child-src pour les vieux
# Safari) et manipule des images data:/blob: ; le style, les tuiles, sprites et polices
# viennent du serveur de tuiles en HTTPS (connect-src).
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob: https:",
        "connect-src 'self' https:",
        "worker-src blob:",
        "child-src blob:",
        "frame-ancestors 'none'",
    ]
)
# Swagger UI (/docs) et ReDoc chargent leurs scripts depuis un CDN : pas de CSP sur ces pages.
CSP_EXEMPT_PREFIXES = ("/docs", "/redoc")


class SecurityHeadersMiddleware:
    """Ajoute les en-têtes de sécurité à toutes les réponses HTTP.

    Middleware ASGI pur plutôt que @app.middleware("http") (BaseHTTPMiddleware) : il ne
    modifie que le message `http.response.start`, le corps (dont le flux SSE) passe tel quel.
    """

    def __init__(self, app: ASGIApp, csp_enabled: bool = True) -> None:
        self.app = app
        self.csp_enabled = csp_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer")]
        if self.csp_enabled and not scope["path"].startswith(CSP_EXEMPT_PREFIXES):
            extra.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RevalidatedStaticFiles(StaticFiles):
    """Fichiers statiques en `Cache-Control: no-cache` : le navigateur revalide à chaque
    chargement (ETag, 304 si inchangé). Sans en-tête, il peut resservir un ancien app.js
    pendant une durée heuristique après une modification."""

    def file_response(self, full_path, stat_result, scope: Scope, status_code: int = 200) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["cache-control"] = "no-cache"
        return response


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await create_pool(settings.database_url)
    try:
        await apply_schema(pool)
        app.state.pool = pool
        jobs = asyncio.create_task(run_jobs_loop(pool, settings.expiry_interval_seconds))
        try:
            yield
        finally:
            jobs.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await jobs
    finally:
        await pool.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware, csp_enabled=settings.csp_enabled)
app.include_router(api_public.router)
app.include_router(api_pro.router)
app.include_router(api_bookings.router)
# Monté en dernier : "/" capte tout ce qu'aucune route /api/* n'a servi avant lui.
app.mount("/", RevalidatedStaticFiles(directory=STATIC_DIR, html=True), name="static")
