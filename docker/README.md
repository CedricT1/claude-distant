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
| `PORT` | `8000` | Port TCP du relay |
| `CLIENT_TOKEN` | — | Token Bearer pour l'authentification client (secret), mode `CLIENT_AUTH_MODE=shared` |
| `CLIENT_AUTH_MODE` | `shared` | `shared` (jeton unique) ou `per_session` (jetons courts émis via l'outil MCP `issue_client_token`) |
| `MCP_BEARER_TOKEN` | — | Token Bearer pour l'authentification MCP/harness (secret), mode `MCP_AUTH_MODE=static_bearer` |
| `MCP_AUTH_MODE` | `static_bearer` | `static_bearer` (jeton unique) ou `oauth` (JWT scopés, voir `docs/SECURITY.md`) |
| `MCP_JWT_SECRET` | — | Secret HS256 de signature/vérification des JWT (mode `oauth` uniquement) |
| `MCP_JWT_ALGORITHM` | `HS256` | Algorithme de signature JWT (mode `oauth`) |
| `MCP_JWT_ISSUER_URL` / `MCP_JWT_RESOURCE_SERVER_URL` | auto-suffisant | Métadonnées OAuth exposées par le SDK MCP (mode `oauth`) |
| `SESSION_TTL_SECONDS` | `1800` | Durée de vie des codes de session (en secondes) ; aussi TTL par défaut des jetons `issue_client_token` |
| `COMMAND_DENYLIST` / `COMMAND_ALLOWLIST` | — | Motifs regex séparés par `;` (voir `relay/command_policy.py`) |
| `MAX_COMMANDS_PER_SESSION` / `RATE_LIMIT_PER_MINUTE` | — | Quotas par session |
| `AUDIT_LOG_PATH` | `/app/logs/audit.log` | Chemin du journal d'audit JSONL chaîné |
| `TLS_DOMAIN` | `relay.example.com` | Nom d'hôte public pour le reverse proxy Caddy (profil `tls`, voir ci-dessous) |

### Volumes

- `./logs` : répertoire optionnel pour les logs et audit du relay
- `caddy_data` / `caddy_config` : certificats TLS et état Caddy (profil `tls`, voir ci-dessous)

## TLS strict (reverse proxy)

Le relay écoute en HTTP interne uniquement ; il ne fait jamais de terminaison
TLS lui-même. Un reverse proxy Caddy (fourni, service optionnel sous le
profil `tls`) termine le TLS 1.2+ devant le relay :

```bash
# Configurer TLS_DOMAIN dans docker/.env (nom d'hôte public, DNS pointant
# vers cette machine pour que Let's Encrypt puisse valider le domaine)
echo "TLS_DOMAIN=relay.example.com" >> docker/.env

docker-compose -f docker/docker-compose.yml --profile tls up -d
```

Seul le port 443 (Caddy) doit être exposé publiquement en production ; le
port direct du relay (`PORT`, 8000 par défaut) ne devrait pas être publié sur
l'hôte une fois le profil `tls` actif (voir le commentaire dans
`docker-compose.yml`). Une alternative Nginx est fournie dans
`docker/nginx.conf.example` pour qui préfère gérer ses certificats
manuellement. Voir [`docs/SECURITY.md`](../docs/SECURITY.md) pour le détail
complet (modèle de menace, en-têtes de sécurité, choix Caddy vs Nginx).

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

Le relay (FastAPI) s'exécute dans un conteneur Python 3.12 slim. Il expose :

- **WebSocket** (`wss://localhost:8000/ws/client`) : canal de communication avec les clients distants
- **MCP HTTP** (`http://localhost:8000/mcp`) : endpoint pour le harness (Claude)
- **Health** (`http://localhost:8000/healthz`) : probe pour Docker

Voir `docs/PROTOCOL.md` et `docs/PLAN.md` pour les détails du protocole et de l'architecture.
