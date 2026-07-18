# relay

Broker de session WebSocket + serveur MCP du projet `claude-distant`. Voir
`docs/PROTOCOL.md` et `docs/PLAN.md` à la racine du dépôt pour le contexte
protocolaire complet.

## Lancer en local

```bash
cd relay
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export CLIENT_TOKEN="un-secret-pour-le-client-distant"
export MCP_BEARER_TOKEN="un-secret-pour-le-harnais-mcp"
export SESSION_TTL_SECONDS=1800   # optionnel, défaut 1800 (30 min)
export HOST=0.0.0.0               # optionnel
export PORT=8000                  # optionnel

uvicorn relay.app:app --host "$HOST" --port "$PORT"
# ou directement : python -m relay.app
```

Endpoints exposés :
- `wss://<host>/ws/client` : connexion sortante du client PC distant (auth
  `Authorization: Bearer <CLIENT_TOKEN>`).
- `https://<host>/mcp` : serveur MCP Streamable HTTP pour le harnais (auth
  `Authorization: Bearer <MCP_BEARER_TOKEN>`).
- `GET /healthz` : sonde de santé (sans auth), retourne `{"status": "ok"}`.

Si `CLIENT_TOKEN` ou `MCP_BEARER_TOKEN` ne sont pas définis, l'app démarre
quand même mais refuse toutes les connexions sur les canaux concernés (un
jeton attendu vide ne matche jamais, voir `relay/auth.py`) : sûr par défaut,
pas d'ouverture accidentelle sans authentification.

## Variables d'environnement

| Variable | Défaut | Rôle |
|---|---|---|
| `CLIENT_TOKEN` | *(vide → tout refusé)* | jeton Bearer attendu du client PC distant sur `/ws/client` |
| `MCP_BEARER_TOKEN` | *(vide → tout refusé)* | jeton Bearer attendu du harnais sur `/mcp` |
| `SESSION_TTL_SECONDS` | `1800` | TTL (secondes) d'un code de session, prolongé par `heartbeat` |
| `HOST` | `0.0.0.0` | interface d'écoute uvicorn |
| `PORT` | `8000` | port d'écoute uvicorn |

## Structure

- `broker.py` — connexions client WS, génération/anti-collision des codes à
  9 chiffres, routage `code → connexion`, agrégation `stream`/`result`
  corrélée par `request_id` (`dispatch_command`, utilisé par la couche MCP).
- `session_store.py` — `SessionStore` (interface abstraite) + implémentation
  en mémoire (`InMemorySessionStore`, dict + `asyncio.Lock` + TTL). Prête à
  être remplacée par un store Redis (cf. `docs/PLAN.md` §2) sans changer les
  appelants.
- `auth.py` — extraction/validation des jetons Bearer (client et MCP).
- `mcp_server.py` — les 4 outils MCP (`connect_session`, `system_info`,
  `run_command`, `run_shell`) construits avec le SDK MCP officiel
  (`mcp.server.fastmcp.FastMCP`), transport Streamable HTTP. Voir les TODO en
  tête de fichier concernant la migration OAuth 2.1 (phase 5 du plan).
- `app.py` — application FastAPI : monte `/ws/client`, `/mcp`, `/healthz`,
  point d'entrée `uvicorn relay.app:app`.

## Tests

```bash
cd /home/user/claude-distant
python -m pytest tests/relay -q
```

Développés en TDD (test rouge avant l'implémentation), couvrant : format et
unicité des codes de session, TTL du `SessionStore`, corrélation
`request_id` et gestion d'erreurs (session inconnue/expirée, client
déconnecté en cours de commande, timeout) dans `broker.py`, les 4 outils MCP
via le vrai SDK, et un test d'intégration bout-en-bout sur un vrai socket
WebSocket (`register` → `registered` avec code 9 chiffres → `command` →
`stream`/`result` → agrégation).
