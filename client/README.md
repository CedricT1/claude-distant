# claude-distant — client

Agent Go portable lancé sur le PC distant, en deux variantes de binaire
(**console**, le livrable par défaut, et **GUI**, voir plus bas) construites
depuis la même base de code. Il se connecte en sortant au relay, affiche un
code de session à 9 chiffres, puis exécute les commandes envoyées par le
harnais (via le relay) en respectant une politique de garde-fou locale. Voir
`docs/PROTOCOL.md` à la racine du dépôt pour le protocole complet.

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

### Build cross-plateforme (binaires de distribution)

Voir `Makefile` et [`docs/PACKAGING.md`](../docs/PACKAGING.md) pour la
procédure complète (build reproductible, checksums, signature, prérequis
GUI). En bref :

```sh
make dist       # console : linux/amd64, linux/arm64, windows/amd64 -> dist/
make dist-gui   # GUI : linux/amd64, windows/amd64 -> dist/ (voir "Interface graphique" ci-dessous)
make checksums  # dist/SHA256SUMS -- couvre les deux familles
```

`RELAY_URL`/`CLIENT_TOKEN`/`IDENTITY_SECRET` (vides par défaut, voir
`Makefile`) personnalisent ces builds pour produire un binaire qui se lance
sans argument (§ « Binaire personnalisé » plus bas).

Équivalent manuel pour une seule cible (ex. Linux amd64) :

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
  -trimpath -ldflags "-s -w -X main.version=$(git describe --tags --always)" \
  -o dist/claude-distant-client-linux-amd64 .
```

Le binaire résultant est autonome (statique, `CGO_ENABLED=0`, strippé) : il
peut être copié et lancé directement depuis un dossier temporaire sur la
machine cible, sans installation, service, clé de registre ni autostart.

## Utilisation

```sh
./claude-distant-client \
  --url wss://relay.example.com/ws/client \
  --token <CLIENT_TOKEN>
```

Sans `--policy`, le client démarre en `auto` : les commandes envoyées par le
harnais s'exécutent sans invite de confirmation. Ajouter `--policy confirm`
pour valider chaque opération sensible une par une (voir « Politique de
garde-fou » ci-dessous).

### Binaire personnalisé : lancement sans argument

Un binaire compilé avec `RELAY_URL`/`CLIENT_TOKEN` (voir `Makefile` et
`docs/PACKAGING.md`) embarque ces valeurs au link (`-ldflags -X
main.buildRelayURL=... -X main.buildClientToken=...`, `client/buildconfig.go`)
et se lance alors **sans le moindre argument** :

```sh
./claude-distant-client
```

`parseConfig` résout chaque réglage dans l'ordre flag > variable
d'environnement > valeur compilée, le premier non vide gagnant ; l'erreur
« `--url` (ou `CLAUDE_DISTANT_URL`) est requis » n'est levée que si les
**trois** sources sont vides. Un binaire *générique* (ces variables laissées
vides à la compilation, le cas par défaut de `make dist`/`make dist-gui`) se
comporte donc exactement comme avant : `--url`/`--token` restent requis. Un
flag ou une variable d'environnement passés explicitement l'emportent
toujours sur la valeur compilée, y compris sur un binaire personnalisé — utile
pour rediriger ponctuellement un binaire déjà distribué vers un autre relay
sans le recompiler.

**Sécurité, sans détour** : une valeur compilée via `-ldflags -X` n'est ni
chiffrée ni obfusquée — un simple `strings` sur le binaire la récupère en
clair (`client/buildconfig.go`). Le binaire personnalisé lui-même EST donc un
secret à traiter comme tel (voir `docs/SECURITY.md`).

### Flags / variables d'environnement

| Flag | Env | Défaut | Description |
|---|---|---|---|
| `--url` | `CLAUDE_DISTANT_URL` | — (requis, sauf binaire personnalisé) | URL WebSocket du relay, ex. `wss://relay.example.com/ws/client` |
| `--token` | `CLAUDE_DISTANT_TOKEN` | — (requis, sauf binaire personnalisé) | Jeton Bearer pré-configuré |
| `--policy` | `CLAUDE_DISTANT_POLICY` | `auto` | Garde-fou : `auto` \| `confirm` \| `deny` |
| `--insecure-skip-verify` | — | `false` | Désactive la vérification TLS (dev uniquement, jamais en production) |
| `--remove-on-exit` | `CLAUDE_DISTANT_REMOVE_ON_EXIT` | `false` | À l'arrêt propre, supprime aussi le binaire lui-même (best-effort). Voir `docs/PACKAGING.md` §1 |
| `--ephemeral-code` | `CLAUDE_DISTANT_EPHEMERAL_CODE` | `false` | Revient au code de session aléatoire à chaque connexion (comportement d'avant l'adresse stable) au lieu du code stable dérivé de la machine. Voir « Adresse stable » ci-dessous |

Un flag l'emporte toujours sur la variable d'environnement correspondante.
`CLAUDE_DISTANT_REMOVE_ON_EXIT`/`CLAUDE_DISTANT_EPHEMERAL_CODE` acceptent
`1`/`true`/`yes`/`on` (insensible à la casse) comme valeurs activantes. Il
n'y a délibérément **pas** de `--identity-secret` : accepter ce secret en
ligne de commande viderait de son sens le fait de le compiler dans un binaire
personnalisé (`IDENTITY_SECRET`, voir `Makefile`) — seule la valeur compilée
(ou, à défaut, le sel par défaut du projet) est utilisée.

### Adresse stable

Par défaut (sans `--ephemeral-code`), le client redemande à chaque connexion
le **même** code de session à 9 chiffres, dérivé de façon déterministe de
l'identifiant de la machine (`client/identity.go:MachineID` —
`/etc/machine-id` sur Linux, `MachineGuid` de la registry sur Windows) et
d'un secret (`IDENTITY_SECRET` compilé, ou un sel par défaut du projet sinon).
Le relay honore ce code souhaité (`desired_code` du `register`) s'il est
encore libre, sinon retombe sur un code aléatoire pour cette session — voir
`docs/PROTOCOL.md` pour le détail complet (dérivation, TTL/heartbeat
inchangés, course à la reconnexion). Ni le TTL ni le heartbeat ne changent :
seule l'*adresse* est stable, pas la durée de vie de la session.

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

- `auto` (défaut) : toutes les commandes s'exécutent sans confirmation, y
  compris `read_file`/`write_file`. C'est le mode « le harnais travaille sans
  interrompre l'opérateur » : aucune invite locale n'est affichée, aucune
  `approval_response` n'est émise. Le garde-fou reste disponible à tout
  moment via `--policy confirm`/`deny`, ou l'interrupteur « mode
  automatique » de la GUI (à décocher pour repasser en `confirm` à chaud).
- `confirm` : les commandes classées **destructives** déclenchent
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
- **`read_file`/`write_file`** (`filetransfer.go`) — transfert de fichiers,
  réservé aux relays qui les dispatchent (le client annonce la capacité
  `file_transfer` à son `register`) :
  - plafond de **8 Mio** par transfert, dans les deux sens ;
  - `read_file` découpe le fichier en tranches de 192 Kio brutes (≈256 Kio en
    base64) envoyées en plusieurs messages `file_chunk` numérotés par `seq`,
    et supporte `offset`/`max_bytes` (troncature signalée par `truncated`) ;
  - `write_file` installe le contenu de façon **atomique** : fichier
    temporaire dans le même répertoire que la cible, puis `rename` — un échec
    à n'importe quelle étape laisse la cible préexistante intacte et ne laisse
    aucun temporaire derrière lui ; un chemin final qui est un lien symbolique
    est **remplacé** par un fichier régulier (conséquence du `rename`), pas
    suivi ;
  - erreurs stables portées par `result.error` (`file_not_found`,
    `permission_denied`, `is_a_directory`, `file_exists`, `file_too_large`,
    `invalid_base64`, `invalid_params`, `io_error`), classifiées via
    `errors.Is` sur les sentinelles `fs.Err*` plutôt que par correspondance de
    texte (dépendante de l'OS/la locale) ;
  - comme `run_shell`/`run_command`, systématiquement soumis au garde-fou
    local : en `confirm`, chaque lecture ou écriture déclenche une invite
    (`read_file <chemin>` / `write_file <chemin> (<n> octets)`), sans passer
    par la classification « destructif » ; en `deny`, toujours refusés. Voir
    `docs/PROTOCOL.md` §4 pour le détail protocolaire complet.

Les deux outils `run_shell`/`run_command` respectent `params.timeout`
(secondes) : au dépassement, le process (et son arbre de sous-processus) est
tué (`SIGKILL` + groupe de processus sur Linux, `taskkill /T /F` sur
Windows), et `result` est renvoyé avec `error:"timeout"`.

## Interface graphique (GUI)

En plus du binaire console (le livrable par défaut, utilisable partout — y
compris sans affichage), `make dist-gui` produit une variante avec fenêtre
Fyne (`client/gui.go`, tag de build `gui`), destinée aux postes de bureau
Windows/Ubuntu où un opérateur humain préfère une fenêtre à un terminal. Le
point d'entrée commun est `runUI` : `client/gui_stub.go` (tag `!gui`) le
délègue tel quel à la boucle console historique, `client/gui.go` (tag `gui`)
en fournit la version Fyne. Voir `docs/PACKAGING.md` pour le pourquoi du
dédoublement et les prérequis de build.

> **Dette technique connue** : la variante GUI porte sa propre boucle de
> connexion (`guiRunForever`/`guiRunSession`), qui duplique celle de
> `main.go`. Elle appelle les mêmes primitives de protocole (`DialRelay`,
> `resolveDesiredCode`, `heartbeatLoop`…), mais garde un `PolicyController`
> et un `ActivityLog` vivants d'une reconnexion à l'autre, ce que la boucle
> console ne fait pas. Conséquence à garder en tête : **une évolution du
> protocole doit être répercutée aux deux endroits**, sous peine de
> divergence silencieuse entre les deux variantes.

À l'usage, la fenêtre GUI reprend les mêmes informations et décisions que la
console, sous une autre forme :

- **En-tête** : le code de session en très gros caractères, groupé
  `784 123 678`, avec un bouton « Copier » ; l'état de la connexion (connecté
  / reconnexion en cours / déconnecté) ; le hostname et l'OS de la machine.
- **Mode automatique** : un interrupteur « Mode automatique — ne plus
  demander de confirmation » bascule la politique de garde-fou de `confirm`
  vers `auto` en cours de session (et inversement), sans redémarrer le
  client. Il reflète la politique de lancement, donc **coché par défaut**
  (`--policy` valant `auto` en l'absence de flag) ; le décocher repasse la
  session en `confirm` à chaud. Un avertissement visible et permanent reste
  affiché tant qu'il est actif : plus aucune commande ni opération fichier
  (`read_file`/`write_file` compris) n'est alors soumise à confirmation
  locale. C'est une décision de l'utilisateur du poste, à prendre en
  connaissance de cause — voir `docs/SECURITY.md`.
- **Confirmation** : en mode `confirm`, une boîte de dialogue Fyne remplace
  l'invite console, avec les mêmes trois réponses — Autoriser, Refuser,
  Toujours (mémorisation de cette commande exacte pour le reste de la
  session, comme en console).
- **Journal d'activité** : en bas de fenêtre, le journal d'activité en
  direct (connexions, commandes, lectures/écritures de fichiers,
  approbations, résultats), redimensionnable à la souris, avec un bouton
  « Agrandir » (bascule vue réduite / plein écran) et un bouton
  « Enregistrer le log… » qui écrit l'historique complet de la session dans
  un fichier choisi par l'utilisateur.

Le binaire GUI accepte les mêmes flags/variables d'environnement que la
console (tableau ci-dessus), y compris le lancement sans argument d'un
binaire personnalisé (`RELAY_URL`/`CLIENT_TOKEN` compilés) — double-clic et
c'est parti, sans terminal à ouvrir.

## Structure du code

| Fichier | Rôle |
|---|---|
| `main.go` | flags/env (`parseConfig`), boucle de connexion/reconnexion, affichage du code de session, orchestration de l'arrêt propre |
| `buildconfig.go` | `buildRelayURL`/`buildClientToken`/`buildIdentitySecret` : valeurs injectées au link par un build personnalisé (`Makefile`) |
| `identity.go` (+ `identity_linux.go`, `identity_windows.go`) | `MachineID`/`DeriveSessionCode` : adresse stable (§ « Adresse stable » ci-dessus) |
| `wsconn.go` | connexion WebSocket (dial, JSON I/O thread-safe, deadlines) |
| `executor.go` | exécution `run_shell`/`run_command` (sélection d'interpréteur, streaming, timeout, répertoire de travail = workspace) |
| `filetransfer.go` | exécution `read_file`/`write_file` (chunking en lecture, écriture atomique, codes d'erreur stables) |
| `sysinfo.go` (+ `sysinfo_linux.go`, `sysinfo_windows.go`) | `system_info` cross-plateforme |
| `proc_linux.go`, `proc_windows.go` | démarrage/arrêt de l'arbre de processus par OS |
| `policy.go` | garde-fou local : classification destructive, invite `confirm`, `PolicyController` (bascule `auto`/`confirm` à chaud, utilisé par la GUI) |
| `activitylog.go` | `ActivityLog` : journal d'activité borné, thread-safe, alimente le panneau « journal » de la GUI et le bouton « Enregistrer le log… » |
| `protocol.go` | types Go des messages du protocole |
| `workspace.go` | répertoire de travail temporaire dédié (`NewWorkspace`/`Cleanup`), « sans résidu » |
| `lifecycle.go` | `RunGuarded` : garantit le nettoyage à la sortie, y compris sur panic |
| `secrets.go` | `SecretBytes` : effacement best-effort des secrets (token) en mémoire |
| `cleanup_binary.go` | `--remove-on-exit` : suppression best-effort du binaire lui-même à l'arrêt |
| `gui_stub.go` (`!gui`) / `gui.go` (`gui`) | point d'entrée commun (`runUI`) : stub console par défaut, fenêtre Fyne derrière `-tags gui` (§ « Interface graphique » ci-dessus) |
| `*_test.go` | tests unitaires (sérialisation protocole, sélection de shell, classification destructive, parsing des flags, workspace/remove-on-exit/secrets, adresse stable, journal d'activité) |

## Sans résidu

Le client ne s'installe pas : pas de service, pas de clé de registre, pas
d'autostart. Voir `docs/PACKAGING.md` pour le détail complet du modèle
« sans résidu » et le build portable ; en résumé :

- **Aucun log sur disque par défaut** : toute la sortie va sur la console
  (stdout/stderr) uniquement.
- **Répertoire de travail temporaire dédié** (`workspace.go`,
  `os.MkdirTemp`), utilisé comme répertoire de travail des commandes
  exécutées (`run_shell`/`run_command`), **supprimé intégralement** à la
  sortie — arrêt propre, Ctrl-C/SIGTERM, ou panic (`lifecycle.go:RunGuarded`).
- **Secrets effacés en mémoire** (`secrets.go:SecretBytes.Zero()`) : le
  jeton Bearer n'est jamais stocké en `string` Go immuable, et est écrasé
  par des zéros à la sortie.
- **`--remove-on-exit` (désactivé par défaut)** : supprime aussi le binaire
  lui-même à l'arrêt (best-effort ; direct sous Linux, via un script
  détaché sous Windows — voir `cleanup_binary.go` et `docs/PACKAGING.md` §1).

Un arrêt (Ctrl-C ou signal) ferme proprement la connexion WebSocket (frame
de fermeture) avant de quitter.
