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

Ce qui reste **hors du contrôle du client**, par nature, et n'est donc pas
« nettoyé » — à documenter côté utilisateur final (§4) :
- L'historique shell (le lancement de la commande peut apparaître dans
  `~/.bash_history` / `PSReadLine`) si l'utilisateur tape la commande
  manuellement plutôt que de double-cliquer sur le binaire.
- Les journaux systèmes génériques de création de process
  (`journalctl`/Sysmon/Event Log Windows si configuré) — le client
  lui-même n'écrit rien là, mais l'OS peut avoir sa propre télémétrie de
  processus indépendamment de l'application.

## 2. Build — binaire unique portable

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
`windows/amd64`. Sorties dans `client/dist/` (déjà couvert par l'entrée
générique `dist/` du `.gitignore` racine — non versionné).

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

## 3. Signature / intégrité de la distribution

Cet environnement de développement ne dispose d'aucun certificat de
signature de code ni de clé GPG — la signature réelle n'est donc **pas**
automatisée ici. Procédure documentée pour un pipeline de release réel :

### 3.1 Checksums (minimum, toujours applicable)

```sh
cd client
make dist
make checksums          # écrit dist/SHA256SUMS
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

### 3.2 Windows — Authenticode (`signtool`)

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
  frottement mais ne dispense pas des checksums (§3.1) pour la vérification
  d'intégrité indépendante du fournisseur du certificat.

### 3.3 Linux — signature détachée GPG

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

### 3.4 Publication recommandée par release

Pour chaque binaire produit par `make dist` :
1. `dist/claude-distant-client-<os>-<arch>[.exe]` (le binaire)
2. `dist/SHA256SUMS` (checksums de tous les binaires de la release)
3. `.asc` détaché GPG (Linux) — et binaire signé Authenticode (Windows,
   remplace directement le binaire non signé, `signtool` modifie le
   fichier en place)
4. `SHA256SUMS.asc` : signature détachée GPG du fichier `SHA256SUMS`
   lui-même, pour que la vérification de checksums ne repose pas sur un
   canal de téléchargement non authentifié.

## 4. Mode d'emploi utilisateur final

1. **Télécharger** le binaire correspondant à sa machine
   (`claude-distant-client-windows-amd64.exe` ou
   `claude-distant-client-linux-amd64`/`-linux-arm64`) depuis le canal de
   distribution fourni par l'opérateur, dans n'importe quel dossier
   (Bureau, Téléchargements, clé USB...) — aucune installation.
2. **Vérifier l'intégrité** (recommandé) : comparer le SHA-256 du fichier
   téléchargé à celui publié dans `SHA256SUMS` (§3.1), et/ou vérifier la
   signature Authenticode (clic droit → Propriétés → Signatures
   numériques, sous Windows) ou GPG (§3.3, sous Linux).
3. **Lancer** le binaire :
   - Windows : double-clic, ou depuis un terminal :
     `.\claude-distant-client-windows-amd64.exe --url wss://... --token ...`
   - Linux : `chmod +x` puis
     `./claude-distant-client-linux-amd64 --url wss://... --token ...`
   (`--url`/`--token` peuvent aussi venir de `CLAUDE_DISTANT_URL`/
   `CLAUDE_DISTANT_TOKEN`, fournis par l'opérateur — voir
   `client/README.md`.)
4. **Communiquer le code de session** à 9 chiffres affiché à l'écran
   (`784 123 678`) à l'opérateur (le harnais Claude), qui l'utilise côté
   relay pour cibler cette machine.
5. **Approuver/refuser** les commandes sensibles si la politique
   `--policy confirm` (par défaut) est active — chaque commande classée
   destructive affiche une invite locale avant exécution.
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
