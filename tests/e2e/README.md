# Tests e2e bout-en-bout (`tests/e2e/`)

Ces tests relient les **trois composants réels** de `claude-distant`, sans
aucun mock :

1. le **relay** (`relay.app.create_app`), lancé avec un vrai `uvicorn.Server`
   sur un port TCP éphémère réel (même technique que
   `tests/relay/test_integration.py`) ;
2. le **vrai binaire client Go compilé** (`client/`), lancé en sous-processus
   réel : il se connecte en sortant au relay, exécute réellement les
   commandes reçues (vrai `exec.Cmd`) et affiche le vrai code de session à
   9 chiffres sur sa sortie standard ;
3. un **vrai client MCP** (SDK officiel `mcp`, transport Streamable HTTP),
   qui joue le rôle du harnais et appelle les 6 outils MCP décrits dans
   `docs/PROTOCOL.md`.

Contexte : `claude-distant` est un outil de télémaintenance **consenti et
documenté** (cf. `docs/SECURITY.md`) — lancer le relay et le client
localement pour ces tests est un usage légitime et attendu.

## Lancer les tests

Depuis la racine du dépôt, avec un environnement Python contenant
`fastapi`, `uvicorn`, `mcp`, `pyjwt`, `pytest`, `pytest-asyncio`, `httpx`,
`websockets`, et un compilateur Go (1.22+) disponible dans `PATH` :

```bash
python -m pytest tests/e2e -q
```

(`python -m pytest`, pas juste `pytest`, pour que le paquet `relay/` du
dépôt soit importable — voir la remarque plus bas.)

Pour ne lancer que les tests marqués `e2e` (utile si ce dossier est un jour
mélangé avec des tests plus rapides) :

```bash
python -m pytest tests/e2e -q -m e2e
```

Le premier lancement compile le binaire client Go (`go build`) dans un
répertoire temporaire propre à la session pytest (fixture `client_binary`,
scope `session` — un seul build, réutilisé par tous les tests du module) ;
les lancements suivants réutilisent `client/dist/claude-distant-client-linux-amd64`
s'il existe déjà (produit par `make -C client dist`) et que l'hôte
d'exécution est bien linux/amd64.

## Ce qui est couvert

- `test_e2e_static_bearer.py` : mode `MCP_AUTH_MODE=static_bearer` (défaut).
  Handshake complet `connect_session` → `run_shell` → `run_command` →
  `terminate_session` (kill-switch), plus un cas négatif (mauvais jeton
  Bearer rejeté en 401 au niveau transport).
- `test_e2e_oauth.py` : mode `MCP_AUTH_MODE=oauth`. Émission de JWT scopés
  via `relay.jwt_auth.issue_token` (équivalent programmatique de
  `python -m relay.tokens issue`), même handshake complet avec un jeton
  portant tous les scopes nécessaires, **et** un cas négatif : un jeton ne
  portant que `session:connect` peut se connecter mais se voit refuser
  `run_shell` (`forbidden_scope`) ; un jeton absent est rejeté en 401 avant
  tout appel d'outil.

Chaque test attend activement (poll borné par un timeout, jamais de
`sleep` fixe) : `/healthz` du relay, puis l'apparition du code de session à
9 chiffres dans la sortie du vrai process client.

## Notes

- Pourquoi `python -m pytest` : `relay/` est un paquet du dépôt sans
  installation (`pip install -e .`) — `python -m pytest` insère le
  répertoire courant dans `sys.path`, ce qui rend `import relay...`
  possible depuis n'importe quel sous-dossier de tests, exactement comme
  pour `tests/relay/`.
- Ces tests sont plus lents que la suite `tests/relay/` (compilation Go au
  premier lancement, vrai sous-processus, vrai aller-retour réseau
  local) : comptez quelques secondes par test, pas des millisecondes.
- Le nettoyage (arrêt du client, arrêt du serveur uvicorn, libération du
  port éphémère) est garanti par les context managers asynchrones
  `RunningClient`/`RunningRelay` de `tests/e2e/harness.py`, y compris en cas
  d'échec d'assertion en cours de test.
