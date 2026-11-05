# Image de l'API Last Slot : FastAPI + front statique, servis par uvicorn.
# Construite par docker-compose.yml (racine du dépôt) : `docker compose up -d --build`.
FROM python:3.13-slim

# Pas de .pyc (le code est en lecture seule à l'exécution), logs non tamponnés, pip sans cache.
# DEMO_TOKENS_FILE : le seed écrit les liens commerçants dans /tmp (seul dossier inscriptible).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEMO_TOKENS_FILE=/tmp/demo-tokens.csv

WORKDIR /app

# Dépendances d'abord : cette couche reste en cache tant que requirements.txt ne change pas.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Code et front appartiennent à root : l'utilisateur de l'API peut les lire, pas les modifier.
COPY app ./app
COPY static ./static

# Utilisateur dédié, sans privilèges, sans shell de connexion ni répertoire personnel.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin lastslot
USER lastslot

EXPOSE 8000

# curl n'est pas dans l'image slim : sonde en Python (urlopen lève une erreur si l'API ne répond pas).
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/config', timeout=2)"]

# Au premier démarrage (base vide), le seed crée les données de démo ; ensuite il ne touche à rien.
# exec : uvicorn remplace le shell et devient le PID 1, il reçoit donc directement SIGTERM.
# --host 0.0.0.0 : nécessaire dans le conteneur ; l'exposition est limitée par compose (127.0.0.1).
# Un seul worker : le broker SSE vit dans la mémoire du processus.
CMD ["sh", "-c", "python -m app.seed --if-empty && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --timeout-graceful-shutdown 2"]
