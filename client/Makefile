# claude-distant — build système du client portable (Phase 6, docs/PLAN.md ;
# personnalisation à la compilation et variante GUI, docs/PACKAGING.md).
#
# Produit deux familles de binaires depuis la même base de code :
#   - console : statiques strippés (CGO_ENABLED=0, -s -w), aucune dépendance
#     runtime — pour serveurs, y compris sans affichage (`make dist`).
#   - GUI (tag de build `gui`) : CGO + Fyne (X11 sur Linux, mingw sur
#     Windows), fenêtre graphique — pour postes de bureau Windows/Ubuntu
#     uniquement, ne démarrent pas sur une machine sans affichage
#     (`make dist-gui`). Voir docs/PACKAGING.md pour le pourquoi du
#     dédoublement et les paquets système requis pour compiler cette
#     variante.
#
# Cibles principales :
#
#   make            build natif (dev/itération rapide, non strippé, console)
#   make test       go test ./...
#   make vet        go vet ./...
#   make fmt        gofmt -l . (liste les fichiers mal formatés)
#   make check      fmt + vet + test (vérification complète avant release)
#   make dist       cross-compile console : linux/amd64, linux/arm64,
#                   windows/amd64, dans dist/
#   make dist-gui   cross-compile GUI : linux/amd64 (CGO natif) et
#                   windows/amd64 (mingw), dans dist/
#   make checksums  SHA-256 de chaque binaire produit (console + GUI)
#                   -> dist/SHA256SUMS (voir docs/PACKAGING.md pour la
#                   signature Authenticode / GPG détachée)
#   make clean      supprime dist/ et le binaire de dev
#
# Build reproductible (variante console) : VERSION/COMMIT sont dérivés de
# git (ou "dev"/"unknown" hors dépôt git), -trimpath retire les chemins
# absolus de la machine de build, et -buildid= retire l'identifiant de
# build embarqué par le linker Go — à source et à version de Go
# identiques, deux exécutions de `make dist` produisent des binaires
# identiques. La variante GUI (CGO + toolchain C externe, mingw pour la
# cible Windows) reçoit les mêmes drapeaux mais n'a pas la même garantie :
# le compilateur C système contribue lui aussi au binaire final, et son
# empreinte (chemins, version) n'est pas maîtrisée par ce Makefile.

BINARY  := claude-distant-client
DIST    := dist

# Le `v` initial des tags sémantiques (`v0.3.0`) est retiré : `main.go` affiche
# déjà « client v%s » et la variable `version` est envoyée telle quelle au relay
# dans `register`. Sans ce nettoyage, un build tagué s'annonce « vv0.3.0 » et le
# relay journalise une version incohérente avec le défaut codé en dur ("0.3.0").
VERSION ?= $(patsubst v%,%,$(shell git describe --tags --always --dirty 2>/dev/null || echo dev))
COMMIT  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

# --- Personnalisation à la compilation (binaire à un seul argument zéro) ---
#
# RELAY_URL / CLIENT_TOKEN / IDENTITY_SECRET sont injectées au link dans
# main.buildRelayURL / main.buildClientToken / main.buildIdentitySecret
# (client/buildconfig.go), que client/main.go:parseConfig ne consulte qu'en
# tout dernier recours (flag > variable d'environnement > cette valeur
# compilée, voir docs/PROTOCOL.md et client/main_test.go). Vides par
# défaut (`?=` ne fait rien si déjà vide) : un `make dist`/`make dist-gui`
# sans ces variables produit donc un binaire générique, en tout point
# identique au comportement actuel — --url/--token restent requis.
#
# ATTENTION, à lire avant de mettre un vrai jeton ici : une valeur de
# variable make apparaît en clair dans l'historique interactif du shell si
# elle est tapée sur la ligne de commande (`make dist CLIENT_TOKEN=...`).
# `?=` a une propriété utile pour l'éviter : GNU Make importe automatiquement
# les variables d'environnement au démarrage, et `?=` ne les écrase pas —
# donc `set -a && . ./secrets.env && set +a && make dist` (fichier local,
# jamais versionné, chargé par le shell plutôt que tapé) tient CLIENT_TOKEN
# hors de l'historique du shell et de la ligne de commande `make` elle-même.
# Ceci NE règle en revanche pas l'exposition inhérente au mécanisme
# `-ldflags -X` : quelle que soit la façon dont la valeur arrive jusqu'ici,
# le `go build` sous-jacent la reçoit comme argument de sa propre ligne de
# commande, donc visible via `ps aux`/`/proc/<pid>/cmdline` par tout autre
# utilisateur de la machine de build pendant la durée du build — et le
# binaire produit contient la valeur en clair, récupérable par un simple
# `strings` (docs/SECURITY.md « le binaire distribué EST un secret »).
RELAY_URL       ?=
CLIENT_TOKEN    ?=
IDENTITY_SECRET ?=

# -s -w        : strip du tableau de symboles et des infos de debug DWARF
# -buildid=    : build id vide -> reproductibilité binaire à source égale
# -X main.version=...              : stampe VERSION+COMMIT dans main.version
# -X main.buildRelayURL=...        : voir "Personnalisation" ci-dessus
# -X main.buildClientToken=...     : idem
# -X main.buildIdentitySecret=...  : idem
LDFLAGS := -s -w -buildid= -X main.version=$(VERSION)+$(COMMIT) \
           -X main.buildRelayURL=$(RELAY_URL) \
           -X main.buildClientToken=$(CLIENT_TOKEN) \
           -X main.buildIdentitySecret=$(IDENTITY_SECRET)

# -H windowsgui : variante Windows uniquement (ci-dessous), empêche
# l'ouverture d'une console noire derrière la fenêtre Fyne.
LDFLAGS_WINDOWS_GUI := $(LDFLAGS) -H windowsgui

.PHONY: all build test vet fmt check dist dist-gui checksums clean \
        dist-linux-amd64 dist-linux-arm64 dist-windows-amd64 \
        dist-gui-linux-amd64 dist-gui-windows-amd64

all: build

build:
	go build -o $(BINARY) .

test:
	go test ./...

vet:
	go vet ./...

fmt:
	gofmt -l .

check: fmt vet test

# --- Variante console : CGO_ENABLED=0, statique, aucune dépendance runtime ---

dist: dist-linux-amd64 dist-linux-arm64 dist-windows-amd64

dist-linux-amd64:
	mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-linux-amd64 .

dist-linux-arm64:
	mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-linux-arm64 .

dist-windows-amd64:
	mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-windows-amd64.exe .

# --- Variante GUI (tag `gui`) : CGO + Fyne, postes de bureau uniquement ---
#
# Fyne lie X11 dynamiquement sur Linux : ce binaire ne démarre pas sur un
# serveur sans affichage, d'où la variante console ci-dessus comme livrable
# par défaut. Prérequis système détaillés dans docs/PACKAGING.md (paquets
# de dev X11/OpenGL côté Linux, gcc-mingw-w64-x86-64 pour la cible Windows).
#
# Pas de linux/arm64 ni de macOS ici : seules les deux cibles listées dans
# la SPEC (linux/amd64, windows/amd64) sont couvertes — les postes de
# bureau visés par la GUI.

dist-gui: dist-gui-linux-amd64 dist-gui-windows-amd64

dist-gui-linux-amd64:
	mkdir -p $(DIST)
	CGO_ENABLED=1 GOOS=linux GOARCH=amd64 go build -tags gui -trimpath -ldflags "$(LDFLAGS)" -o $(DIST)/$(BINARY)-gui-linux-amd64 .

dist-gui-windows-amd64:
	mkdir -p $(DIST)
	CGO_ENABLED=1 GOOS=windows GOARCH=amd64 CC=x86_64-w64-mingw32-gcc go build -tags gui -trimpath -ldflags "$(LDFLAGS_WINDOWS_GUI)" -o $(DIST)/$(BINARY)-gui-windows-amd64.exe .

# SHA-256 de chaque binaire produit, console ET GUI confondus (les deux
# familles partagent le préfixe $(BINARY)-, ce même glob les couvre déjà :
# claude-distant-client-linux-amd64, ...-gui-linux-amd64, etc.). La
# procédure complète de signature (Authenticode signtool.exe côté Windows,
# signature détachée GPG côté Linux) est documentée dans docs/PACKAGING.md
# — elle ne peut pas être automatisée ici faute de certificat/clé
# disponibles dans cet environnement de build.
checksums:
	cd $(DIST) && sha256sum $(BINARY)-* > SHA256SUMS

clean:
	rm -rf $(DIST) $(BINARY)
