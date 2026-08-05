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
| `register` | `os` (`linux`\|`windows`), `hostname`, `version`, `capabilities?` (liste de str) | annonce à la connexion |
| `heartbeat` | — | maintien de session |
| `stream` | `request_id`, `stream` (`stdout`\|`stderr`), `data` (str) | sortie partielle d'une commande |
| `file_chunk` | `request_id`, `seq` (int, croissant depuis 0), `data` (base64) | tranche d'un fichier lu (§4) |
| `result` | `request_id`, `exit_code` (int), `error` (str\|null), `meta?` (obj) | fin d'exécution |
| `approval_response` | `request_id`, `approved` (bool) | réponse au garde-fou local |

Les champs suffixés `?` sont **optionnels et additifs** : un client antérieur à
leur introduction ne les envoie pas, et le relay se comporte alors exactement
comme avant (voir « Négociation de capacités et rétrocompatibilité » plus bas).
Seul `result` termine une requête — `approval_response`
et `file_chunk` sont des messages intermédiaires.

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

### Négociation de capacités et rétrocompatibilité
Le champ optionnel `capabilities` du `register` déclare les extensions que ce
client sait traiter. La seule définie à ce jour est `"file_transfer"`
(`read_file`/`write_file`, §4), annoncée par les clients ≥ 0.2.0. Absent, `null`
ou malformé → le relay retient `()`, c'est-à-dire « aucune capacité ».

Le relay refuse alors un outil dépendant d'une capacité **avant tout dispatch**,
plutôt que d'envoyer au client une `command` dont il ignore le `tool` (qu'il
laisserait tomber silencieusement, jusqu'au timeout côté harnais et sans le
moindre diagnostic). Le harnais reçoit immédiatement :

```json
{"status":"error","error":"unsupported_by_client",
 "detail":"le client distant (version '0.1.0') ne supporte pas le transfert de fichiers ; mettez-le à jour vers une version ≥ 0.2.0",
 "client_version":"0.1.0","capabilities":[]}
```

`connect_session` retourne également la liste `capabilities`, ce qui permet de
savoir *avant* d'essayer ce qui est utilisable sur une cible donnée.

Les quatre combinaisons se comportent donc ainsi :

| | ancien relay | nouveau relay |
|---|---|---|
| **ancien client** | inchangé | inchangé ; outils fichiers → `unsupported_by_client` |
| **nouveau client** | `capabilities` ignoré (champ inconnu), reste inchangé | transfert de fichiers disponible |

Aucun champ existant n'a été renommé ni re-typé, et aucun champ requis n'a été
ajouté : toute addition est optionnelle, dans les deux sens.

### Garde-fou local (politique configurable)
Le client est lancé avec une politique `--policy auto|confirm|deny` :
- `auto` : exécute sans confirmation.
- `confirm` : pour les commandes classées destructives, affiche localement
  « Le harnais veut exécuter : `X` [Autoriser/Refuser/Toujours] (o/N/t) ».
  Sans approbation → refus. Répondre « toujours » (`t`) approuve la commande
  et mémorise cette commande exacte pour le reste de la session : elle ne
  redéclenchera plus de prompt tant que le client tourne (mémorisation en
  mémoire uniquement, perdue à chaque redémarrage du client).
- `deny` : refuse les commandes destructives.
En mode `confirm`, le client attend la décision de l'utilisateur avant d'exécuter,
puis répond via `result` (avec `error:"refused_by_user"` si refusé).

`read_file` et `write_file` sont un cas à part : ils déclenchent **toujours** la
confirmation en mode `confirm` (et sont **toujours** refusés en mode `deny`),
sans passer par la classification « destructif » des commandes shell — lire un
fichier l'exfiltre de la machine, en écrire un la modifie. La description
soumise à l'opérateur (et mémorisée par « toujours ») est `read_file <chemin>`
ou `write_file <chemin> (<n> octets)`.

---

## 2. Canal Harnais ↔ Relay (MCP)

Endpoint MCP Streamable HTTP (`/mcp`), servi en HTTP interne derrière un
reverse proxy TLS strict en production (voir `docs/SECURITY.md`). Auth
sélectionnée via `MCP_AUTH_MODE` :

- `static_bearer` (défaut, MVP) : jeton Bearer unique (`MCP_BEARER_TOKEN`),
  tous les outils accessibles à quiconque le détient.
- `oauth` (phase 5) : Resource Server OAuth 2.1, jetons Bearer **JWT scopés**
  (HS256, `MCP_JWT_SECRET`) — voir `relay/jwt_auth.py`, émission via
  `python -m relay.tokens issue`. Chaque outil requiert un scope précis
  (colonne « Scope » ci-dessous) ; un jeton sans ce scope reçoit
  `{"status": "error", "error": "forbidden_scope"}` et est journalisé dans
  l'audit. Un jeton absent/invalide/expiré est rejeté au niveau transport
  (401), avant tout appel d'outil.

Chaque outil prend un `session_code` pour cibler le bon client (sauf
`issue_client_token`, qui n'en a pas besoin).

| Outil | Paramètres | Retour | Scope (mode oauth) |
|---|---|---|---|
| `connect_session` | `session_code` | statut, `os`, `hostname`, `capabilities` de la cible | `session:connect` |
| `system_info` | `session_code` | OS, uptime, RAM, CPU | — (non protégé par scope, cf. `TOOL_SCOPES`) |
| `run_command` | `session_code`, `command`, `timeout?` | stdout/stderr, `exit_code` | `command:execute` |
| `run_shell` | `session_code`, `command`, `shell?` (`auto`\|`powershell`\|`pwsh`\|`bash`\|`sh`), `timeout?` | stdout/stderr, `exit_code` | `command:execute` |
| `read_file` | `session_code`, `path`, `offset?`, `max_bytes?` | `content_base64`, `size`, `sha256`, `truncated` (§4) | `file:read` |
| `write_file` | `session_code`, `path`, `content_base64`, `mode?`, `create_dirs?`, `overwrite?` | `bytes_written`, `sha256` (§4) | `file:write` |
| `terminate_session` | `session_code` | statut (kill-switch, phase 5) | `session:terminate` |
| `issue_client_token` | `ttl_seconds?` | `token` client `per_session`, `expires_in` (phase 5) | `client:provision` |

`run_shell` avec `shell="auto"` → PowerShell sur Windows, Bash sur Linux (selon l'OS
détecté à `register`). Le relay traduit l'appel MCP en message `command` vers le client
et agrège les `stream`/`result` renvoyés.

---

## 3. Codes de session
- 9 chiffres, format affiché `784 123 678`.
- TTL court (par défaut 30 min), régénérés à chaque connexion client.
- Anti-collision (unicité dans le store), rate-limit sur les tentatives `connect_session`.

---

## 4. Transfert de fichiers

Réservé aux clients déclarant la capacité `file_transfer` (§1). Plafond
commun aux deux sens : **8 MiB** de contenu décodé.

### Lecture (`read_file`) — chunkée client → relay
Le contenu remonte en plusieurs `file_chunk` de 192 Kio d'octets bruts
(≈ 256 Kio de base64 sur le fil), que le relay réordonne par `seq` avant de
concaténer les octets décodés. Le `result` final porte le `meta` faisant foi.

```
Relay  → command    {request_id:"r1", tool:"read_file",
                     params:{path:"/var/log/syslog", max_bytes:262144}}
Client → file_chunk {request_id:"r1", seq:0, data:"<base64 de 192 Kio>"}
Client → file_chunk {request_id:"r1", seq:1, data:"<base64 du reste>"}
Client → result     {request_id:"r1", exit_code:0, error:null,
                     meta:{path:"/var/log/syslog", size:262144,
                           sha256:"…", truncated:true}}
```

`offset`/`max_bytes` ne sont transmis au client que lorsqu'ils sont signifiants
(`offset` non nul, `max_bytes` fourni) : une lecture simple envoie exactement
`params:{path:…}` et se borne alors au plafond de 8 MiB. `truncated:true`
signale qu'il restait des octets au-delà de la limite : au harnais de relancer
la lecture avec `offset:262144` pour obtenir la suite.

### Écriture (`write_file`) — une seule trame relay → client
Il n'y a **pas** de découpage relay → client : tout le base64 tient dans
l'unique message `command`. C'est précisément d'où vient le plafond de 8 MiB.
La validité du base64 et la taille sont vérifiées côté relay *avant* dispatch
(`invalid_base64` / `file_too_large`), inutile de réveiller le client pour une
charge qu'on sait déjà invalide.

```
Relay  → command {request_id:"r2", tool:"write_file",
                  params:{path:"/etc/motd", content_base64:"<base64>",
                          mode:"0644", create_dirs:true}}
Client → result  {request_id:"r2", exit_code:0, error:null,
                  meta:{path:"/etc/motd", bytes_written:1234, sha256:"…"}}
```

L'écriture est **atomique** : fichier temporaire dans le même répertoire, puis
`rename`. Un échec à n'importe quelle étape laisse la cible préexistante
intacte et ne laisse aucun temporaire derrière lui. `mode` est une chaîne
octale (`"0644"`), appliquée au temporaire avant le `rename` et en best-effort
(sur Windows, `chmod` est largement sans effet, ce qui ne fait pas échouer le
transfert) ; sans `mode` exploitable, les permissions du fichier remplacé sont
conservées, ou `0644` s'il s'agit d'une création. `create_dirs:true` crée
l'arborescence parente manquante — sinon un répertoire parent absent fait
échouer le transfert avec `file_not_found`.

### Codes d'erreur (portés par `result.error`, puis par le retour de l'outil)
| code | sens |
|---|---|
| `file_not_found` | le chemin n'existe pas |
| `permission_denied` | droits insuffisants côté client |
| `is_a_directory` | le chemin désigne un répertoire |
| `file_exists` | la cible existe et `overwrite:false` |
| `file_too_large` | contenu au-delà du plafond de 8 MiB |
| `invalid_base64` | `content_base64` illisible |
| `invalid_params` | `path` vide, `offset`/`max_bytes` négatif, params indécodables |
| `io_error` | toute autre erreur d'E/S |

Un transfert en échec est rapporté par un `result` portant `exit_code:1` et le
code dans `error` ; un refus du garde-fou local porte `exit_code:126` et
`refused_by_policy`/`refused_by_user`. L'outil MCP relaie tel quel :
`{"status":"error","error":<code>,"path":…,"exit_code":…}`.

À quoi s'ajoutent, côté relay et sans dispatch : `unsupported_by_client` (§1),
`session_not_found`, `forbidden_scope` (§2), et les codes habituels du canal
(`client_disconnected`, `timeout`, `denied`).

### Notes
- Le chemin n'est **pas** confiné à un workspace : ces outils servent à
  l'administration système. `~` n'est pas expansé.
- Le garde-fou local s'applique systématiquement en mode `confirm`/`deny` (§1).
- L'allow/denylist du relay (`COMMAND_DENYLIST`/`COMMAND_ALLOWLIST`, voir
  `relay/command_policy.py`) s'applique aussi au **chemin** (`params.path`)
  pour `read_file`/`write_file`, au même titre que le champ `command` pour
  `run_command`/`run_shell` : un motif de denylist qui matche le chemin refuse
  le transfert avant tout dispatch au client.
- Sémantique des liens symboliques : à l'**écriture**, si le chemin final
  (dernier composant) est un lien symbolique, l'installation atomique
  (fichier temporaire + `rename`, voir §4 « Écriture ») **remplace le lien
  par un fichier régulier** plutôt que d'écrire au travers — `rename`
  remplace l'entrée de répertoire elle-même, il ne suit pas la cible du lien.
  Un **répertoire parent** symbolique, lui, est suivi normalement par l'OS. À
  la **lecture**, les liens symboliques sont suivis : `read_file` renvoie le
  contenu de la cible du lien. Voir `docs/SECURITY.md` pour les implications
  côté sécurité.
- Le contenu (`content_base64`) est **rédigé** du journal d'audit, remplacé par
  un marqueur de taille : le journal reste lisible et ne recopie pas de données
  potentiellement sensibles.
