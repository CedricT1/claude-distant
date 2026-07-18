# claude-distant — client

Agent Go portable (binaire statique unique) lancé sur le PC distant. Il se
connecte en sortant au relay, affiche un code de session à 9 chiffres, puis
exécute les commandes envoyées par le harnais (via le relay) en respectant
une politique de garde-fou locale. Voir `docs/PROTOCOL.md` à la racine du
dépôt pour le protocole complet.

## Build

Go 1.22+ requis. Aucune dépendance système autre que le module Go
`github.com/gorilla/websocket` (WebSocket) — le reste n'utilise que la
bibliothèque standard (y compris `system_info`, sans dépendance lourde type
gopsutil).

Depuis `client/` :

```sh
go build ./...          # build natif (vérification rapide)
go vet ./...             # analyse statique
go test ./...             # tests unitaires
```

### Build cross-plateforme

```sh
# Linux amd64
GOOS=linux   GOARCH=amd64 go build -o dist/claude-distant-client-linux-amd64 .

# Windows amd64
GOOS=windows GOARCH=amd64 go build -o dist/claude-distant-client-windows-amd64.exe .
```

Le binaire résultant est autonome (statique) : il peut être copié et lancé
directement depuis un dossier temporaire sur la machine cible, sans
installation, service, clé de registre ni autostart.

## Utilisation

```sh
./claude-distant-client \
  --url wss://relay.example.com/ws/client \
  --token <CLIENT_TOKEN> \
  --policy confirm
```

### Flags / variables d'environnement

| Flag | Env | Défaut | Description |
|---|---|---|---|
| `--url` | `CLAUDE_DISTANT_URL` | — (requis) | URL WebSocket du relay, ex. `wss://relay.example.com/ws/client` |
| `--token` | `CLAUDE_DISTANT_TOKEN` | — (requis) | Jeton Bearer pré-configuré |
| `--policy` | `CLAUDE_DISTANT_POLICY` | `confirm` | Garde-fou : `auto` \| `confirm` \| `deny` |
| `--insecure-skip-verify` | — | `false` | Désactive la vérification TLS (dev uniquement, jamais en production) |

Un flag l'emporte toujours sur la variable d'environnement correspondante.

Au démarrage, le client :
1. se connecte au relay et envoie `register` (OS détecté, hostname, version) ;
2. affiche le code de session reçu (`registered`), formaté `784 123 678` ;
3. exécute en boucle les `command` reçus (`run_shell`, `run_command`,
   `system_info`), streame stdout/stderr, puis renvoie `result` ;
4. envoie un `heartbeat` toutes les 20 s ;
5. se reconnecte automatiquement (backoff exponentiel + jitter, 1s→30s) si la
   connexion tombe, jusqu'à interruption (Ctrl-C / SIGTERM), gérée
   proprement (fermeture de la connexion WebSocket puis arrêt).

### Politique de garde-fou (`--policy`)

- `auto` : toutes les commandes s'exécutent sans confirmation.
- `confirm` (défaut) : les commandes classées **destructives** déclenchent
  une invite locale `Le harnais veut exécuter : <commande> [Autoriser/Refuser]`
  et attendent la réponse de l'opérateur avant exécution. Un refus renvoie
  `result` avec `error:"refused_by_user"`.
- `deny` : les commandes destructives sont automatiquement refusées
  (`error:"refused_by_policy"`), sans invite.

La classification "destructive" (`policy.go`, `destructivePatterns`) est une
liste simple et extensible d'expressions régulières couvrant entre autres :
`rm -rf`/`rm -fr`, `Remove-Item -Recurse`/`-Force`, `mkfs`, `dd ... of=`,
`wipefs`, `fdisk`/`parted`, `diskpart`, `format`, écriture directe sur
`/dev/sd*`, `shutdown`/`reboot`/`poweroff`/`Restart-Computer`,
`userdel`/`deluser`, `reg delete`, `iptables -F`, et le fork bomb classique
`:(){ :|:& };:`. Pour l'étendre, ajouter une entrée à `destructivePatterns`
dans `policy.go`.

### Outils exécutés côté client

- **`run_shell`** — exécute `params.command` dans un interpréteur choisi via
  `params.shell` :
  - `auto` (défaut) : PowerShell (`pwsh` si présent, sinon `powershell`) sur
    Windows ; Bash sur Linux.
  - overrides explicites : `powershell`, `pwsh`, `bash`, `sh`.
  - stdout/stderr sont streamés séparément (`stream` messages) ; PowerShell
    est forcé en UTF-8 (entrée/sortie) pour un décodage correct quel que
    soit le code page actif de la console.
- **`run_command`** — exécute une commande simple sans shell (argv splitté
  en respectant guillemets simples/doubles et échappements).
- **`system_info`** — OS, hostname, uptime, CPU (nombre de cœurs), RAM
  totale/disponible (Mo). Implémenté sans dépendance lourde : `/proc/uptime`
  et `/proc/meminfo` sur Linux, `GetTickCount64`/`GlobalMemoryStatusEx` de
  `kernel32.dll` via `syscall` sur Windows.

Les deux outils `run_shell`/`run_command` respectent `params.timeout`
(secondes) : au dépassement, le process (et son arbre de sous-processus) est
tué (`SIGKILL` + groupe de processus sur Linux, `taskkill /T /F` sur
Windows), et `result` est renvoyé avec `error:"timeout"`.

## Structure du code

| Fichier | Rôle |
|---|---|
| `main.go` | flags/env (`parseConfig`), boucle de connexion/reconnexion, affichage du code de session |
| `wsconn.go` | connexion WebSocket (dial, JSON I/O thread-safe, deadlines) |
| `executor.go` | exécution `run_shell`/`run_command` (sélection d'interpréteur, streaming, timeout) |
| `sysinfo.go` (+ `sysinfo_linux.go`, `sysinfo_windows.go`) | `system_info` cross-plateforme |
| `proc_linux.go`, `proc_windows.go` | démarrage/arrêt de l'arbre de processus par OS |
| `policy.go` | garde-fou local (classification destructive + invite `confirm`) |
| `protocol.go` | types Go des messages du protocole |
| `*_test.go` | tests unitaires (sérialisation protocole, sélection de shell, classification destructive, parsing des flags) |

## Sans trace

Le client ne s'installe pas : pas de service, pas de clé de registre, pas
d'autostart. Il ne crée aucun fichier temporaire propre à son
fonctionnement ; un arrêt (Ctrl-C ou signal) ferme proprement la connexion
WebSocket (frame de fermeture) avant de quitter.
