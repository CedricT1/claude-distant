# Docker — Relay claude-distant

Ce répertoire contient les fichiers de containerisation du relay (broker de session + serveur MCP).

## Structure

- `Dockerfile.relay` : image Docker pour le relay (Python 3.12 + FastAPI)
- `docker-compose.yml` : orchestration du relay
- `.env.example` : modèle de configuration d'environnement

## Démarrage rapide

### 1. Préparer la configuration

Copie le fichier d'exemple et personnalise les secrets :

```bash
cp docker/.env.example docker/.env
```

Édite `docker/.env` pour configurer les tokens d'authentification :

```bash
CLIENT_TOKEN=<token-fort-secret-pour-client>
MCP_BEARER_TOKEN=<token-fort-secret-pour-harness>
```

**Important** : utilise des tokens forts et aléatoires en production. Les valeurs par défaut (`change-me`) ne sont que des exemples.

### 2. Builder et lancer le relay

```bash
# Builder l'image
docker-compose -f docker/docker-compose.yml build

# Lancer en arrière-plan
docker-compose -f docker/docker-compose.yml up -d

# Vérifier que le relay est actif
docker-compose -f docker/docker-compose.yml logs -f relay
```

Le relay écoute par défaut sur `http://0.0.0.0:8000` (configurable via `PORT` dans `.env`).

### 3. Vérifier la santé

```bash
# Appel direct au health check
curl http://localhost:8000/healthz

# Ou via docker-compose
docker-compose -f docker/docker-compose.yml ps
```

## Configuration

### Variables d'environnement

Toutes les variables se configent dans `docker/.env` (voir `.env.example` pour les détails) :

| Variable | Défaut | Description |
|----------|--------|-------------|
| `HOST` | `0.0.0.0` | Adresse de binding du relay |
| `PORT` | `8000` | Port TCP du relay (interne, sur le réseau Docker) |
| `CLIENT_TOKEN` | — | Token Bearer pour l'authentification client (secret), mode `CLIENT_AUTH_MODE=shared` |
| `CLIENT_AUTH_MODE` | `shared` | `shared` (jeton unique) ou `per_session` (jetons courts émis via l'outil MCP `issue_client_token`) |
| `MCP_BEARER_TOKEN` | — | Token Bearer pour l'authentification MCP/harness (secret), mode `MCP_AUTH_MODE=static_bearer` |
| `MCP_AUTH_MODE` | `static_bearer` | `static_bearer` (jeton unique) ou `oauth` (JWT scopés, voir `docs/SECURITY.md`) |
| `MCP_JWT_SECRET` | — | Secret HS256 de signature/vérification des JWT (mode `oauth` uniquement) |
| `MCP_JWT_ALGORITHM` | `HS256` | Algorithme de signature JWT (mode `oauth`) |
| `MCP_JWT_ISSUER_URL` / `MCP_JWT_RESOURCE_SERVER_URL` | auto-suffisant | Métadonnées OAuth exposées par le SDK MCP (mode `oauth`) ; doivent être en HTTPS pour la production |
| `SESSION_TTL_SECONDS` | `1800` | Durée de vie des codes de session (en secondes) ; aussi TTL par défaut des jetons `issue_client_token` |
| `COMMAND_DENYLIST` / `COMMAND_ALLOWLIST` | — | Motifs regex séparés par `;` (voir `relay/command_policy.py`) |
| `MAX_COMMANDS_PER_SESSION` / `RATE_LIMIT_PER_MINUTE` | — | Quotas par session |
| `AUDIT_LOG_PATH` | `/app/logs/audit.log` | Chemin du journal d'audit JSONL chaîné |
| `HTTP_PORT` | `8080` | Port HTTP exposé par le reverse proxy nginx interne (pas exposé directement sur l'hôte) |

### Volumes

- `./logs` : répertoire optionnel pour les logs et audit du relay
- `docker/nginx.conf` : fichier de configuration nginx (monté read-only)

## TLS strict (terminaison externe)

Le relay écoute en **HTTP interne uniquement** ; il ne fait jamais de terminaison
TLS lui-même. La stack Docker expose un reverse proxy **nginx HTTP-only** (port 8080
par défaut, sur le réseau Docker interne uniquement) qui proxies vers le relay.

La **terminaison TLS est assurée par un reverse proxy externe**, de la responsabilité
du déployeur (Caddy, Nginx, Traefik, etc.). Le déployeur place son propre reverse
proxy HTTPS devant la stack et forward le trafic vers le port nginx HTTP (8080).

**Important** : le reverse proxy TLS externe **DOIT positionner l'en-tête**
`X-Forwarded-Proto: https` lors du forward vers le nginx HTTP interne. Cela
permet au relay de générer les URLs correctes pour les métadonnées OAuth RFC 9728
en mode `MCP_AUTH_MODE=oauth`.

Exemple de configuration Caddy externe (sur l'hôte du déployeur) :

```caddy
relay.example.com {
    tls {
        protocols tls1.2 tls1.3
    }
    reverse_proxy localhost:8080 {
        header_up X-Forwarded-Proto https
    }
}
```

Voir [`docs/SECURITY.md`](../docs/SECURITY.md) pour le détail complet (modèle de
menace, en-têtes de sécurité, configuration du reverse proxy externe).

## Arrêt et nettoyage

```bash
# Arrêter le relay
docker-compose -f docker/docker-compose.yml down

# Arrêter et supprimer les volumes
docker-compose -f docker/docker-compose.yml down -v
```

## Sécurité

- **Tokens** : jamais de secrets en dur dans les Dockerfile ou fichiers source. Tous les secrets viennent du `.env` ou des secrets Docker.
- **TLS** : reverse-proxy TLS strict fourni (Caddy, profil `tls` — voir ci-dessus ; alternative Nginx dans `docker/nginx.conf.example`). Le relay lui-même ne parle jamais TLS.
- **Auth MCP** : `MCP_AUTH_MODE=static_bearer` (défaut) ou `oauth` (JWT scopés, voir `docs/SECURITY.md`).
- **Healthcheck** : le relay expose un endpoint `/healthz` utilisé par Docker pour surveiller la santé du conteneur.

Voir [`docs/SECURITY.md`](../docs/SECURITY.md) pour le modèle de menace complet.

## Dépannage

### Le relay refuse la connexion client

Vérifier :
1. Le conteneur tourne : `docker-compose ps`
2. Le port est libre : `netstat -an | grep 8000`
3. Le token client dans `.env` correspond à celui fourni au client

### Logs du relay

```bash
docker-compose -f docker/docker-compose.yml logs -f relay
```

### Reconstruire l'image

```bash
docker-compose -f docker/docker-compose.yml build --no-cache
```

## Architecture

**Relay** (FastAPI, Python 3.12) :
- Écoute en HTTP sur `relay:8000` (réseau Docker interne uniquement)
- Expose :
  - **WebSocket** (`ws://relay:8000/ws/client`) : canal vers les clients distants
  - **MCP HTTP** (`http://relay:8000/mcp`) : endpoint pour le harness (Claude)
  - **Health** (`http://relay:8000/healthz`) : probe Docker

**Nginx** (HTTP reverse proxy interne) :
- Écoute sur `0.0.0.0:8080` (port HTTP, réseau Docker interne)
- Proxies vers `relay:8000` avec support WebSocket et streaming
- Transmet les en-têtes pour que le relay reconnaisse le schéma/hôte externes

**Reverse proxy TLS (externe, responsabilité du déployeur)** :
- Termine le TLS en HTTPS (port 443)
- Forward le trafic vers `nginx:8080`
- DOIT positionner `X-Forwarded-Proto: https`

Voir `docs/PROTOCOL.md` et `docs/PLAN.md` pour les détails du protocole et de l'architecture.
