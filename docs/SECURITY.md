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
volontairement. C'est un usage **autorisé et supervisé uniquement** : l'utilisateur du PC
distant lance lui-même le client et partage explicitement un code de session
— c'est ce partage, et non l'approbation commande par commande (désactivée
par défaut depuis le passage de `--policy` à `auto`, §1ter (d)), qui porte le
consentement. Pas un outil d'accès furtif.

Surfaces d'attaque principales et mitigations correspondantes :

| Menace | Mitigation |
|---|---|
| Interception réseau (MITM) sur le canal client↔relay ou harnais↔relay | TLS obligatoire en production (§2) ; `wss://` côté client, reverse proxy TLS devant `/mcp` |
| Vol/fuite du jeton Bearer du harnais | Jetons **scopés** à durée de vie courte (mode oauth, §3) plutôt qu'un jeton statique unique à privilèges illimités ; rotation facile (réémission), jamais de secret en dur (§4) |
| Réutilisation d'une connexion client compromise pour usurper une autre session | Jetons client `per_session` à usage unique, consommés au premier `register` réussi (`relay/auth.py:PerSessionTokenStore`) |
| Commande destructive exécutée sans consentement de l'utilisateur du PC distant | Garde-fou local configurable (`auto`/`confirm`/`deny`, côté client — **`auto` par défaut**, voir §1ter (d)) + politique serveur (allow/denylist, quotas — `relay/command_policy.py`), cette dernière restant active quelle que soit la politique locale |
| Session compromise ou comportement suspect détecté en cours d'usage | Kill-switch (`terminate_session`, outil MCP + `Broker.terminate_session`) : invalide immédiatement la session et ferme la connexion WS |
| Répudiation / contestation a posteriori d'une commande exécutée | Journal d'audit JSONL **chaîné par hash** (`relay/audit.py`), falsification détectable (`verify_chain`) |
| Un harnais compromis ou mal scopé outrepasse son rôle (ex. appelle `terminate_session` alors qu'il ne devrait que lire `system_info`) | Scopes MCP par outil en mode oauth (§3) — principe du moindre privilège par jeton émis |
| Attaque DNS rebinding contre l'endpoint MCP HTTP | Déléguée au reverse proxy TLS (`server_name`/`Host` strict, §2) plutôt qu'à l'allowlist `localhost`-only par défaut du SDK MCP, inadaptée à un déploiement proxifié — voir note dans `relay/mcp_server.py:create_mcp_server` |
| Exfiltration de données via `read_file` | Scope `file:read` dédié en mode oauth ; garde-fou local confirm/deny (la lecture d'un fichier déclenche **aussi** la confirmation, pas seulement l'écriture — lire un fichier l'exfiltre de la machine) ; journal d'audit de chaque lecture ; allow/denylist du relay appliquée au chemin |
| Persistance / altération via `write_file` | Scope `file:write` dédié en mode oauth ; garde-fou local confirm/deny ; audit avec contenu rédigé (pas de fuite de données sensibles dans le journal) ; écriture atomique (pas de fichier cible corrompu par un transfert interrompu) |

## 1bis. Surface d'attaque élargie par le transfert de fichiers

`read_file`/`write_file` élargissent nettement le modèle de menace par rapport
à la seule exécution de commandes : un opérateur (ou un harnais compromis
avec les bons scopes) peut désormais lire ou écrire **n'importe quel** fichier
accessible au processus client, sans passer par un `run_command` visible et
journalisable comme tel. Limites à connaître, honnêtement :

- **Pas de confinement de chemin.** C'est délibéré : `claude-distant` est un
  outil d'administration système, pas un partage de fichiers scopé à un
  répertoire. La protection ne vient donc pas d'un sandbox de chemin, mais de
  la combinaison scopes MCP + politique du relay (allow/denylist) + garde-fou
  local — voir `client/filetransfer.go:transferPath` (aucune validation au-delà
  d'un chemin non vide) et `docs/PROTOCOL.md` §4.
- **Écriture sur un lien symbolique : le lien est remplacé par un fichier
  régulier**, pas suivi. Conséquence directe de l'installation atomique
  (fichier temporaire dans le même répertoire puis `rename`, voir
  `client/filetransfer.go:writeFileTransfer`) : `rename` remplace l'entrée de
  répertoire elle-même, il ne déréférence pas la cible du lien. C'est plus sûr
  que de suivre aveuglément le lien (pas d'écriture surprise ailleurs sur le
  disque via un lien piégé), mais surprenant pour un administrateur qui
  s'attendrait à ce que son `write_file` sur un chemin symlinké mette à jour
  le fichier pointé : le lien est cassé et remplacé.
- **Répertoire parent symbolique : suivi normalement.** Seul le composant
  final du chemin bénéficie du comportement ci-dessus ; un lien sur un
  répertoire intermédiaire est résolu comme d'habitude par l'OS.
- **Lecture : les liens symboliques sont suivis**, `read_file` renvoie le
  contenu de la cible du lien (`os.Open` suit les liens par défaut sur
  Linux/Windows).
- **Le contenu est rédigé dans l'audit**, jamais conservé en clair :
  `content_base64` est remplacé par un marqueur de taille
  (`_redact_params`/`_REDACTED_PARAM_KEYS` dans `relay/broker.py`). L'audit
  prouve donc qu'un transfert a eu lieu (qui, quand, quel chemin, quelle
  taille) mais ne permet pas de reconstituer les données transférées après
  coup — une garantie de confidentialité du journal, pas une preuve
  d'intégrité du contenu.

Hors périmètre (assumé) : compromission du PC distant lui-même en dehors de
ce canal, ou compromission du poste opérateur du harnais — ce sont les
frontières de confiance du système, pas des failles qu'un durcissement du
relay peut combler.

## 1ter. Personnalisation à la compilation, adresse stable, GUI, défaut `auto`

Quatre entrées supplémentaires au modèle de menace, introduites par la
personnalisation à la compilation, l'adresse stable, le client graphique
(Fyne) et le passage du garde-fou local à `auto` par défaut. À prendre au
sérieux : aucune n'est un bug, toutes les quatre sont des compromis assumés
dont l'utilisateur/déployeur doit connaître le prix.

- **(a) Le binaire distribué EST lui-même un secret.** Un `RELAY_URL`, un
  `CLIENT_TOKEN` ou un `IDENTITY_SECRET` compilés dans un binaire personnalisé
  (`client/Makefile`, injectés via `-ldflags -X` dans les variables décrites
  par `client/buildconfig.go`) ne sont ni chiffrés ni obfusqués : un simple
  `strings claude-distant-client-linux-amd64 | grep wss://` (ou n'importe quel
  visualiseur hex/désassembleur) les récupère en clair, aussi facilement que
  s'ils étaient dans un fichier texte. `-ldflags -X` patche une valeur
  initiale de variable dans la section données du binaire ; ce n'est pas un
  mécanisme de secret. Conséquence pratique et non négociable : **le binaire
  personnalisé lui-même doit être manipulé, stocké et distribué avec
  exactement le même soin qu'un `docker/.env`** contenant ces mêmes valeurs en
  clair (§4) — jamais commité dans un dépôt, jamais posté sur un canal de
  distribution non maîtrisé (chat public, forge publique). Un jeton
  compromis via cette voie se révoque comme n'importe quel jeton compromis
  (rotation, §4), mais le fait qu'il ait fuité *via le binaire* est facile à
  manquer si on ne pense « secret » que pour les fichiers de configuration.

- **(b) L'adresse stable rend le code de session prédictible et rejouable
  dans le temps, ce qui affaiblit une garantie déjà documentée.**
  `docs/PROTOCOL.md` §3 énonçait jusqu'ici, sans condition, que les codes de
  session sont « régénérés à chaque connexion client » — c'est précisément
  cette ligne que l'adresse stable affaiblit. Avec `desired_code`
  (`docs/PROTOCOL.md` §1), `DeriveSessionCode` (`client/identity.go`)
  recalcule volontairement le **même** code à chaque reconnexion tant que la
  machine et le secret ne changent pas : la régénération à chaque connexion
  n'est plus vraie par défaut, elle devient une option (`--ephemeral-code`).
  Un code intercepté une fois (capture d'écran, regard par-dessus l'épaule,
  journal applicatif tiers) reste donc valide pour cibler la même machine
  indéfiniment d'une session à l'autre — le TTL court ne protège plus que la
  fenêtre d'une session *active*, plus la prévisibilité de l'adresse
  elle-même. Mitigations disponibles, à la charge du déploiement : compiler
  un `IDENTITY_SECRET` propre au déploiement (`client/Makefile`) rend le code
  non dérivable sans connaître ce secret ; `--ephemeral-code` /
  `CLAUDE_DISTANT_EPHEMERAL_CODE` désactive entièrement la fonctionnalité et
  restaure le tirage aléatoire d'avant, si la prévisibilité est inacceptable
  pour un déploiement donné. Sans `IDENTITY_SECRET` dédié, le sel par défaut
  du projet (`defaultIdentitySalt`, public dans le code source) rend le code
  dérivable par quiconque connaît le `MachineID` de la cible — lui-même pas un
  secret fort (`/etc/machine-id` est lisible par tout utilisateur local par
  construction sur Linux).

- **(c) Le mode automatique de la GUI désactive le garde-fou local, y
  compris pour les opérations fichiers, sur décision de l'utilisateur.**
  L'interrupteur « Mode automatique » de la fenêtre Fyne (`client/gui.go`)
  bascule le `PolicyController` de `confirm` vers `auto` en cours de session :
  tant qu'il reste actif, plus aucune commande shell (`run_shell`/
  `run_command`) ni opération fichier (`read_file`/`write_file`) n'est
  soumise à confirmation locale — la même politique que `--policy auto` en
  ligne de commande, mais activable/désactivable à la volée. C'est une
  décision prise par **l'utilisateur du PC distant lui-même**, pas par le
  harnais ni par l'opérateur distant, cohérente avec le modèle de consentement
  du projet (§1) — un avertissement visible et permanent reste affiché dans
  la fenêtre tant que le mode est actif, pour qu'elle ne soit jamais prise par
  inadvertance. Reste que, pour toute sa durée d'activation, elle supprime la
  dernière ligne de défense locale contre une commande destructive ou un
  transfert de fichier (lecture qui exfiltre, écriture qui altère) envoyé par
  un harnais compromis ou mal aiguillé — exactement le même compromis que
  `--policy auto` en console, rendu plus accessible et donc plus facile à
  activer sans y réfléchir.

- **(d) La politique de garde-fou par défaut est `auto` : aucune commande
  n'est soumise à confirmation locale tant que l'opérateur n'a rien
  demandé.** Le défaut historique était `confirm` (invite locale pour chaque
  commande classée destructive, et pour chaque `read_file`/`write_file`) ; il
  est passé à `auto` parce qu'en pratique une session d'administration
  enchaîne des dizaines d'opérations et qu'une invite par opération rendait
  le canal inutilisable. Le prix, à connaître : **sur un client lancé sans
  `--policy`, la dernière ligne de défense locale n'est pas active** — un
  `rm -rf`, un `shutdown` ou un `read_file /etc/shadow` envoyé par un harnais
  compromis, mal aiguillé (mauvais code de session) ou simplement trop
  confiant s'exécute sans que l'utilisateur du PC ait un mot à dire, exactement
  comme le décrit le point (c) pour le mode automatique de la GUI, mais sans
  qu'il ait fallu cocher quoi que ce soit. Ce qui reste en place, et sur quoi
  s'appuie donc désormais l'essentiel de la protection :
  - le **consentement d'entrée** (le client est lancé volontairement, le code
    de session est communiqué volontairement, la session est éphémère) ;
  - la **politique serveur** (`relay/command_policy.py` : allow/denylist sur
    les commandes *et* les chemins, quotas par session), indépendante du
    client et non désactivable depuis lui — c'est le levier à configurer pour
    un déploiement qui veut une limite dure ;
  - les **scopes MCP** par outil (§3) et le **kill-switch**
    (`terminate_session`) ;
  - l'**audit chaîné** (`relay/audit.py`), qui reste exhaustif.

  Un déploiement qui veut conserver l'ancien comportement le rétablit sans
  recompiler : `--policy confirm` (ou `CLAUDE_DISTANT_POLICY=confirm`), et
  `--policy deny` pour un refus systématique. La variante GUI expose le même
  choix à la volée via son interrupteur « mode automatique » — désormais
  coché au démarrage, avertissement affiché, décochable à tout moment. En
  console, le client annonce explicitement le mode automatique au démarrage,
  pour qu'il ne soit jamais actif à l'insu de l'utilisateur.

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

    # Indispensable pour `write_file` : le contenu (jusqu'à 8 Mio bruts,
    # ~11 Mo en base64) voyage dans un POST /mcp ; le défaut nginx (1m)
    # répondrait 413 au-delà de ~750 Ko.
    client_max_body_size 12m;

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
| `file:read` | `read_file` |
| `file:write` | `write_file` |

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

Côté client, la coupure est **définitive** : à la réception du message
`session_terminated` (ou du code de fermeture WebSocket 4402 qui le suit),
le client sort de sa boucle de reconnexion et s'arrête
(`client/main.go:errSessionTerminated`, idem pour la GUI). Sans cela, en mode
`CLIENT_AUTH_MODE=shared`, le client se reconnectait de lui-même après
quelques secondes avec le même jeton partagé et le même code stable, ce qui
réduisait le kill-switch à une brève interruption. Relancer le client reste
possible — c'est alors un nouveau consentement explicite de l'utilisateur
du poste.

## 7. Politique de commandes

Allow/denylist par expression régulière, quotas par session (nombre total,
débit par minute) — voir `relay/command_policy.py`. Les motifs s'appliquent
au **sujet filtrable** de l'outil : le champ `command` pour
`run_command`/`run_shell`, et le champ `path` pour `read_file`/`write_file`
— les mêmes listes `COMMAND_DENYLIST`/`COMMAND_ALLOWLIST` couvrent les deux
familles d'outils, pour qu'une denylist pensée pour contraindre le harnais
dans son ensemble ne laisse pas un trou béant côté transfert de fichiers
(ex. `cat /etc/shadow` refusé via `run_shell` mais le même chemin accessible
tel quel via `read_file` si le filtrage ne portait que sur les commandes). La
denylist est toujours prioritaire sur l'allowlist. Les autres outils
(`system_info`, `connect_session`...) n'ont pas de sujet filtrable et ne sont
soumis qu'aux quotas. Configuration via `COMMAND_DENYLIST`/
`COMMAND_ALLOWLIST`/`MAX_COMMANDS_PER_SESSION`/`RATE_LIMIT_PER_MINUTE`.
