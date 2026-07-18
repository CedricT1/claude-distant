# claude-distant

Accès distant piloté par un harnais IA (Claude) pour l'administration système sur un PC Windows ou Ubuntu, **sans installation** et **sans trace** sur la machine distante.

## À quoi ça sert ?

`claude-distant` permet à Claude (via un harnais IA) de prendre en main à distance un poste de travail Windows ou Ubuntu pour des tâches d'administration système : diagnostiquer des problèmes, exécuter des commandes, lire/modifier des fichiers, redémarrer des services, etc.

**Invariants de sécurité** :
- Le PC distant n'ouvre **aucun port entrant** : seule connexion **sortante** WebSocket/TLS vers le relay
- Client **portable, sans installation, sans trace** : binaire unique lancé depuis un dossier temporaire, auto-nettoyage à la fermeture
- Sessions **éphémères** : codes 9 chiffres à durée de vie courte (30 min par défaut)
- **Consentement explicite** : l'utilisateur voit le code et approuve les commandes sensibles selon la politique locale

## Architecture

```
┌─────────────────┐         ┌──────────────────────────────────┐         ┌──────────────────┐
│  PC distant     │  WS/TLS │  RELAY (Docker)                  │   MCP   │  Harnais (Claude)│
│  (Win / Ubuntu) │ ───────▶│  [TLS externe] → nginx (HTTP) →  │◀─────── │  + opérateur     │
│  client Go      │ sortant │  broker WS + serveur MCP auth    │  Bearer │                  │
│  portable       │         │  + audit / routage               │  /OAuth │                  │
└─────────────────┘         └──────────────────────────────────┘         └──────────────────┘
    affiche                        mappe code→client
    « 784 123 678 »                                                  « connecte-toi à
                                                                      784 123 678 »
```

> La terminaison **TLS est externe** (reverse proxy du déployeur : Caddy/nginx/Traefik).
> La stack expose un **nginx HTTP-only** en interne devant le relay ; le proxy externe
> doit positionner `X-Forwarded-Proto: https`. Voir [docs/SECURITY.md](docs/SECURITY.md).

### Flot type

1. **Lancement du client** : l'opérateur lance le binaire Go sur la machine distante
2. **Enregistrement** : le client se connecte au relay via WebSocket/TLS, reçoit un code unique (ex. `784 123 678`)
3. **Partage du code** : l'opérateur donne ce code au harness (Claude)
4. **Connexion du harness** : Claude utilise l'outil MCP `connect_session(code)` pour s'authentifier auprès du relay
5. **Exécution de commandes** : Claude exécute des tâches via les outils MCP (`system_info`, `run_shell`, etc.) ; le relay les route au client
6. **Garde-fou local** : pour les commandes destructives, le client demande confirmation localement selon la politique (`auto` / `confirm` / `deny`)
7. **Fermeture** : le code expire ou le client s'arrête ; session clôturée, aucune trace sur le PC

## Structure du dépôt

```
claude-distant/
├── relay/                   # Broker WebSocket + serveur MCP (Python 3.12 + FastAPI)
│   ├── app.py              # Point d'entrée FastAPI
│   ├── broker.py           # Gestion des sessions et routage WS
│   ├── mcp_server.py       # Serveur MCP HTTP Streamable
│   ├── auth.py             # Authentification Bearer
│   └── requirements.txt     # Dépendances Python
├── client/                  # Client portable Go (Makefile de build cross-platform)
├── docker/                  # Dockerfile + docker-compose + configuration
│   ├── Dockerfile.relay     # Image Docker du relay
│   ├── docker-compose.yml   # Orchestration : nginx (HTTP) + relay
│   ├── nginx.conf           # Reverse proxy HTTP-only interne
│   ├── .env.example         # Modèle de configuration
│   └── README.md            # Guide Docker détaillé
├── docs/                    # Documentation
│   ├── PLAN.md              # Plan de développement et phases
│   ├── PROTOCOL.md          # Spécification des protocoles client↔relay et harness↔relay
│   ├── SECURITY.md          # Modèle de menace, TLS externe, scopes, audit
│   └── PACKAGING.md         # Build, signature, modèle « sans trace »
├── tests/                   # Tests (relay pytest ; client Go côté client/)
└── README.md               # Ce fichier
```

## Démarrage rapide

### 1. Lancer le relay avec Docker

```bash
# Copier et personnaliser la configuration
cp docker/.env.example docker/.env

# Éditer docker/.env pour configurer les tokens secrets
# CLIENT_TOKEN=<token-fort>
# MCP_BEARER_TOKEN=<token-fort>

# Builder et lancer la stack (nginx HTTP-only + relay)
docker-compose -f docker/docker-compose.yml up -d
```

La stack expose un **nginx HTTP-only** sur `http://localhost:8080` (configurable via `HTTP_PORT`).
Placez votre reverse proxy **TLS externe** (Caddy/nginx/Traefik) devant ce port en positionnant
`X-Forwarded-Proto: https`. Voir [docs/SECURITY.md](docs/SECURITY.md).

### 2. Builder le client Go

```bash
cd client
go build -o claude-distant .          # build local rapide
# ou, binaires portables strippés (linux amd64/arm64 + windows amd64) :
make dist                             # sorties dans client/dist/
make checksums                        # SHA256SUMS
```

### 3. Lancer le client sur la machine distante

```bash
# Sur Windows
./claude-distant.exe --url wss://relay.example.com --token <CLIENT_TOKEN>

# Sur Linux
./claude-distant --url wss://relay.example.com --token <CLIENT_TOKEN>
```

Flags utiles : `--policy auto|confirm|deny` (garde-fou local), `--self-destruct`
(supprime le binaire à l'arrêt propre). Équivalents en variables d'environnement :
`CLAUDE_DISTANT_URL`, `CLAUDE_DISTANT_TOKEN`, `CLAUDE_DISTANT_POLICY`, `CLAUDE_DISTANT_SELF_DESTRUCT`.

Le client affiche un code unique à 9 chiffres.

### 4. Connecter Claude (harness)

L'opérateur donne le code au harness. Claude exécute :

```
connect_session(code="784123678")
system_info()
run_shell(command="df -h", shell="auto")
# ... d'autres commandes
```

## Stack technique

| Composant | Technologie | Notes |
|-----------|------------|-------|
| Relay / Broker | Python 3.12 + FastAPI + websockets | Même écosystème que le harness |
| Serveur MCP | MCP Python SDK (Streamable HTTP) | Auth Bearer native |
| Client PC | **Go** (binaire statique unique) | Portable, sans dépendances runtime, sans trace |
| Transport client↔relay | WebSocket over TLS (`wss://`) | Sortant, firewall/NAT-friendly |
| Session store | Redis (optionnel) | Pour multi-instance ; in-memory par défaut (single-instance) |
| Déploiement | Docker + docker-compose | Reproducibilité et isolation |

## Outils MCP disponibles

Le harness (Claude) accède au PC distant via les outils MCP **actuellement implémentés** (ciblés par `session_code`, sauf `issue_client_token`) :

| Outil | Rôle | Scope (mode `oauth`) |
|-------|------|----------------------|
| `connect_session(session_code)` | Valider et se connecter à une session client | `session:connect` |
| `system_info(session_code)` | Récupérer OS, uptime, RAM, CPU | — |
| `run_command(session_code, command, timeout?)` | Exécuter une commande (sans shell) | `command:execute` |
| `run_shell(session_code, command, shell="auto", timeout?)` | Exécuter en PowerShell (Windows) / Bash (Linux) selon l'OS | `command:execute` |
| `terminate_session(session_code)` | Kill-switch : clôturer une session | `session:terminate` |
| `issue_client_token(ttl_seconds?)` | Émettre un jeton client `per_session` court | `client:provision` |

Les tâches sysadmin (check disk, processus, services, logs, mises à jour…) se font via `run_shell`/`run_command` (ex. `df -h` / `Get-Volume`, `systemctl` / `Get-Service`). Des helpers de plus haut niveau dédiés (`disk_check`, `service_restart`, etc.) sont prévus au plan mais pas encore exposés.

Tous les outils respectent la **politique de confirmation locale** du client : en mode `confirm`, l'utilisateur doit approuver les actions destructives localement.

### Authentification MCP : `static_bearer` vs `oauth`

Le canal harnais↔relay (`/mcp`) supporte deux modes via `MCP_AUTH_MODE` :

- `static_bearer` (défaut, MVP) : jeton unique `MCP_BEARER_TOKEN`, tous les outils accessibles.
- `oauth` : Resource Server OAuth 2.1, jetons Bearer JWT scopés (`session:connect`, `command:execute`, `session:terminate`, `client:provision`). Émission via :

  ```bash
  python -m relay.tokens issue --sub harness-operateur \
    --scopes session:connect,command:execute,session:terminate,client:provision \
    --ttl 3600
  ```

- `issue_client_token(ttl_seconds?)` : outil MCP (scope `client:provision`) pour obtenir un jeton client `per_session` court à donner à l'opérateur distant, sans passer par un appel direct à `PerSessionTokenStore`.

Voir [docs/SECURITY.md](docs/SECURITY.md) pour le détail (modèle de menace, TLS, scopes, audit, kill-switch).

## Documentation

- **[docs/PLAN.md](docs/PLAN.md)** : plan de développement, phases, invariants de sécurité
- **[docs/PROTOCOL.md](docs/PROTOCOL.md)** : spécification détaillée des protocoles JSON sur WebSocket et MCP HTTP
- **[docker/README.md](docker/README.md)** : guide complet pour déployer le relay avec Docker

## Sécurité

⚠️ **`claude-distant` est un exécuteur de commandes à distance privilégié.**

Usage **autorisé et supervisé uniquement** (utilisateur présent et consentant) :

- **Consentement explicite** : partage du code unique + approbation locale pour les actions destructives
- **Audit complet** : journal immuable de chaque commande exécutée
- **Sessions éphémères** : codes à TTL court, révocables
- **Isolation réseau** : client ne reçoit de commandes que s'il partage le code valide ; pas d'accès non-autorisé
- **Authentification Bearer** : tokens forts, régénérés régulièrement
- **Zéro installation** : aucun service, clé registre, ou autostart ; nettoyage automatique

Voir [docs/SECURITY.md](docs/SECURITY.md) pour le modèle de menace complet et les mitigations.

## Phases de développement

1. **Phase 0** (✓) : Cadrage sécurité & protocole
2. **Phase 1** (✓) : Broker de session (WebSocket, génération code 9 chiffres)
3. **Phase 2** (✓) : Client Go minimal
4. **Phase 3** (✓) : Serveur MCP sur le relay
5. **Phase 4** (✓ primitives) : Exécution cross-platform via `run_shell`/`run_command` (helpers sysadmin dédiés à venir)
6. **Phase 5** (✓) : Durcissement sécurité (OAuth 2.1 scopé, audit immuable, kill-switch, tokens par-session)
7. **Phase 6** (✓) : Client portable sans trace (workspace temp auto-nettoyé, `--self-destruct`, build strippé)
8. **Phase 7** (en cours) : Tests (relay 151 + client 61 verts), observabilité, test d'intégration bout-en-bout relay↔client

Voir [docs/PLAN.md](docs/PLAN.md) pour les détails.

## Développement

### Prérequis

- Python 3.12 (pour le relay)
- Go 1.22+ (pour le client)
- Docker + Docker Compose (pour le déploiement)
- Redis (optionnel, pour multi-instance)

### Lancer le relay localement (sans Docker)

```bash
cd relay
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

### Lancer les tests

```bash
# Relay (Python) — 151 tests
pip install -r relay/requirements.txt
pytest tests/relay -q

# Client (Go) — 61 tests
cd client && go test ./...
```

## Licence

[Voir LICENSE](LICENSE)

## Notes

- Ce projet est en développement actif.
- Le protocole et la sécurité peuvent évoluer entre les versions.
- Consulte [docs/PROTOCOL.md](docs/PROTOCOL.md) pour les détails d'intégration avec le harness.
