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
| `CLIENT_TOKEN` | — | Token Bearer pour l'authentification client (secret) |
| `MCP_BEARER_TOKEN` | — | Token Bearer pour l'authentification MCP/harness (secret) |
| `SESSION_TTL_SECONDS` | `1800` | Durée de vie des codes de session (en secondes) |

### Volumes

- `./logs` : répertoire optionnel pour les logs et audit du relay

## Arrêt et nettoyage

```bash
# Arrêter le relay
docker-compose -f docker/docker-compose.yml down

# Arrêter et supprimer les volumes
docker-compose -f docker/docker-compose.yml down -v
```

## Sécurité

- **Tokens** : jamais de secrets en dur dans les Dockerfile ou fichiers source. Tous les secrets viennent du `.env` ou des secrets Docker.
- **Port** : par défaut le relay ne s'expose que localement. Pour un déploiement distant, utilise un reverse-proxy TLS (nginx, Caddy, etc.) ou expose via un VPN.
- **Healthcheck** : le relay expose un endpoint `/healthz` utilisé par Docker pour surveiller la santé du conteneur.

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
