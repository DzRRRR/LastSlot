#!/bin/sh
# Exécuté une seule fois, à l'initialisation du volume de la base (image postgres/postgis, après
# le script PostGIS de l'image). Crée le rôle de l'API, sans privilège de superutilisateur, et lui
# donne la base : l'API crée et supprime ses propres tables (schema.sql, seed --reset) mais ne
# peut ni lire les fichiers du serveur, ni lancer de programme, ni installer d'extension non
# « trusted ». PostGIS est déjà installé par le superutilisateur ; btree_gist est « trusted » :
# le propriétaire de la base peut le créer lui-même.
set -eu

# Le mot de passe passe par une variable psql (:'app_password' le cite proprement) et le heredoc
# est entre apostrophes : aucune interpolation par le shell.
psql -v ON_ERROR_STOP=1 -v app_password="$APP_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE lastslot_app LOGIN PASSWORD :'app_password';
SELECT format('ALTER DATABASE %I OWNER TO lastslot_app', current_database()) \gexec
SQL
