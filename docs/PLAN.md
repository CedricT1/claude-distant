# Plan de développement — `claude-distant`

Accès distant piloté par un harnais IA (type TeamViewer/AnyDesk, mais l'opérateur
est Claude) pour faire de l'administration système sur un PC Windows ou Ubuntu,
**sans installation** et **sans trace** sur la machine distante.

## 1. Architecture

```
┌─────────────────┐         ┌──────────────────────┐         ┌──────────────────┐
│  PC distant     │  WS/TLS │   RELAY (Docker)     │   MCP   │  Harnais (Claude)│
│  (Win / Ubuntu) │ ───────▶│  - Broker de session │◀─────── │  + opérateur     │
│  client Go      │ sortant │  - Serveur MCP auth  │  Bearer │                  │
│  portable       │         │  - Audit / routage   │  /OAuth │                  │
└─────────────────┘         └──────────────────────┘         └──────────────────┘
    affiche « 784 123 678 »        mappe code→client              « connecte-toi à
                                                                    784 123 678 »
```

### Invariants
- Le PC distant n'ouvre **aucun port entrant** : connexion **sortante** WebSocket/TLS.
- Client **portable, sans installation, sans trace** : binaire unique lancé depuis un
  dossier temporaire, auto-nettoyage à la fermeture (pas de service, clé registre, autostart).
- Le harnais ne parle jamais au PC directement : il passe par le relay via **MCP
  authentifié**, en ciblant un **numéro de session** éphémère (9 chiffres).
- **Consentement + garde-fou local** : l'utilisateur voit le code et approuve/refuse
  les commandes sensibles selon la politique choisie.

## 2. Stack retenue

| Composant | Techno | Notes |
|---|---|---|
| Relay / broker | Python 3.12 + FastAPI + `websockets` | même langage que l'écosystème existant |
| Serveur MCP | MCP Python SDK (Streamable HTTP) | auth Bearer/OAuth natif |
| Client PC distant | **Go** (binaire statique unique) | portable, sans runtime, sans trace |
| Transport client↔relay | WebSocket over TLS (wss) | sortant, firewall/NAT-friendly |
| Session store | Redis | TTL des codes, rate-limit, multi-instance |
| Déploiement | Docker + docker-compose | |

### Décisions validées
- **Client : Go** (binaire unique).
- **Auth MCP : Bearer d'abord, puis OAuth 2.1** en phase de durcissement.
- **Garde-fou : politique configurable** (`auto` / `confirm` / `deny`) au lancement du client.

## 3. Phases

### Phase 0 — Cadrage sécurité & protocole
- Modèle de menace (le relay = RCE-as-a-service), modèle de consentement.
- `docs/PROTOCOL.md` : messages client↔relay JSON (`register`, `assign_code`,
  `command`, `stream_chunk`, `result`, `heartbeat`, `approval_request/response`).
- Schéma des outils MCP (§4).

### Phase 1 — Broker de session (relay MVP)
- WebSocket : auth token pré-configuré, génération code 9 chiffres, TTL court,
  anti-collision, rate-limit.
- Routage `code → connexion client` (Redis), heartbeat, expiration.

### Phase 2 — Client Go minimal
- Connexion à l'URL configurée + token, affichage du code.
- Boucle : reçoit commande → exécute → renvoie résultat (streaming) ; arrêt propre.

### Phase 3 — Serveur MCP sur le relay
- Endpoint MCP Streamable HTTP + **auth Bearer**.
- `connect_session(code)` valide et ouvre le canal ; `run_command(...)` route vers
  le client et streame la sortie ; gestion session expirée / client déconnecté / timeout.

### Phase 4 — Couche sysadmin cross-platform
- Détection OS + outils : `system_info`, `disk_check`/`disk_usage`,
  `list_processes`/`kill_process`, `service_status`/`service_restart`,
  `logs` (journalctl / Event Log), `read_file`/`write_file`/`list_dir`,
  `package_update` (apt / Windows Update), `run_command`.

### Phase 5 — Durcissement sécurité
- **Migration OAuth 2.1** côté harnais ; token par-session court côté client.
- **Confirmation locale configurable** (`auto`/`confirm`/`deny`) pour les commandes destructives.
- Journal d'audit immuable, allowlist/denylist, quotas, kill-switch de session, TLS strict.

### Phase 6 — Client portable « sans trace »
- Build Go binaire unique Win/Linux, exécution en temp, auto-nettoyage, signature (option).

### Phase 7 — Tests, observabilité, doc
- Tests unitaires + intégration bout-en-bout, métriques/logs structurés,
  healthchecks Docker, guides opérateur/utilisateur.

## 4. Outils MCP exposés au harnais

```
connect_session(code)
system_info()
disk_check() / disk_usage()
list_processes() / kill_process(pid)
service_status(name) / service_restart(name)
logs(source, lines)
read_file(path) / write_file(path) / list_dir(path)
package_update()
run_command(cmd, timeout)          # soumis à la politique de confirmation
```

## 5. Structure de dépôt cible

```
claude-distant/
├── relay/            # broker WS + serveur MCP (FastAPI)
│   ├── broker.py
│   ├── mcp_server.py
│   └── auth.py
├── client/           # agent PC distant (Go)
├── shared/           # schémas de protocole partagés
├── docker/           # Dockerfile relay + compose
├── docs/             # ARCHITECTURE, PROTOCOL, SECURITY, PLAN
└── tests/
```

## 6. Sécurité

Système = exécuteur de commandes à distance privilégié. Usage **autorisé et
supervisé** uniquement (utilisateur présent et consentant) :
- Consentement explicite (code partagé) + confirmation locale des actions destructives.
- Audit complet et immuable de chaque commande.
- Sessions éphémères à TTL court, révocables (kill-switch).
