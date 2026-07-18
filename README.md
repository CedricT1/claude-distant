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
┌─────────────────┐         ┌──────────────────────┐         ┌──────────────────┐
│  PC distant     │  WS/TLS │   RELAY (Docker)     │   MCP   │  Harnais (Claude)│
│  (Win / Ubuntu) │ ───────▶│  - Broker de session │◀─────── │  + opérateur     │
│  client Go      │ sortant │  - Serveur MCP auth  │  Bearer │                  │
│  portable       │         │  - Audit / routage   │  /OAuth │                  │
└─────────────────┘         └──────────────────────┘         └──────────────────┘
    affiche                        mappe code→client
    « 784 123 678 »                                    « connecte-toi à
                                                        784 123 678 »
```

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
├── client/                  # Client portable Go
├── shared/                  # Schémas de protocole partagés (Python/Go)
├── docker/                  # Dockerfile + docker-compose + configuration
│   ├── Dockerfile.relay     # Image Docker du relay
│   ├── docker-compose.yml   # Orchestration du relay
│   ├── .env.example         # Modèle de configuration
│   └── README.md            # Guide Docker détaillé
├── docs/                    # Documentation
│   ├── PLAN.md              # Plan de développement et phases
│   ├── PROTOCOL.md          # Spécification des protocoles client↔relay et harness↔relay
│   └── SECURITY.md          # Modèle de menace et considérations de sécurité
├── tests/                   # Tests unitaires et intégration
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

# Builder et lancer le relay
docker-compose -f docker/docker-compose.yml up -d
```

Le relay écoute sur `http://localhost:8000` (configurable).

### 2. Builder le client Go

```bash
cd client
go build -o claude-distant .
```

Crée un binaire portable `claude-distant` (Windows .exe / Linux /client/claude-distant).

### 3. Lancer le client sur la machine distante

```bash
# Sur Windows
./claude-distant.exe --relay-url wss://relay.example.com:8000 --token <CLIENT_TOKEN>

# Sur Linux
./claude-distant --relay-url wss://relay.example.com:8000 --token <CLIENT_TOKEN>
```

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

Le harness (Claude) accède au PC distant via les outils suivants (tous ciblés par `session_code`) :

| Outil | Rôle |
|-------|------|
| `connect_session(code)` | Authentifier et se connecter à une session client |
| `system_info()` | Récupérer OS, uptime, RAM, CPU |
| `disk_check()` / `disk_usage()` | État et usage disque |
| `list_processes()` / `kill_process(pid)` | Gestion des processus |
| `service_status(name)` / `service_restart(name)` | État et redémarrage des services |
| `logs(source, lines)` | Consulter journalctl (Linux) / Event Log (Windows) |
| `read_file(path)` / `write_file(path)` / `list_dir(path)` | Accès fichiers (sous restrictions) |
| `package_update()` | Mises à jour système (apt / Windows Update) |
| `run_command(cmd, timeout)` | Exécuter une commande arbitraire |
| `run_shell(command, shell="auto", timeout)` | Exécuter en PowerShell (Windows) / Bash (Linux) selon l'OS |

Tous les outils respectent la **politique de confirmation locale** du client : en mode `confirm`, l'utilisateur doit approuver les actions destructives localement.

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
2. **Phase 1** (actuelle) : Broker de session MVP (WebSocket, génération code)
3. **Phase 2** : Client Go minimal
4. **Phase 3** : Serveur MCP sur le relay
5. **Phase 4** : Couche sysadmin cross-platform
6. **Phase 5** : Durcissement sécurité (OAuth 2.1, audit avancé)
7. **Phase 6** : Client portable sans trace
8. **Phase 7** : Tests, observabilité, documentation

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
pytest tests/ -v
```

## Licence

[Voir LICENSE](LICENSE)

## Notes

- Ce projet est en développement actif.
- Le protocole et la sécurité peuvent évoluer entre les versions.
- Consulte [docs/PROTOCOL.md](docs/PROTOCOL.md) pour les détails d'intégration avec le harness.
