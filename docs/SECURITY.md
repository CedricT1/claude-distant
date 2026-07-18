# Sécurité — `claude-distant`

Ce document résume le modèle de menace et les mitigations mises en place,
en particulier celles de la **Phase 5 (durcissement sécurité)** :
authentification MCP scopée (Bearer JWT / OAuth 2.1), TLS strict, et les
mécanismes déjà en place depuis les phases précédentes (audit immuable,
politique de commandes, kill-switch, tokens client par-session).

Voir aussi [`docs/PLAN.md`](PLAN.md) (phases) et [`docs/PROTOCOL.md`](PROTOCOL.md)
(spécification des messages).

## 1. Modèle de menace (résumé)

`claude-distant` est, par construction, **un exécuteur de commandes à
distance privilégié** : le relay reçoit des instructions d'un harnais IA
(Claude) et les fait exécuter sur un PC tiers via un client qui s'y connecte
volontairement. C'est un usage **autorisé et supervisé uniquement**
(l'utilisateur du PC distant partage explicitement un code de session et
approuve les actions destructives localement) — pas un outil d'accès furtif.

Surfaces d'attaque principales et mitigations correspondantes :

| Menace | Mitigation |
|---|---|
| Interception réseau (MITM) sur le canal client↔relay ou harnais↔relay | TLS obligatoire en production (§2) ; `wss://` côté client, reverse proxy TLS devant `/mcp` |
| Vol/fuite du jeton Bearer du harnais | Jetons **scopés** à durée de vie courte (mode oauth, §3) plutôt qu'un jeton statique unique à privilèges illimités ; rotation facile (réémission), jamais de secret en dur (§4) |
| Réutilisation d'une connexion client compromise pour usurper une autre session | Jetons client `per_session` à usage unique, consommés au premier `register` réussi (`relay/auth.py:PerSessionTokenStore`) |
| Commande destructive exécutée sans consentement de l'utilisateur du PC distant | Garde-fou local configurable (`auto`/`confirm`/`deny`, côté client) + politique serveur (allow/denylist, quotas — `relay/command_policy.py`) |
| Session compromise ou comportement suspect détecté en cours d'usage | Kill-switch (`terminate_session`, outil MCP + `Broker.terminate_session`) : invalide immédiatement la session et ferme la connexion WS |
| Répudiation / contestation a posteriori d'une commande exécutée | Journal d'audit JSONL **chaîné par hash** (`relay/audit.py`), falsification détectable (`verify_chain`) |
| Un harnais compromis ou mal scopé outrepasse son rôle (ex. appelle `terminate_session` alors qu'il ne devrait que lire `system_info`) | Scopes MCP par outil en mode oauth (§3) — principe du moindre privilège par jeton émis |
| Attaque DNS rebinding contre l'endpoint MCP HTTP | Déléguée au reverse proxy TLS (`server_name`/`Host` strict, §2) plutôt qu'à l'allowlist `localhost`-only par défaut du SDK MCP, inadaptée à un déploiement proxifié — voir note dans `relay/mcp_server.py:create_mcp_server` |

Hors périmètre (assumé) : compromission du PC distant lui-même en dehors de
ce canal, ou compromission du poste opérateur du harnais — ce sont les
frontières de confiance du système, pas des failles qu'un durcissement du
relay peut combler.

## 2. TLS strict (terminaison externe)

Le relay (`uvicorn`) écoute en **HTTP interne** uniquement ; il ne termine
jamais le TLS lui-même. La stack Docker expose un reverse proxy **nginx HTTP-only**
(port 8080, réseau interne) qui proxies vers `relay:8000`.

La **terminaison TLS est assurée par un reverse proxy externe**, de la
responsabilité du déployeur (Caddy, Nginx, Traefik, etc.). Le déployeur
place son propre reverse proxy HTTPS devant la stack et forward vers le
port nginx HTTP (8080).

### Configuration du proxy TLS externe

Le reverse proxy externe DOIT :

1. **Terminer le TLS 1.2+** (HTTPS, port 443)
2. **Forward le trafic** vers `nginx:8080` (ou `localhost:8080` s'il est sur le même hôte)
3. **Positionner les en-têtes** : `Host`, `X-Real-IP`, `X-Forwarded-For`, 
   `X-Forwarded-Proto: https`, `X-Forwarded-Host`
4. **Préserver l'upgrade WebSocket** (`Upgrade` / `Connection` headers)
5. **Désactiver le buffering de réponse** (streaming SSE du transport MCP Streamable HTTP)
6. Optionnellement, ajouter les en-têtes de sécurité stricts :
   - `Strict-Transport-Security: max-age=31536000; includeSubDomains`
   - `X-Content-Type-Options: nosniff`
   - `X-Frame-Options: DENY`
   - `Referrer-Policy: no-referrer`

**Important** : l'en-tête `X-Forwarded-Proto: https` doit TOUJOURS être
positionné par le proxy externe, pour que le relay génère des URLs en `https://`
pour les métadonnées OAuth RFC 9728 (mode `MCP_AUTH_MODE=oauth`). Voir la section 3 ci-dessous.

### Exemple : Caddy externe

```caddy
relay.example.com {
    tls {
        protocols tls1.2 tls1.3
    }

    encode gzip zstd

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "no-referrer"
        -Server
    }

    reverse_proxy localhost:8080 {
        flush_interval -1
        header_up X-Forwarded-Proto https
    }
}
```

(Remplacer `localhost:8080` par l'adresse de la machine hôte si le Caddy
est sur une machine différente.)

### Exemple : Nginx externe

```nginx
server {
    listen 443 ssl http2;
    server_name relay.example.com;

    ssl_certificate /etc/nginx/tls/cert.pem;
    ssl_certificate_key /etc/nginx/tls/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;

    location / {
        proxy_pass http://localhost:8080;  # ou l'adresse de la machine hôte
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

Le client Go distant se connecte toujours en `wss://` (jamais `ws://` en
production) — c'est l'invariant documenté dans `docs/PROTOCOL.md`/`README.md`.

## 3. Authentification MCP (harnais ↔ relay)

Deux modes, sélectionnés via `MCP_AUTH_MODE` :

### `static_bearer` (défaut, compat MVP)

Un unique jeton pré-partagé (`MCP_BEARER_TOKEN`) donne accès à **tous** les
outils MCP. Simple, mais pas de granularité : quiconque détient le jeton a
tous les droits, et sa seule rotation possible est de le changer et
redéployer. Adapté à un déploiement mono-opérateur, à faible enjeu, ou en
développement.

### `oauth` (Resource Server OAuth 2.1)

Le relay valide des **jetons Bearer JWT signés HS256** (secret
`MCP_JWT_SECRET`) portant `sub`, `exp` et des **scopes** :

| Scope | Outil(s) protégé(s) |
|---|---|
| `session:connect` | `connect_session` |
| `command:execute` | `run_command`, `run_shell` |
| `session:terminate` | `terminate_session` |
| `client:provision` | `issue_client_token` |

Un jeton sans le scope requis reçoit une erreur d'outil claire
(`{"status": "error", "error": "forbidden_scope", ...}`) et l'événement est
journalisé dans l'audit (`decision: "denied"`, `outcome.reason` =
`missing_scope:<scope>`). Un jeton absent, invalide ou expiré est rejeté au
niveau transport (`401 Unauthorized`, avant même d'atteindre un outil).

**Émission de jetons** : `python -m relay.tokens issue --sub <nom> --scopes
<scope1>,<scope2>,... --ttl <secondes>` (voir `relay/tokens.py`). Émettre des
jetons **à portée minimale et TTL court** pour chaque usage (ex. un jeton
`session:connect,command:execute` de courte durée pour une session de
dépannage donnée, plutôt qu'un jeton `*` longue durée).

**Compromis assumé** (documenté en détail dans `relay/mcp_server.py`) : le
SDK MCP officiel (`mcp.server.auth`) n'exprime des scopes requis qu'au niveau
global de l'endpoint, pas par outil. Le relay câble donc le SDK
(`TokenVerifier`/`AuthSettings`, `BearerAuthBackend`, `AuthContextMiddleware`,
`RequireAuthMiddleware`) pour la validation de signature/expiration/format,
et n'ajoute qu'une vérification de scope par outil (au-dessus du contexte
d'authentification déjà posé par le SDK) — pas de middleware d'authentification
maison réinventant ce que le SDK fait déjà bien. Ce n'est pas une fédération
multi-émetteurs (pas de JWKS, pas de serveur d'autorisation externe) : le
relay est à la fois émetteur et vérifieur de ses propres jetons, ce qui est
raisonnable pour un déploiement à opérateur unique.

## 4. Gestion des secrets et des tokens

- Jamais de secret en dur dans le code, les images Docker ou les fichiers
  versionnés : `docker/.env.example` ne contient que des valeurs d'exemple
  (`change-me`) ou des variables commentées.
- `CLIENT_TOKEN`, `MCP_BEARER_TOKEN`, `MCP_JWT_SECRET` : à générer avec un
  aléa fort (ex. `python -c "import secrets; print(secrets.token_urlsafe(48))"`)
  et à stocker uniquement dans `docker/.env` (non versionné) ou un gestionnaire
  de secrets externe.
- **Tokens client par-session** (`CLIENT_AUTH_MODE=per_session`) : jetons
  courts, à usage unique, émis à la demande via l'outil MCP
  `issue_client_token` (protégé par le scope `client:provision` en mode
  oauth) plutôt que par appel direct à `PerSessionTokenStore.issue(...)` côté
  déploiement — remplace le mécanisme manuel des vagues précédentes.
- Un jeton attendu vide/non défini refuse **tout le monde** par construction
  (`relay/auth.py:verify_token`, `relay/jwt_auth.py:issue_token`) : le relay
  est sûr par défaut plutôt que de s'ouvrir sans authentification en cas de
  mauvaise configuration.

## 5. Audit

Journal JSONL append-only, **chaîné par hash SHA-256** (`relay/audit.py`) :
chaque entrée référence le hash de la précédente, toute modification,
suppression ou réordonnancement casse la chaîne et est détectable via
`relay.audit.verify_chain(path)`. Entrées : `timestamp`, `session_code`,
`tool`, `params_summary` (tronqué), `decision` (`allowed`/`denied`/`killed`),
`outcome`. Couvre : dispatch de commande (autorisé/refusé par
`CommandPolicy`), kill-switch (`terminate_session`), et refus de scope MCP
(mode oauth). Chemin configurable via `AUDIT_LOG_PATH` (monté en volume
Docker pour survivre aux redémarrages, voir `docker-compose.yml`).

## 6. Kill-switch

L'outil MCP `terminate_session(session_code)` (protégé par le scope
`session:terminate` en mode oauth) invalide immédiatement une session :
retrait du store, échec propre de toute commande en cours
(`ClientDisconnectedError`), fermeture de la connexion WebSocket cliente.
Utilisable à tout moment par l'opérateur/harnais pour couper court à un
comportement suspect, sans attendre l'expiration du TTL de session.

## 7. Politique de commandes

Allow/denylist par expression régulière sur `run_command`/`run_shell`, quotas
par session (nombre total, débit par minute) — voir
`relay/command_policy.py`. La denylist est toujours prioritaire sur
l'allowlist. Configuration via `COMMAND_DENYLIST`/`COMMAND_ALLOWLIST`/
`MAX_COMMANDS_PER_SESSION`/`RATE_LIMIT_PER_MINUTE`.
