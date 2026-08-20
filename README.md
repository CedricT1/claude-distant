# claude-distant

Accès distant piloté par un harnais IA (Claude) pour l'administration système sur un PC Windows ou Ubuntu, **sans installation** et **sans résidu** sur la machine distante.

## À quoi ça sert ?

`claude-distant` permet à Claude (via un harnais IA) de prendre en main à distance un poste de travail Windows ou Ubuntu pour des tâches d'administration système : diagnostiquer des problèmes, exécuter des commandes, lire/modifier des fichiers, redémarrer des services, etc.

**Invariants de sécurité** :
- Le PC distant n'ouvre **aucun port entrant** : seule connexion **sortante** WebSocket/TLS vers le relay
- Client **portable, sans installation, sans résidu** : binaire unique lancé depuis un dossier temporaire, auto-nettoyage à la fermeture — en variante console (par défaut) ou GUI (fenêtre Fyne, postes de bureau), voir `client/README.md`
- Sessions **éphémères** : codes 9 chiffres à durée de vie courte (30 min par défaut) ; le *code* peut être rendu stable d'une reconnexion à l'autre (adresse dérivée de la machine), sans changer la durée de vie de la session — voir `docs/PROTOCOL.md`
- **Consentement explicite** : l'utilisateur voit le code de session et décide de le communiquer — c'est là que se joue le consentement. Le garde-fou local par commande est, lui, configurable : `--policy auto` (défaut, exécution sans invite), `confirm` (approbation opération par opération) ou `deny`

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

1. **Lancement du client** : l'opérateur lance le binaire Go sur la machine distante — console ou GUI, générique ou personnalisé (compilé avec URL/token embarqués pour un lancement sans argument, voir `client/README.md`)
2. **Enregistrement** : le client se connecte au relay via WebSocket/TLS, reçoit un code unique (ex. `784 123 678`)
3. **Partage du code** : l'opérateur donne ce code au harness (Claude)
4. **Connexion du harness** : Claude utilise l'outil MCP `connect_session(code)` pour s'authentifier auprès du relay
5. **Exécution de commandes** : Claude exécute des tâches via les outils MCP (`system_info`, `run_shell`, etc.) ; le relay les route au client
6. **Transfert de fichiers** : `read_file` rapatrie un fichier de la machine distante, `write_file` en dépose un (base64 + SHA-256, 8 MiB max, écriture atomique) — réservés aux clients déclarant la capacité `file_transfer`
7. **Garde-fou local** : par défaut (`auto`), le client exécute les commandes du harnais sans rien demander à l'utilisateur. Lancé avec `--policy confirm`, il demande une confirmation locale pour les commandes destructives — et *systématiquement* pour `read_file`/`write_file` ; avec `--policy deny`, il les refuse d'office
8. **Fermeture** : le code expire ou le client s'arrête ; session clôturée, aucun résidu sur le PC

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
│   └── PACKAGING.md         # Build, signature, modèle « sans résidu »
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
go build -o claude-distant .          # build local rapide (console)
# ou, binaires portables strippés (linux amd64/arm64 + windows amd64) :
make dist                             # console -> client/dist/
make dist-gui                         # + variante GUI (Fyne), voir docs/PACKAGING.md
make checksums                        # SHA256SUMS (couvre les deux familles)
```

### 3. Lancer le client sur la machine distante

```bash
# Sur Windows
./claude-distant.exe --url wss://relay.example.com --token <CLIENT_TOKEN>

# Sur Linux
./claude-distant --url wss://relay.example.com --token <CLIENT_TOKEN>
```

Flags utiles : `--policy auto|confirm|deny` (garde-fou local, `auto` par défaut :
aucune confirmation demandée ; `confirm` valide chaque opération sensible), `--remove-on-exit`
(supprime le binaire à l'arrêt propre). Équivalents en variables d'environnement :
`CLAUDE_DISTANT_URL`, `CLAUDE_DISTANT_TOKEN`, `CLAUDE_DISTANT_POLICY`, `CLAUDE_DISTANT_REMOVE_ON_EXIT`.

Le client affiche un code unique à 9 chiffres, puis journalise à l'écran
chaque commande reçue du harnais (variante console comme variante GUI, où le
panneau « Journal d'activité » offre en plus un filtre « commandes du harnais
uniquement » et l'enregistrement du journal dans un fichier).

### 4. Connecter Claude (harness)

L'opérateur donne le code au harness. Claude exécute :

```
connect_session(code="784123678")
system_info()
run_shell(command="df -h", shell="auto")
read_file(path="/etc/os-release")
write_file(path="/etc/motd", content_base64="…", mode="0644")
# ... d'autres commandes
```

`connect_session` retourne les `capabilities` de la cible : `file_transfer` y
figure pour les clients ≥ 0.2.0, et conditionne `read_file`/`write_file`.

## Stack technique

| Composant | Technologie | Notes |
|-----------|------------|-------|
| Relay / Broker | Python 3.12 + FastAPI + websockets | Même écosystème que le harness |
| Serveur MCP | MCP Python SDK (Streamable HTTP) | Auth Bearer native |
| Client PC | **Go** (binaire statique unique) | Portable, sans dépendances runtime, sans résidu |
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
| `read_file(session_code, path, offset?, max_bytes?)` | Rapatrier un fichier de la machine distante (base64 + SHA-256) | `file:read` |
| `write_file(session_code, path, content_base64, mode?, create_dirs?, overwrite?)` | Déposer un fichier sur la machine distante (écriture atomique) | `file:write` |
| `terminate_session(session_code)` | Kill-switch : clôturer une session | `session:terminate` |
| `issue_client_token(ttl_seconds?)` | Émettre un jeton client `per_session` court | `client:provision` |

Les tâches sysadmin (check disk, processus, services, logs, mises à jour…) se font via `run_shell`/`run_command` (ex. `df -h` / `Get-Volume`, `systemctl` / `Get-Service`). Des helpers de plus haut niveau dédiés (`disk_check`, `service_restart`, etc.) sont prévus au plan mais pas encore exposés.

Tous les outils respectent la **politique de confirmation locale** du client : en mode `confirm`, l'utilisateur doit approuver les actions destructives localement — et `read_file`/`write_file` déclenchent *systématiquement* la confirmation, sans classification préalable.

`read_file`/`write_file` exigent en outre que le client déclare la capacité `file_transfer` (clients ≥ 0.2.0). Un client déjà déployé continue de fonctionner à l'identique et reçoit une erreur explicite `unsupported_by_client` sur ces deux outils seulement ; `connect_session` retourne la liste `capabilities` de la cible. Le contenu transféré est plafonné à 8 MiB et rédigé du journal d'audit (voir `docs/PROTOCOL.md` §4).

### Authentification MCP : `static_bearer` vs `oauth`

Le canal harnais↔relay (`/mcp`) supporte deux modes via `MCP_AUTH_MODE` :

- `static_bearer` (défaut, MVP) : jeton unique `MCP_BEARER_TOKEN`, tous les outils accessibles.
- `oauth` : Resource Server OAuth 2.1, jetons Bearer JWT scopés (`session:connect`, `command:execute`, `session:terminate`, `client:provision`, `file:read`, `file:write`). Émission via :

  ```bash
  python -m relay.tokens issue --sub harness-operateur \
    --scopes session:connect,command:execute,session:terminate,client:provision \
    --ttl 3600
  ```

  `file:read`/`file:write` sont volontairement absents de cet exemple : ils
  doivent être demandés explicitement (`--scopes …,file:read,file:write`). Les
  jetons émis avant leur introduction ne les portent pas et se voient donc
  refuser `read_file`/`write_file` (`forbidden_scope`), sans action requise.

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
7. **Phase 6** (✓) : Client portable sans résidu (workspace temp auto-nettoyé, `--remove-on-exit`, build strippé)
8. **Phase 7** (en cours) : Tests (relay 205 + client 113 verts), observabilité, test d'intégration bout-en-bout relay↔client (dont l'aller-retour `write_file`/`read_file`)

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
# Relay (Python) — 205 tests
pip install -r relay/requirements.txt
pytest tests/relay -q

# Client (Go) — 113 tests
cd client && go test ./...
```

## Licence

[Voir LICENSE](LICENSE)

## Notes

- Ce projet est en développement actif.
- Le protocole et la sécurité peuvent évoluer entre les versions.
- Consulte [docs/PROTOCOL.md](docs/PROTOCOL.md) pour les détails d'intégration avec le harness.
