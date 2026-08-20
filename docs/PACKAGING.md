# Packaging — client portable « sans résidu » (Phase 6)

Ce document couvre la **Phase 6** de [`docs/PLAN.md`](PLAN.md) : livrer le
client PC distant (`client/`) sous forme de **binaire unique portable**
Windows/Linux qui ne laisse **aucun résidu** sur la machine une fois fermé
— pas de service, pas de clé de registre/autostart, pas de fichier
résiduel. Voir aussi [`docs/PROTOCOL.md`](PROTOCOL.md) pour le protocole et
`client/README.md` pour la référence complète des flags/variables
d'environnement.

## 1. Modèle « sans résidu »

Le client n'installe rien et ne persiste rien par défaut :

- **Aucun service, clé de registre ou autostart.** Le client est un
  exécutable autonome (`CGO_ENABLED=0`, statique) lancé manuellement depuis
  n'importe quel dossier (Bureau, clé USB, dossier temporaire...).
- **Aucun log sur disque par défaut.** Toute la sortie (`fmt.Println`,
  `log.Printf`) va sur la console (stdout/stderr) uniquement ; rien n'est
  écrit dans un fichier de log.
- **Un unique répertoire de travail temporaire, nettoyé à la sortie.** Au
  démarrage, le client crée un dossier temporaire dédié via
  `NewWorkspace()` (`client/workspace.go`, sous `os.TempDir()`, permissions
  `0700` best-effort). C'est le **seul** endroit où le client écrirait quoi
  que ce soit sur disque (scripts intermédiaires, fichiers de travail) ; les
  commandes exécutées (`run_shell`/`run_command`) y ont leur répertoire de
  travail par défaut (`exec.Cmd.Dir`), de sorte que tout fichier qu'une
  commande crée sans chemin absolu atterrit dans ce dossier plutôt que dans
  le profil de l'utilisateur.
- **Nettoyage garanti à la sortie — y compris sur panic.** `main()`
  encapsule toute l'exécution dans `RunGuarded(cleanup, run)`
  (`client/lifecycle.go`) : `cleanup` (suppression du workspace,
  effacement du token en mémoire, suppression optionnelle du binaire) s'exécute
  **exactement une fois**, que `run` retourne normalement, retourne une
  erreur, ou **panique** — le panic est re-levé après coup pour ne jamais
  masquer un vrai bug. Le même chemin de sortie est emprunté sur Ctrl-C/
  SIGTERM (`signal.NotifyContext` annule le contexte, la boucle de
  connexion retourne normalement, puis `RunGuarded` nettoie).
- **Effacement best-effort des secrets en mémoire.** Le jeton Bearer n'est
  jamais stocké en `string` Go (immuable, non écrasable) mais dans un
  `*SecretBytes` (`client/secrets.go`) — un `[]byte` que `Zero()` écrase
  avec des zéros à la sortie. Best-effort : cela ne peut pas rattraper les
  copies déjà produites par d'éventuels appels antérieurs à `.String()`
  (utilisé une seule fois, juste avant `DialRelay`), ni empêcher toute copie
  que le runtime Go aurait pu faire de son côté — mais le buffer principal
  ne contient plus le secret en clair après `Zero()`.
- **`--remove-on-exit` (optionnel, désactivé par défaut).** À la sortie, si
  activé (`--remove-on-exit` ou `CLAUDE_DISTANT_REMOVE_ON_EXIT=true`), le
  client supprime aussi son propre binaire, en best-effort :
  - **Linux/macOS** : `os.Remove(cheminExe)` direct. Sous Unix, supprimer
    l'entrée de répertoire d'un fichier encore ouvert par le processus qui
    tourne fonctionne immédiatement (l'inode reste vivant jusqu'à la fin du
    process, mais le fichier disparaît de tout listing/`ls`).
  - **Windows** : un exécutable en cours d'exécution ne peut pas se
    supprimer lui-même (le fichier est verrouillé par l'OS). Le client
    écrit un petit script `.cmd` détaché dans un dossier temporaire, qui
    attend (poll `tasklist`) la fin du PID du client, supprime l'exe, puis
    se supprime lui-même (`del "%~f0"`) — voir
    `buildWindowsCleanupScript`/`removeBinaryWindows` dans
    `client/cleanup_binary.go`.
  - Le résultat (succès ou échec) est journalisé sur la console — jamais
    fatal : un échec de la suppression du binaire ne doit jamais empêcher un
    arrêt propre par ailleurs.

**Nuance importante : les fichiers écrits sur la machine cible via `write_file`
survivent au nettoyage du client.** Contrairement au workspace temporaire
(§ ci-dessus), supprimé intégralement à l'arrêt, un fichier installé par
`write_file` (`client/filetransfer.go:writeFileTransfer`) est écrit à
l'emplacement demandé par le harnais — potentiellement hors du workspace — et
n'est **pas** un résidu que le client nettoie : c'est précisément le but de
l'outil (déposer un fichier de façon durable sur la cible). Un projet qui
promet « sans résidu » doit le dire noir sur blanc : le modèle « sans résidu »
couvre l'empreinte du *client lui-même* (binaire, workspace, secrets en
mémoire), pas les effets délibérés des commandes/transferts que l'opérateur
lui fait exécuter. Comme les autres outils, les opérations `read_file`/
`write_file` respectent la politique de garde-fou locale (`--policy`, voir
`docs/PROTOCOL.md` §1) : en `confirm`, elles déclenchent toujours une
confirmation locale ; en `deny`, elles sont toujours refusées.

Ce qui reste **hors du contrôle du client**, par nature, et n'est donc pas
« nettoyé » — à documenter côté utilisateur final (§6) :
- L'historique shell (le lancement de la commande peut apparaître dans
  `~/.bash_history` / `PSReadLine`) si l'utilisateur tape la commande
  manuellement plutôt que de double-cliquer sur le binaire.
- Les journaux systèmes génériques de création de process
  (`journalctl`/Sysmon/Event Log Windows si configuré) — le client
  lui-même n'écrit rien là, mais l'OS peut avoir sa propre télémétrie de
  processus indépendamment de l'application.

## 2. Deux variantes de binaire : console et GUI

Depuis la même base de code, `client/Makefile` produit deux familles de
binaires bien distinctes :

| Variante | Build | Cible | Taille indicative (strippé) |
|---|---|---|---|
| **console** | `CGO_ENABLED=0`, statique, `make dist` | serveurs, y compris **sans affichage** | ~5,5 Mo |
| **GUI** | `-tags gui`, CGO + X11 (Linux)/mingw (Windows), `make dist-gui` | postes de bureau Windows / Ubuntu | ~24 Mo (Linux et Windows) |

Tailles mesurées sur le build `v0.3.0` : la variante GUI pèse environ **quatre
fois** la console, Fyne embarquant son propre moteur de rendu et ses polices.
C'est le prix de la fenêtre, et une raison de plus de ne pas l'imposer aux
déploiements serveur.

**Pourquoi deux binaires, et pas un seul avec la GUI en option activable au
runtime :** Fyne (le framework graphique utilisé, `client/gui.go`, tag de
build `gui`) lie **dynamiquement** la bibliothèque X11 sur Linux (et
équivalent GDI/Direct3D sur Windows) — un binaire compilé avec ce lien ne
démarre tout simplement pas sur une machine sans serveur d'affichage
(headless, la cible principale de ce projet : un serveur administré à
distance). Séparer les deux au build (tag `gui` + `CGO_ENABLED`) plutôt que de
détecter l'absence d'affichage au runtime évite ce piège : le livrable par
défaut (console, `make dist`) reste utilisable absolument partout, et la GUI
(`make dist-gui`) est un livrable additionnel réservé aux postes de bureau où
un opérateur humain est présent devant l'écran. C'est aussi pour cette raison
que la variante GUI n'est ni statique ni `CGO_ENABLED=0` : Fyne l'exige.

### Prérequis de build — variante GUI uniquement

La variante console (§3) ne requiert que Go. La variante GUI, elle, a besoin
d'une toolchain C et des bibliothèques de développement X11/OpenGL sur la
machine qui compile :

```sh
# Debian/Ubuntu — cible Linux (compilation native, CGO_ENABLED=1)
sudo apt install gcc libgl1-mesa-dev xorg-dev libxxf86vm-dev

# Debian/Ubuntu — cible Windows depuis Linux (cross-compilation mingw)
sudo apt install gcc-mingw-w64-x86-64
```

`libxxf86vm-dev` mérite d'être cité explicitement : c'est le seul paquet qui
a manqué lors de la mise en place de la variante GUI sur la machine de
développement de ce projet (les autres paquets X11/OpenGL usuels étaient déjà
présents) — un oubli facile puisque `xorg-dev` seul ne le tire pas
automatiquement sur toutes les distributions. `gcc-mingw-w64-x86-64` fournit
`x86_64-w64-mingw32-gcc`, le compilateur C utilisé par `make dist-gui` (via
`CC=x86_64-w64-mingw32-gcc`) pour produire l'exécutable Windows depuis Linux.

### Binaires non versionnés dans git

`client/client` (le binaire de build local, `make`/`make build`) a été retiré
du suivi git, et le `.gitignore` racine couvre désormais `client/client`,
`client/dist/` (sortie de `make dist`/`make dist-gui`) et `*.exe` : aucun des
binaires produits par ce document, console ou GUI, n'est destiné à être
commité. Seuls les checksums (`dist/SHA256SUMS`, §5) ont vocation à
accompagner une release dans un canal de distribution externe au dépôt.

## 3. Build — variante console (binaire unique portable)

Go 1.22+ (le dépôt est développé/testé avec Go 1.24). Aucune dépendance
CGO. Depuis `client/` :

```sh
make            # build natif rapide (dev, non strippé)
make check      # gofmt -l . && go vet ./... && go test ./...
make dist        # cross-compile : dist/claude-distant-client-{linux-amd64,linux-arm64,windows-amd64.exe}
make checksums   # dist/SHA256SUMS
make clean       # supprime dist/ et le binaire de dev
```

Équivalent manuel d'une cible `dist-*` (ex. Linux amd64) :

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
  -trimpath \
  -ldflags "-s -w -buildid= -X main.version=1.0.0+abc1234" \
  -o dist/claude-distant-client-linux-amd64 .
```

- `CGO_ENABLED=0` : binaire **statique**, aucune dépendance à `libc`/DLL
  système au runtime — copiable tel quel sur n'importe quelle machine de
  la même architecture/OS.
- `-ldflags "-s -w"` : retire le tableau de symboles et les infos de debug
  DWARF (binaire plus petit, rétro-ingénierie un peu plus pénible — pas une
  garantie d'obfuscation).
- `-ldflags "-X main.version=..."` : stampe la version/commit dans la
  variable `main.version` (`client/main.go`), affichée au démarrage et
  envoyée au relay dans le message `register`.
- `-trimpath` + `-buildid=` : retirent les chemins absolus de la machine de
  build et l'identifiant de build embarqué par le linker Go — à source et
  version de Go identiques, deux exécutions de `make dist` produisent des
  binaires identiques (build reproductible).

Plateformes cibles (Phase 6) : `linux/amd64`, `linux/arm64`,
`windows/amd64`. Sorties dans `client/dist/` (couvert par `.gitignore`
racine — non versionné, voir §2).

`RELAY_URL`/`CLIENT_TOKEN`/`IDENTITY_SECRET` (vides par défaut) personnalisent
ce même build pour produire un binaire à lancer sans argument ; voir
`client/Makefile` (commentaires en tête de fichier) et `docs/PROTOCOL.md`
pour la résolution flag > variable d'environnement > valeur compilée.

### Reproductibilité

```sh
make dist
sha256sum dist/claude-distant-client-linux-amd64 > /tmp/run1.sha256
rm -rf dist && make dist
sha256sum -c /tmp/run1.sha256   # doit rapporter "OK"
```

Tant que la même version de Go, le même module (`go.sum` inchangé) et les
mêmes `VERSION`/`COMMIT` sont utilisés, le binaire produit est identique
octet pour octet.

## 4. Build — variante GUI

Prérequis : §2 ci-dessus (paquets X11/OpenGL côté Linux,
`gcc-mingw-w64-x86-64` pour cross-compiler la cible Windows). Depuis
`client/` :

```sh
make dist-gui    # linux/amd64 (CGO natif) + windows/amd64 (mingw) -> dist/
```

Équivalent manuel, cible par cible :

```sh
# Linux amd64 — compilation native, CGO requis par Fyne
CGO_ENABLED=1 GOOS=linux GOARCH=amd64 go build -tags gui \
  -trimpath -ldflags "-s -w -buildid= -X main.version=1.0.0+abc1234" \
  -o dist/claude-distant-client-gui-linux-amd64 .

# Windows amd64 — cross-compilation depuis Linux via mingw
CGO_ENABLED=1 GOOS=windows GOARCH=amd64 CC=x86_64-w64-mingw32-gcc \
  go build -tags gui -trimpath \
  -ldflags "-s -w -buildid= -X main.version=1.0.0+abc1234 -H windowsgui" \
  -o dist/claude-distant-client-gui-windows-amd64.exe .
```

- `-tags gui` : bascule `client/gui.go` (implémentation Fyne) au lieu de
  `client/gui_stub.go` (point d'entrée console, compilé par défaut) — voir
  `docs/PROTOCOL.md`/le code pour le détail de la fenêtre.
- `CGO_ENABLED=1` : contrairement à la variante console, non négociable ici —
  Fyne s'appuie sur des bibliothèques graphiques natives (X11 côté Linux,
  GDI/Direct3D côté Windows) accessibles uniquement via CGO.
- `-H windowsgui` (**Windows uniquement**) : indique au linker Go de produire
  un exécutable de sous-système `GUI` plutôt que `console` — sans ce
  drapeau, une fenêtre de console noire s'ouvrirait derrière la fenêtre Fyne
  et resterait ouverte tant que le processus tourne, ce qui n'a pas de sens
  pour un livrable destiné à un utilisateur de bureau. Absent sur la cible
  Linux (spécifique au format d'exécutable PE de Windows).
- `-trimpath`/`-buildid=`/`-s -w`/`-X main.version=...` : mêmes drapeaux et
  même intention que pour la variante console (§3) — mais la reproductibilité
  binaire stricte n'est **pas** garantie ici : la toolchain C système (gcc
  natif ou `x86_64-w64-mingw32-gcc`) contribue elle aussi au binaire final, et
  sa version n'est pas figée par ce Makefile comme l'est celle de Go.

Sorties : `dist/claude-distant-client-gui-linux-amd64` et
`dist/claude-distant-client-gui-windows-amd64.exe` (préfixe `-gui-` qui les
distingue des binaires console dans le même dossier `dist/`, et qui reste
couvert par `checksums`, §5).

## 5. Signature / intégrité de la distribution

Cet environnement de développement ne dispose d'aucun certificat de
signature de code ni de clé GPG — la signature réelle n'est donc **pas**
automatisée ici. Procédure documentée pour un pipeline de release réel :

### 5.1 Checksums (minimum, toujours applicable)

```sh
cd client
make dist              # binaires console
make dist-gui           # + binaires GUI, si distribués (§4)
make checksums          # écrit dist/SHA256SUMS -- couvre les deux familles
cat dist/SHA256SUMS
```

L'utilisateur final vérifie après téléchargement :

```sh
# Linux
sha256sum -c SHA256SUMS --ignore-missing

# Windows (PowerShell)
Get-FileHash .\claude-distant-client-windows-amd64.exe -Algorithm SHA256
# comparer la sortie à la ligne correspondante de SHA256SUMS
```

### 5.2 Windows — Authenticode (`signtool`)

Sur une machine de release disposant d'un certificat de signature de code
(EV ou OV, émis par une autorité reconnue) :

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
  /f codesign-cert.pfx /p <mot-de-passe> `
  dist\claude-distant-client-windows-amd64.exe

signtool verify /pa dist\claude-distant-client-windows-amd64.exe
```

- `/fd SHA256` : algorithme de hachage du fichier.
- `/tr ... /td SHA256` : horodatage RFC 3161 (la signature reste valide
  après expiration du certificat).
- Un exécutable non signé déclenche des avertissements SmartScreen/Defender
  plus agressifs sur les postes Windows récents ; signer réduit ce
  frottement mais ne dispense pas des checksums (§5.1) pour la vérification
  d'intégrité indépendante du fournisseur du certificat. S'applique aux deux
  variantes (console et GUI, `*-windows-amd64.exe`/`*-gui-windows-amd64.exe`).

### 5.3 Linux — signature détachée GPG

```sh
# Une fois, côté mainteneur : générer/posséder une clé de signature dédiée
gpg --full-generate-key

# Pour chaque release
gpg --armor --detach-sign dist/claude-distant-client-linux-amd64
gpg --armor --detach-sign dist/claude-distant-client-linux-arm64

# Vérification côté utilisateur (après import de la clé publique du
# mainteneur, une seule fois : gpg --import maintainer-pubkey.asc)
gpg --verify claude-distant-client-linux-amd64.asc claude-distant-client-linux-amd64
```

### 5.4 Publication recommandée par release

Pour chaque binaire produit par `make dist`/`make dist-gui` :
1. `dist/claude-distant-client-<os>-<arch>[.exe]` (console) et/ou
   `dist/claude-distant-client-gui-<os>-<arch>[.exe]` (GUI) — le binaire
2. `dist/SHA256SUMS` (checksums de tous les binaires de la release, console
   et GUI confondus, §5.1)
3. `.asc` détaché GPG (Linux) — et binaire signé Authenticode (Windows,
   remplace directement le binaire non signé, `signtool` modifie le
   fichier en place)
4. `SHA256SUMS.asc` : signature détachée GPG du fichier `SHA256SUMS`
   lui-même, pour que la vérification de checksums ne repose pas sur un
   canal de téléchargement non authentifié.

## 6. Mode d'emploi utilisateur final

1. **Choisir puis télécharger** le binaire correspondant à sa machine et à
   son usage, depuis le canal de distribution fourni par l'opérateur, dans
   n'importe quel dossier (Bureau, Téléchargements, clé USB...) — aucune
   installation :
   - **console** (`claude-distant-client-windows-amd64.exe`,
     `-linux-amd64`, `-linux-arm64`) : le choix par défaut, fonctionne
     partout, y compris sur un serveur sans écran.
   - **GUI** (`claude-distant-client-gui-windows-amd64.exe`,
     `-gui-linux-amd64`) : pour un poste de bureau, quand un opérateur
     humain préfère une fenêtre (code de session en gros caractères,
     interrupteur mode automatique, journal d'activité en direct) à un
     terminal — voir `client/README.md`.
2. **Vérifier l'intégrité** (recommandé) : comparer le SHA-256 du fichier
   téléchargé à celui publié dans `SHA256SUMS` (§5.1), et/ou vérifier la
   signature Authenticode (clic droit → Propriétés → Signatures
   numériques, sous Windows) ou GPG (§5.3, sous Linux).
3. **Lancer** le binaire :
   - Windows : double-clic, ou depuis un terminal :
     `.\claude-distant-client-windows-amd64.exe --url wss://... --token ...`
   - Linux : `chmod +x` puis
     `./claude-distant-client-linux-amd64 --url wss://... --token ...`
   (`--url`/`--token` peuvent aussi venir de `CLAUDE_DISTANT_URL`/
   `CLAUDE_DISTANT_TOKEN`, fournis par l'opérateur, ou avoir été compilés
   dans le binaire via `RELAY_URL`/`CLIENT_TOKEN` (`client/Makefile`) —
   auquel cas un double-clic sans le moindre argument suffit. Voir
   `client/README.md`.) La variante GUI lance directement la fenêtre ; la
   variante console reste dans le terminal qui l'a lancée.
4. **Communiquer le code de session** à 9 chiffres affiché à l'écran
   (`784 123 678`) à l'opérateur (le harnais Claude), qui l'utilise côté
   relay pour cibler cette machine.
5. **Approuver/refuser** les commandes sensibles *si* la politique
   `--policy confirm` a été demandée : chaque commande classée destructive
   (et chaque `read_file`/`write_file`) affiche alors une invite locale avant
   exécution. Par défaut (`--policy auto`), aucune invite n'est affichée et
   les commandes du harnais s'exécutent directement — le client l'annonce au
   démarrage, et la variante GUI garde son avertissement permanent à l'écran.
6. **Fermer** le client (Ctrl-C dans le terminal, ou fermer la fenêtre) dès
   la session terminée : la connexion se ferme proprement, le dossier de
   travail temporaire est supprimé intégralement, et le jeton est effacé de
   la mémoire du processus. **Plus aucun résidu** sur la machine — sauf si
   `--remove-on-exit` était actif, auquel cas le binaire lui-même est aussi
   supprimé (best-effort ; sous Windows, la suppression effective peut
   prendre quelques secondes après la fermeture, le temps que le script de
   nettoyage détecte la fin du processus).

`--remove-on-exit` reste **optionnel et désactivé par défaut** : à activer
uniquement si l'utilisateur souhaite explicitement qu'aucune copie du
binaire ne survive sur la machine après usage (par exemple un poste
partagé/public). Dans le cas courant où la même machine sera réutilisée
pour de prochaines sessions, laisser l'option désactivée évite d'avoir à
re-télécharger le binaire à chaque fois.
