# claude-distant — build système du client portable (Phase 6, docs/PLAN.md).
#
# Produit des binaires statiques strippés (CGO_ENABLED=0, -s -w), prêts à
# être copiés tels quels dans un dossier temporaire sur la machine cible et
# lancés directement — aucune installation, aucun runtime requis.
#
# Cibles principales :
#
#   make            build natif (dev/itération rapide, non strippé)
#   make test       go test ./...
#   make vet        go vet ./...
#   make fmt        gofmt -l . (liste les fichiers mal formatés)
#   make check      fmt + vet + test (vérification complète avant release)
#   make dist       cross-compile linux/amd64, linux/arm64, windows/amd64
#                   dans dist/
#   make checksums  SHA-256 de chaque binaire produit -> dist/SHA256SUMS
#                   (voir docs/PACKAGING.md pour la signature Authenticode /
#                   GPG détachée)
#   make clean      supprime dist/ et le binaire de dev
#
# Build reproductible : VERSION/COMMIT sont dérivés de git (ou "dev"/
# "unknown" hors dépôt git), -trimpath retire les chemins absolus de la
# machine de build, et -buildid= retire l'identifiant de build embarqué par
# le linker Go — à source et à version de Go identiques, deux exécutions de
# `make dist` produisent des binaires identiques.

BINARY  := claude-distant-client
DIST    := dist

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
COMMIT  ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)

# -s -w        : strip du tableau de symboles et des infos de debug DWARF
# -buildid=    : build id vide -> reproductibilité binaire à source égale
# -X main.version=... : stampe VERSION+COMMIT dans la variable main.version
LDFLAGS := -s -w -buildid= -X main.version=$(VERSION)+$(COMMIT)

.PHONY: all build test vet fmt check dist checksums clean \
        dist-linux-amd64 dist-linux-arm64 dist-windows-amd64

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

# SHA-256 de chaque binaire produit. La procédure complète de signature
# (Authenticode signtool.exe côté Windows, signature détachée GPG côté
# Linux) est documentée dans docs/PACKAGING.md — elle ne peut pas être
# automatisée ici faute de certificat/clé disponibles dans cet
# environnement de build.
checksums:
	cd $(DIST) && sha256sum $(BINARY)-* > SHA256SUMS

clean:
	rm -rf $(DIST) $(BINARY)
