# Protocole `claude-distant` (MVP)

Deux canaux :
1. **Client ↔ Relay** : WebSocket sur TLS, messages JSON.
2. **Harnais ↔ Relay** : MCP Streamable HTTP, auth Bearer.

---

## 1. Canal Client ↔ Relay (WebSocket)

Le client se connecte en **sortant** à `wss://<relay>/ws/client` avec l'en-tête
`Authorization: Bearer <CLIENT_TOKEN>` (token pré-configuré).

Chaque message est un objet JSON avec un champ `type`.

### Client → Relay
| type | champs | rôle |
|---|---|---|
| `register` | `os` (`linux`\|`windows`), `hostname`, `version` | annonce à la connexion |
| `heartbeat` | — | maintien de session |
| `stream` | `request_id`, `stream` (`stdout`\|`stderr`), `data` (str) | sortie partielle d'une commande |
| `result` | `request_id`, `exit_code` (int), `error` (str\|null) | fin d'exécution |
| `approval_response` | `request_id`, `approved` (bool) | réponse au garde-fou local |

### Relay → Client
| type | champs | rôle |
|---|---|---|
| `registered` | `session_code` (str, 9 chiffres, ex. `"784123678"`) | code attribué |
| `command` | `request_id`, `tool` (str), `params` (obj) | commande à exécuter |
| `heartbeat_ack` | — | accusé |

### Séquence type
```
Client → register {os:"linux", hostname:"srv01"}
Relay  → registered {session_code:"784123678"}   # affiché à l'écran
...
Relay  → command {request_id:"r1", tool:"run_shell",
                  params:{command:"df -h", shell:"auto", timeout:60}}
Client → stream  {request_id:"r1", stream:"stdout", data:"Filesystem ..."}
Client → result  {request_id:"r1", exit_code:0, error:null}
```

### Garde-fou local (politique configurable)
Le client est lancé avec une politique `--policy auto|confirm|deny` :
- `auto` : exécute sans confirmation.
- `confirm` : pour les commandes classées destructives, affiche localement
  « Le harnais veut exécuter : `X` [Autoriser/Refuser] ». Sans approbation → refus.
- `deny` : refuse les commandes destructives.
En mode `confirm`, le client attend la décision de l'utilisateur avant d'exécuter,
puis répond via `result` (avec `error:"refused_by_user"` si refusé).

---

## 2. Canal Harnais ↔ Relay (MCP)

Endpoint MCP Streamable HTTP, auth **Bearer** (migration OAuth 2.1 en phase 5).
Chaque outil prend un `session_code` pour cibler le bon client.

| Outil | Paramètres | Retour |
|---|---|---|
| `connect_session` | `session_code` | statut, `os`, `hostname` de la cible |
| `system_info` | `session_code` | OS, uptime, RAM, CPU |
| `run_command` | `session_code`, `command`, `timeout?` | stdout/stderr, `exit_code` |
| `run_shell` | `session_code`, `command`, `shell?` (`auto`\|`powershell`\|`pwsh`\|`bash`\|`sh`), `timeout?` | stdout/stderr, `exit_code` |

`run_shell` avec `shell="auto"` → PowerShell sur Windows, Bash sur Linux (selon l'OS
détecté à `register`). Le relay traduit l'appel MCP en message `command` vers le client
et agrège les `stream`/`result` renvoyés.

---

## 3. Codes de session
- 9 chiffres, format affiché `784 123 678`.
- TTL court (par défaut 30 min), régénérés à chaque connexion client.
- Anti-collision (unicité dans le store), rate-limit sur les tentatives `connect_session`.
