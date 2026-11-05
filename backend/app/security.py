"""Authentification minimale des commerçants par token opaque.

Choix de proto : pas de comptes ni de mots de passe. Chaque commerçant reçoit un token
aléatoire de 256 bits ; la base n'en garde que le SHA-256.
Un SHA-256 simple (sans sel ni KDF lente) suffit ici car le token a 256 bits d'entropie :
il n'y a pas de dictionnaire à attaquer, contrairement à un mot de passe humain.
"""
import hashlib
import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


def new_token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(32)  # 32 octets aléatoires -> 43 caractères base64url
    return token, hash_token(token)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


async def current_merchant_id(
    request: Request,
    x_merchant_token: Annotated[str | None, Header(alias="X-Merchant-Token")] = None,
) -> int:
    # En-tête absent ou de forme impossible : 401 (un en-tête requis côté FastAPI donnerait 422).
    if x_merchant_token is None or not 20 <= len(x_merchant_token) <= 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token commerçant manquant ou invalide")
    pool = request.app.state.pool
    merchant_id = await pool.fetchval(
        "SELECT id FROM merchant WHERE token_hash = $1", hash_token(x_merchant_token)
    )
    if merchant_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token commerçant invalide")
    return merchant_id
