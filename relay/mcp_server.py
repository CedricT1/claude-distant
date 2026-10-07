"""Serveur MCP (Streamable HTTP) exposant les outils au harnais : `connect_session`,
`system_info`, `run_command`, `run_shell` (MVP), `terminate_session` (kill-switch,
phase 5), `issue_client_token` (émission de jeton client `per_session`, phase 5)
et `read_file`/`write_file` (transfert de fichiers, phase 6).

Utilise le **SDK MCP officiel** (`mcp.server.fastmcp.FastMCP`) pour la
définition des outils et le transport Streamable HTTP — voir
`FastMCP.streamable_http_app()`. Cette couche ne connaît rien du réseau
client↔relay : elle appelle uniquement `broker.dispatch_command(...)` /
`broker.get_session_info(...)` / `broker.terminate_session(...)` (voir
`broker.py`) et traduit les chunks `stream`/`result` agrégés (ou les
exceptions du protocole, dont `CommandDeniedError` — refus par
`CommandPolicy`, phase 5) en un dict de résultat structuré pour l'outil.

## Auth MCP : `MCP_AUTH_MODE=static_bearer` (défaut) vs `oauth`

Deux modes, sélectionnés par l'appelant (`relay/app.py`) via
`build_mcp_asgi_app(broker, mode=...)` :

- `static_bearer` (défaut, compat MVP inchangée) : Bearer pré-partagé
  (`MCP_BEARER_TOKEN`) via un middleware ASGI minimal
  (:class:`BearerAuthMiddleware`), sans notion de scope — tous les outils
  sont accessibles à quiconque présente le jeton. Volontairement séparé des
  primitives OAuth du SDK MCP : un jeton statique unique ne bénéficierait de
  rien à passer par `token_verifier`/`AuthSettings`.

- `oauth` : **Resource Server OAuth 2.1** utilisant réellement les primitives
  `mcp.server.auth` du SDK — `FastMCP(token_verifier=JWTTokenVerifier(...),
  auth=AuthSettings(...))`. À la construction de `streamable_http_app()`, le
  SDK câble lui-même `BearerAuthBackend` (valide le jeton via
  `JWTTokenVerifier.verify_token`, cf. `relay/jwt_auth.py`) +
  `AuthContextMiddleware` (expose le jeton validé via
  `mcp.server.auth.middleware.auth_context.get_access_token()` pendant
  l'exécution de l'outil) + `RequireAuthMiddleware` (401 si aucun jeton
  valide). C'est le point d'entrée officiel du SDK, pas un middleware maison.

  **Compromis assumé** : `AuthSettings.required_scopes` n'exprime qu'un
  ensemble de scopes *global* à tout l'endpoint MCP, alors que nos outils
  demandent des scopes *différents* (`connect_session` ≠ `run_command` ≠
  `terminate_session` ≠ `issue_client_token`). On configure donc
  `required_scopes=[]` au niveau transport (« un jeton valide et non expiré
  suffit pour entrer ») et l'enforcement **par outil** est fait ici, dans
  chaque fonction d'outil, via `get_access_token()` (voir `_check_scope` /
  `TOOL_SCOPES` ci-dessous) — c'est la seule partie qui n'est pas déléguée
  telle quelle au SDK, faute d'un mécanisme SDK par-outil pour l'exprimer.
  Un jeton sans le scope requis reçoit une erreur d'outil claire
  (`{"status": "error", "error": "forbidden_scope", ...}`) et l'événement est
  journalisé dans l'audit (`decision: "denied"`) si un `audit_log` est fourni.

`require_scopes=False` (défaut de `create_mcp_server`) désactive totalement
cet enforcement par outil, pour ne rien changer au comportement historique
(tests existants qui appellent les outils directement sans aucun contexte
d'authentification).

## Transfert de fichiers et négociation de capacités

`read_file`/`write_file` ne sont utilisables qu'avec un client qui a déclaré la
capacité `file_transfer` à son `register` (voir `_require_capability`). Un
client déjà déployé n'en déclare aucune : il reçoit un refus immédiat et
explicite (`unsupported_by_client`) au lieu d'une trame `command` portant un
`tool` qu'il ne saurait pas traiter — c'est le cœur de la rétrocompatibilité.

L'écriture n'est **pas** chunkée relay→client : tout le base64 tient dans
l'unique trame `command`, d'où le plafond :data:`MAX_FILE_TRANSFER_BYTES`
(8 MiB) vérifié ici, avant tout dispatch. La lecture, elle, remonte en
plusieurs messages `file_chunk` que cette couche réassemble par `seq` — le
même plafond y est appliqué de façon cumulative au fil du flux (voir
`_dispatch_file_transfer`), pour la raison symétrique : le timeout du broker
porte sur *chaque* tranche, pas sur le transfert dans son ensemble, donc rien
n'empêchait auparavant un client de tenir un `read_file` ouvert indéfiniment
en émettant des tranches en continu et de faire grossir la mémoire du relay
sans borne.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import math
from typing import TYPE_CHECKING, Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .auth import PerSessionTokenStore, extract_bearer_token, verify_token
from .broker import (
    Broker,
    ClientDisconnectedError,
    CommandDeniedError,
    CommandTimeoutError,
    SessionNotFoundError,
)
from .jwt_auth import DEFAULT_ALGORITHM, JWTTokenVerifier

if TYPE_CHECKING:
    from .audit import AuditLog

SERVER_NAME = "claude-distant-relay"

DEFAULT_CLIENT_TOKEN_TTL_SECONDS = 1800.0
DEFAULT_RESOURCE_SERVER_URL = "https://claude-distant.local/mcp"
DEFAULT_ISSUER_URL = "https://claude-distant.local/"

# Scope MCP requis par outil, appliqué uniquement quand `require_scopes=True`
# (mode oauth) — voir docstring de module.
SCOPE_SESSION_CONNECT = "session:connect"
SCOPE_COMMAND_EXECUTE = "command:execute"
SCOPE_SESSION_TERMINATE = "session:terminate"
SCOPE_CLIENT_PROVISION = "client:provision"
SCOPE_FILE_READ = "file:read"
SCOPE_FILE_WRITE = "file:write"

TOOL_SCOPES: dict[str, str] = {
    "connect_session": SCOPE_SESSION_CONNECT,
    "run_command": SCOPE_COMMAND_EXECUTE,
    "run_shell": SCOPE_COMMAND_EXECUTE,
    "terminate_session": SCOPE_SESSION_TERMINATE,
    "issue_client_token": SCOPE_CLIENT_PROVISION,
    # Scopes distincts de `command:execute` : les jetons déjà émis ne les
    # portent pas et se voient donc refuser les nouveaux outils (`forbidden_scope`),
    # ce qui est le comportement sûr voulu — lire/écrire un fichier arbitraire
    # est une capacité qu'un opérateur doit accorder explicitement.
    "read_file": SCOPE_FILE_READ,
    "write_file": SCOPE_FILE_WRITE,
}

# Capacité que le client doit déclarer à `register` pour se voir dispatcher
# `read_file`/`write_file` (voir `relay/app.py` et `relay/session_store.py`).
CAPABILITY_FILE_TRANSFER = "file_transfer"
# Première version du client Go qui l'annonce ; citée dans le message d'erreur
# pour que le harnais sache quoi répondre à l'humain.
MIN_FILE_TRANSFER_CLIENT_VERSION = "0.2.0"
# Plafond d'un transfert, dans les deux sens. Côté écriture, il découle
# directement du fait que le contenu voyage dans une seule trame `command`.
MAX_FILE_TRANSFER_BYTES = 8 * 1024 * 1024
# Marge ajoutée au `timeout` du harnais pour l'attente côté relay. Le
# `timeout` est transmis au client (clé `params.timeout`, en secondes
# entières), qui tue le processus à l'échéance et renvoie un `result`
# portant `error="timeout"`. Pour que ce `result` ait le temps d'arriver
# (au lieu que relay et client abandonnent au même instant et que le
# harnais ne voie qu'un timeout relay anonyme), le relay attend un peu
# plus longtemps que le client — voir `_resolve_command_timeout`.
COMMAND_TIMEOUT_GRACE_SECONDS = 5.0


def _resolve_command_timeout(
    timeout: float | None,
) -> tuple[int | None, float | None, dict[str, Any] | None]:
    """Normalise le `timeout` optionnel de `run_command`/`run_shell`.

    Retourne `(timeout_client, timeout_relay, erreur)` :
      - `timeout_client` : valeur entière (secondes, arrondie vers le haut)
        à placer dans `params.timeout` pour que le **client** borne lui-même
        l'exécution (`client/executor.go` lit un `int`, un flottant JSON
        ferait échouer son décodage) ; `None` si le harnais n'a rien demandé,
        auquel cas la trame `command` reste identique à ce qu'elle était
        avant que ce paramètre ne soit transmis.
      - `timeout_relay` : délai d'attente du broker, soit le timeout client
        plus :data:`COMMAND_TIMEOUT_GRACE_SECONDS` ; `None` pour le défaut.
      - `erreur` : dict d'outil `invalid_params` si `timeout` est fourni mais
        non strictement positif (0 vaudrait « pas de limite » côté client,
        exactement l'ambiguïté que `read_file` refuse déjà pour `max_bytes`).

    Historiquement `timeout` ne réglait que l'attente du relay : le client
    exécutait toujours avec son propre défaut (5 min), si bien qu'un
    harnais demandant 10 s recevait bien `timeout` après 10 s… tandis que
    le processus continuait de tourner sur la cible.
    """
    if timeout is None:
        return None, None, None
    if timeout <= 0:
        return None, None, {
            "status": "error",
            "error": "invalid_params",
            "detail": (
                f"`timeout` doit être strictement positif s'il est fourni "
                f"(reçu {timeout!r})"
            ),
        }
    client_timeout = int(math.ceil(timeout))
    return client_timeout, client_timeout + COMMAND_TIMEOUT_GRACE_SECONDS, None


def _check_scope(
    require_scopes: bool,
    tool: str,
    required_scope: str,
    audit_log: "AuditLog | None",
    session_code: str | None = None,
) -> dict[str, Any] | None:
    """Retourne un dict d'erreur d'outil si `required_scope` manque, sinon `None`.

    No-op (retourne toujours `None`) si `require_scopes` est `False` — c'est
    ce qui préserve le comportement `static_bearer`/MVP historique (aucune
    notion de scope). En mode oauth, lit le jeton validé par le SDK via
    `get_access_token()` (contextvar posé par `AuthContextMiddleware` pour une
    vraie requête HTTP, ou positionné directement dans les tests unitaires).
    """
    if not require_scopes:
        return None

    access_token = get_access_token()
    if access_token is not None and required_scope in (access_token.scopes or []):
        return None

    if audit_log is not None:
        audit_log.record(
            {
                "session_code": session_code,
                "tool": tool,
                "decision": "denied",
                "outcome": {"reason": f"missing_scope:{required_scope}"},
            }
        )
    return {
        "status": "error",
        "error": "forbidden_scope",
        "detail": f"le jeton ne porte pas le scope requis : {required_scope!r}",
    }


async def _require_capability(
    broker: Broker, session_code: str, capability: str
) -> dict[str, Any] | None:
    """Retourne un dict d'erreur d'outil si la session est inconnue ou si le
    client ne déclare pas `capability` ; sinon `None`.

    Vérifié **avant** tout dispatch : un client déjà déployé ne doit jamais
    recevoir une `command` dont il ignore le `tool` (il l'ignorerait
    silencieusement et la commande partirait en timeout côté harnais, sans
    diagnostic). Ici, le harnais obtient immédiatement un message actionnable
    nommant la version du client et la version minimale attendue.
    """
    record = await broker.get_session_info(session_code)
    if record is None:
        # Même code d'erreur que `_dispatch_and_aggregate` pour une session
        # inconnue : le harnais n'a pas à distinguer les deux chemins.
        return {
            "status": "error",
            "error": "session_not_found",
            "detail": f"session inconnue ou expirée : {session_code}",
        }
    if capability not in (record.capabilities or ()):
        return {
            "status": "error",
            "error": "unsupported_by_client",
            "detail": (
                f"le client distant (version {record.version!r}) ne supporte pas le "
                f"transfert de fichiers ; mettez-le à jour vers une version "
                f"≥ {MIN_FILE_TRANSFER_CLIENT_VERSION}"
            ),
            "client_version": record.version,
            "capabilities": list(record.capabilities or ()),
        }
    return None


def create_mcp_server(
    broker: Broker,
    *,
    require_scopes: bool = False,
    client_token_store: "PerSessionTokenStore | None" = None,
    client_token_ttl_seconds: float = DEFAULT_CLIENT_TOKEN_TTL_SECONDS,
    audit_log: "AuditLog | None" = None,
    token_verifier: "TokenVerifier | None" = None,
    auth_settings: "AuthSettings | None" = None,
) -> FastMCP:
    """Construit un `FastMCP` et y enregistre les outils, branchés sur
    `broker.dispatch_command`.

    `broker` doit exposer `get_session_info(session_code)`,
    `dispatch_command(session_code, tool, params, timeout)` et
    `terminate_session(session_code)` (voir `relay.broker.Broker` ; un double
    de test compatible suffit).

    Tous les paramètres après `broker` sont optionnels et à valeur par défaut
    neutre : un appel `create_mcp_server(broker)` reproduit exactement le
    comportement MVP (aucun scope, pas d'`issue_client_token` fonctionnel sans
    store, pas d'audit des refus). `token_verifier`/`auth_settings` sont
    transmis tels quels au constructeur `FastMCP` (mode oauth, voir
    `build_mcp_asgi_app`) ; `host="0.0.0.0"` est fixé explicitement pour
    éviter que `FastMCP` n'active silencieusement sa protection anti DNS
    rebinding restreinte à `localhost` (son défaut quand `host` n'est pas
    précisé) — inadaptée à un relay conçu pour tourner derrière un reverse
    proxy TLS avec un nom d'hôte public (voir `docs/SECURITY.md`).
    """
    mcp = FastMCP(
        name=SERVER_NAME,
        stateless_http=True,
        host="0.0.0.0",
        token_verifier=token_verifier,
        auth=auth_settings,
    )

    @mcp.tool()
    async def connect_session(session_code: str) -> dict[str, Any]:
        """Vérifie qu'une session est active et retourne son OS/hostname/version.

        Retourne aussi les `capabilities` déclarées par le client (liste vide
        pour un client antérieur à la négociation de capacités) : c'est ce qui
        permet au harnais de savoir *avant* d'essayer si `read_file`/`write_file`
        sont utilisables sur cette cible.
        """
        scope_error = _check_scope(
            require_scopes, "connect_session", SCOPE_SESSION_CONNECT, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        record = await broker.get_session_info(session_code)
        if record is None:
            return {"status": "not_found", "session_code": session_code}
        return {
            "status": "connected",
            "session_code": session_code,
            "os": record.os,
            "hostname": record.hostname,
            "version": record.version,
            "capabilities": list(record.capabilities or ()),
        }

    @mcp.tool()
    async def system_info(session_code: str) -> dict[str, Any]:
        """Retourne les infos système (OS, uptime, RAM, CPU) de la cible."""
        return await _dispatch_and_aggregate(broker, session_code, "system_info", {})

    @mcp.tool()
    async def run_command(
        session_code: str, command: str, timeout: float | None = None
    ) -> dict[str, Any]:
        """Exécute une commande simple sur la cible (stdout/stderr/exit_code).

        `timeout` (secondes, optionnel) est transmis au client, qui tue le
        processus à l'échéance (plafonné à 30 min côté client) ; le relay
        attend ce même délai plus une courte marge. Doit être strictement
        positif s'il est fourni (`invalid_params` sinon).
        """
        scope_error = _check_scope(
            require_scopes, "run_command", SCOPE_COMMAND_EXECUTE, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        client_timeout, relay_timeout, timeout_error = _resolve_command_timeout(timeout)
        if timeout_error is not None:
            return timeout_error
        params: dict[str, Any] = {"command": command}
        if client_timeout is not None:
            params["timeout"] = client_timeout
        return await _dispatch_and_aggregate(
            broker, session_code, "run_command", params, timeout=relay_timeout
        )

    @mcp.tool()
    async def run_shell(
        session_code: str,
        command: str,
        shell: str = "auto",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Exécute une commande dans le shell natif de la cible.

        `shell="auto"` (défaut) : PowerShell sur Windows, Bash sur Linux,
        selon l'OS détecté à `register`. Override possible :
        `powershell`/`pwsh`/`bash`/`sh`. `timeout` : même sémantique que
        pour `run_command` (transmis au client, strictement positif).
        """
        scope_error = _check_scope(
            require_scopes, "run_shell", SCOPE_COMMAND_EXECUTE, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        client_timeout, relay_timeout, timeout_error = _resolve_command_timeout(timeout)
        if timeout_error is not None:
            return timeout_error
        params: dict[str, Any] = {"command": command, "shell": shell}
        if client_timeout is not None:
            params["timeout"] = client_timeout
        return await _dispatch_and_aggregate(
            broker, session_code, "run_shell", params, timeout=relay_timeout
        )

    @mcp.tool()
    async def read_file(
        session_code: str,
        path: str,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Rapatrie un fichier de la machine distante vers le harnais.

        Le contenu remonte en plusieurs `file_chunk` que le relay réassemble
        par `seq` croissant, puis retourne en base64 (`content_base64`), avec
        sa taille, son SHA-256 et un drapeau `truncated` indiquant qu'il restait
        des octets au-delà de `max_bytes`. `offset`/`max_bytes` ne sont
        transmis au client que lorsqu'ils sont signifiants, pour que la trame
        `command` d'une lecture simple reste identique à ce qu'elle serait sans
        eux. Le chemin n'est pas confiné à un workspace (outil d'administration
        système) et `~` n'est pas expansé.

        `max_bytes` doit être strictement positif s'il est fourni, et
        `offset` ne peut pas être négatif : rejetés ici (`invalid_params`)
        avant tout dispatch. Ce n'est pas qu'une histoire de valeurs
        absurdes — le protocole client (Go) représente `MaxBytes` par un
        `int64` où `0` signifie aussi bien « absent » que « zéro octet
        demandé », et sa garde retombe sur « pas de limite » dans les deux
        cas. Le relay ne peut pas lever cette ambiguïté après coup ; il ne
        peut que refuser en amont la valeur qui la déclencherait, plutôt que
        de laisser croire à un plafond de zéro octet alors que le fichier
        entier serait renvoyé.
        """
        if max_bytes is not None and max_bytes <= 0:
            return {
                "status": "error",
                "error": "invalid_params",
                "detail": (
                    f"`max_bytes` doit être strictement positif s'il est fourni "
                    f"(reçu {max_bytes!r}) ; côté client, 0 est indiscernable "
                    f"d'une absence de plafond"
                ),
            }
        if offset < 0:
            return {
                "status": "error",
                "error": "invalid_params",
                "detail": f"`offset` ne peut pas être négatif (reçu {offset!r})",
            }

        scope_error = _check_scope(
            require_scopes, "read_file", SCOPE_FILE_READ, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        capability_error = await _require_capability(
            broker, session_code, CAPABILITY_FILE_TRANSFER
        )
        if capability_error is not None:
            return capability_error

        params: dict[str, Any] = {"path": path}
        if offset:
            params["offset"] = offset
        if max_bytes is not None:
            params["max_bytes"] = max_bytes

        received, outcome, protocol_error = await _dispatch_file_transfer(
            broker, session_code, "read_file", params
        )
        if protocol_error is not None:
            return protocol_error
        if outcome.get("error"):
            return {
                "status": "error",
                "error": outcome["error"],
                "path": path,
                "exit_code": outcome.get("exit_code"),
            }

        try:
            content = _reassemble_chunks(received)
        except binascii.Error as exc:
            return {
                "status": "error",
                "error": "invalid_base64",
                "detail": f"tranche de fichier illisible envoyée par le client : {exc}",
                "path": path,
            }

        # `meta` fait foi quand le client le fournit (il connaît la taille
        # réelle du fichier et donc la troncature) ; sinon on déduit tout des
        # octets effectivement reçus — un ancien relay/client n'aurait de
        # toute façon jamais atteint ce code.
        meta = outcome.get("meta") or {}
        size = meta.get("size")
        if not isinstance(size, int):
            size = len(content)
        return {
            "status": "ok",
            "path": meta.get("path") or path,
            "encoding": "base64",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "size": size,
            "sha256": meta.get("sha256") or hashlib.sha256(content).hexdigest(),
            "truncated": bool(meta.get("truncated", False)),
        }

    @mcp.tool()
    async def write_file(
        session_code: str,
        path: str,
        content_base64: str,
        mode: str | None = None,
        create_dirs: bool = False,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """Envoie un fichier du harnais vers la machine distante.

        Le contenu voyage en base64 dans une **unique** trame `command` (pas de
        découpage relay→client) : le payload décodé est donc plafonné à
        :data:`MAX_FILE_TRANSFER_BYTES` (8 MiB), et la validité du base64 comme
        la taille sont vérifiées ici, avant tout dispatch — inutile de réveiller
        le client pour lui faire rejeter une charge qu'on sait déjà invalide.
        `mode` est une chaîne octale (`"0644"`) ; `create_dirs`/`overwrite` ne
        sont transmis que lorsqu'ils s'écartent du défaut.
        """
        scope_error = _check_scope(
            require_scopes, "write_file", SCOPE_FILE_WRITE, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        capability_error = await _require_capability(
            broker, session_code, CAPABILITY_FILE_TRANSFER
        )
        if capability_error is not None:
            return capability_error

        try:
            payload = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            return {
                "status": "error",
                "error": "invalid_base64",
                "detail": f"`content_base64` n'est pas du base64 valide : {exc}",
            }
        if len(payload) > MAX_FILE_TRANSFER_BYTES:
            return {
                "status": "error",
                "error": "file_too_large",
                "detail": (
                    f"{len(payload)} octets dépassent le plafond de transfert "
                    f"({MAX_FILE_TRANSFER_BYTES} octets)"
                ),
            }

        params: dict[str, Any] = {"path": path, "content_base64": content_base64}
        if mode is not None:
            params["mode"] = mode
        if create_dirs:
            params["create_dirs"] = True
        if not overwrite:
            params["overwrite"] = False

        _received, outcome, protocol_error = await _dispatch_file_transfer(
            broker, session_code, "write_file", params
        )
        if protocol_error is not None:
            return protocol_error
        if outcome.get("error"):
            return {
                "status": "error",
                "error": outcome["error"],
                "path": path,
                "exit_code": outcome.get("exit_code"),
            }

        meta = outcome.get("meta") or {}
        bytes_written = meta.get("bytes_written")
        if not isinstance(bytes_written, int):
            bytes_written = len(payload)
        return {
            "status": "ok",
            "path": meta.get("path") or path,
            "bytes_written": bytes_written,
            "sha256": meta.get("sha256") or hashlib.sha256(payload).hexdigest(),
        }

    @mcp.tool()
    async def terminate_session(session_code: str) -> dict[str, Any]:
        """Kill-switch : invalide immédiatement une session active.

        Ferme/notifie la connexion client WS et fait échouer proprement toute
        commande en cours pour cette session (cf. `Broker.terminate_session`).
        """
        scope_error = _check_scope(
            require_scopes, "terminate_session", SCOPE_SESSION_TERMINATE, audit_log, session_code
        )
        if scope_error is not None:
            return scope_error
        terminated = await broker.terminate_session(session_code)
        if not terminated:
            return {"status": "not_found", "session_code": session_code}
        return {"status": "terminated", "session_code": session_code}

    @mcp.tool()
    async def issue_client_token(ttl_seconds: float | None = None) -> dict[str, Any]:
        """Émet un jeton client court à usage unique (mode `CLIENT_AUTH_MODE=per_session`).

        Le jeton retourné doit être transmis à l'opérateur/PC distant pour
        authentifier la prochaine connexion WS
        (`Authorization: Bearer <jeton>`) ; il est consommé dès le premier
        `register` réussi (cf. `relay/auth.py:PerSessionTokenStore`). Protégé
        par le scope `client:provision` en mode oauth. Remplace l'émission
        manuelle par appel direct à `PerSessionTokenStore.issue(...)` côté
        opérateur/déploiement (TODO historique de la vague précédente).
        """
        scope_error = _check_scope(require_scopes, "issue_client_token", SCOPE_CLIENT_PROVISION, audit_log)
        if scope_error is not None:
            return scope_error
        if client_token_store is None:
            return {
                "status": "error",
                "error": "not_configured",
                "detail": "aucun PerSessionTokenStore configuré sur ce relay",
            }
        ttl = ttl_seconds if ttl_seconds is not None else client_token_ttl_seconds
        token = client_token_store.issue(ttl_seconds=ttl)
        return {"status": "ok", "token": token, "expires_in": ttl}

    return mcp


async def _dispatch_and_aggregate(
    broker: Broker,
    session_code: str,
    tool: str,
    params: dict[str, Any],
    timeout: float | None = None,
) -> dict[str, Any]:
    """Consomme `broker.dispatch_command(...)` et agrège en un résultat unique.

    Concatène les chunks `stream` par flux (stdout/stderr) et rapporte le
    `result` final. Traduit les erreurs du protocole (session inconnue,
    client déconnecté, timeout) en un dict `{"status": "error", ...}`
    exploitable par le harnais, sans jamais laisser fuiter une exception
    Python côté MCP.
    """
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    outcome: dict[str, Any] = {}
    approved: bool | None = None

    try:
        async for chunk in broker.dispatch_command(session_code, tool, params, timeout=timeout):
            chunk_type = chunk.get("type")
            if chunk_type == "stream":
                if chunk.get("stream") == "stderr":
                    stderr_parts.append(chunk.get("data") or "")
                else:
                    stdout_parts.append(chunk.get("data") or "")
            elif chunk_type == "result":
                # `meta` (transfert de fichiers) est délibérément ignoré ici :
                # la sortie des outils de commande doit rester strictement
                # identique à ce qu'elle était avant son introduction.
                outcome = {"exit_code": chunk.get("exit_code"), "error": chunk.get("error")}
            elif chunk_type == "approval_response":
                # On garde la trace de l'approbation sans en faire l'issue :
                # le client émet son `approval_response` *avant* d'exécuter la
                # commande approuvée, c'est donc le `result` final qui fixe
                # `exit_code`/`error` (y compris `refused_by_user` /
                # `refused_by_policy` en cas de refus).
                approved = chunk.get("approved")
    except SessionNotFoundError as exc:
        return {"status": "error", "error": "session_not_found", "detail": str(exc)}
    except ClientDisconnectedError as exc:
        return {"status": "error", "error": "client_disconnected", "detail": str(exc)}
    except CommandTimeoutError as exc:
        return {"status": "error", "error": "timeout", "detail": str(exc)}
    except CommandDeniedError as exc:
        return {"status": "error", "error": "denied", "detail": str(exc)}

    if not outcome and approved is False:
        # Filet de sécurité : un client qui se tairait après avoir refusé (sans
        # émettre le `result` attendu) ne doit pas passer pour un succès vide.
        outcome = {"exit_code": None, "error": "refused_by_user"}

    return {
        "status": "ok",
        "stdout": "".join(stdout_parts),
        "stderr": "".join(stderr_parts),
        **outcome,
    }


async def _dispatch_file_transfer(
    broker: Broker,
    session_code: str,
    tool: str,
    params: dict[str, Any],
) -> tuple[list[tuple[int, str]], dict[str, Any], dict[str, Any] | None]:
    """Consomme `broker.dispatch_command(...)` pour un outil de fichier.

    Agrégateur dédié : contrairement à `_dispatch_and_aggregate`, il collecte
    les `file_chunk` (paires `(seq, base64)`, laissées brutes pour que
    l'appelant décide du réassemblage) et conserve le `meta` du `result`, qui
    porte taille/SHA-256/troncature. Retourne
    `(chunks, outcome, erreur_protocole)` : `erreur_protocole` est un dict
    d'outil prêt à retourner (session inconnue, déconnexion, timeout, refus par
    la politique, ou dépassement du plafond ci-dessous), `None` si le flux
    s'est déroulé normalement.

    Le volume cumulé des `file_chunk` (mesuré en caractères base64, donc une
    borne conservative sur les octets décodés — cf. `_reassemble_chunks`) est
    plafonné à :data:`MAX_FILE_TRANSFER_BYTES`, comme `write_file` plafonne
    déjà le payload décodé. Sans cette borne, un client qui enchaînerait les
    `file_chunk` sans jamais émettre le `result` final ferait grossir la
    mémoire du relay sans limite : le timeout du broker s'applique par
    tranche (`asyncio.wait_for` autour de chaque `queue.get()`, cf.
    `Broker.dispatch_command`), pas sur la durée totale du transfert. Le
    dépassement interrompt donc la boucle *avant* d'accumuler la tranche en
    trop, plutôt que d'attendre la fin du flux pour vérifier la taille.

    Le générateur `broker.dispatch_command(...)` est piloté via
    `contextlib.aclosing` : que la sortie de la boucle vienne d'un `break`
    (plafond dépassé) ou d'une exception, `aclose()` est appelé, ce qui
    réveille le `finally` de `Broker.dispatch_command` et retire l'entrée de
    `Broker._pending` — indispensable ici puisque, contrairement à une sortie
    normale par `result`, un `break` ne laisserait sinon aucune chance au
    générateur de se nettoyer lui-même.
    """
    received: list[tuple[int, str]] = []
    outcome: dict[str, Any] = {}
    received_size = 0
    too_large = False

    generator = broker.dispatch_command(session_code, tool, params)
    try:
        async with contextlib.aclosing(generator):
            async for chunk in generator:
                chunk_type = chunk.get("type")
                if chunk_type == "file_chunk":
                    data = chunk.get("data") or ""
                    received_size += len(data)
                    if received_size > MAX_FILE_TRANSFER_BYTES:
                        too_large = True
                        break
                    seq = chunk.get("seq")
                    # Un `seq` absent/non entier retombe sur l'ordre d'arrivée :
                    # le transport WS préserve l'ordre, `seq` n'est là que pour
                    # rendre un désordre détectable.
                    received.append((seq if isinstance(seq, int) else len(received), data))
                elif chunk_type == "result":
                    outcome = {
                        "exit_code": chunk.get("exit_code"),
                        "error": chunk.get("error"),
                        "meta": chunk.get("meta"),
                    }
    except SessionNotFoundError as exc:
        return received, outcome, {"status": "error", "error": "session_not_found", "detail": str(exc)}
    except ClientDisconnectedError as exc:
        return received, outcome, {"status": "error", "error": "client_disconnected", "detail": str(exc)}
    except CommandTimeoutError as exc:
        return received, outcome, {"status": "error", "error": "timeout", "detail": str(exc)}
    except CommandDeniedError as exc:
        return received, outcome, {"status": "error", "error": "denied", "detail": str(exc)}

    if too_large:
        return received, outcome, {
            "status": "error",
            "error": "file_too_large",
            "detail": (
                f"le flux de lecture dépasse le plafond de transfert "
                f"({MAX_FILE_TRANSFER_BYTES} octets) ; lecture interrompue"
            ),
        }

    return received, outcome, None


def _reassemble_chunks(received: list[tuple[int, str]]) -> bytes:
    """Réordonne les tranches par `seq` et concatène leurs octets décodés.

    Chaque tranche est décodée séparément : concaténer les chaînes base64
    produirait du padding en plein milieu, donc du base64 invalide. Lève
    `binascii.Error` si une tranche est illisible.
    """
    return b"".join(
        base64.b64decode(data, validate=True)
        for _seq, data in sorted(received, key=lambda item: item[0])
    )


class BearerAuthMiddleware:
    """Middleware ASGI minimal : exige `Authorization: Bearer <MCP_BEARER_TOKEN>`.

    Utilisé uniquement en mode `static_bearer` (voir docstring de module) ;
    ne s'applique qu'aux requêtes HTTP (laisse passer les autres types de
    scope, ex. `lifespan`, tels quels).
    """

    def __init__(self, app: ASGIApp, expected_token: str) -> None:
        self._app = app
        self._expected_token = expected_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_auth = headers.get(b"authorization", b"").decode("latin-1")
        token = extract_bearer_token(raw_auth)
        if not verify_token(token, self._expected_token):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def build_mcp_asgi_app(
    broker: Broker,
    *,
    mode: str = "static_bearer",
    bearer_token: str | None = None,
    jwt_secret: str | None = None,
    jwt_algorithm: str = DEFAULT_ALGORITHM,
    resource_server_url: str | None = None,
    issuer_url: str | None = None,
    client_token_store: "PerSessionTokenStore | None" = None,
    client_token_ttl_seconds: float = DEFAULT_CLIENT_TOKEN_TTL_SECONDS,
    audit_log: "AuditLog | None" = None,
) -> tuple[FastMCP, ASGIApp]:
    """Construit le serveur MCP et son app ASGI, selon `mode` (`MCP_AUTH_MODE`).

    Retourne `(mcp, asgi_app)` : `mcp` est nécessaire à l'appelant (`app.py`)
    pour piloter le cycle de vie de `mcp.session_manager` (voir docstring de
    `app.py` sur le montage lifespan d'une sous-app Starlette dans FastAPI).

    - `mode="static_bearer"` (défaut) : comportement MVP inchangé — un unique
      `MCP_BEARER_TOKEN` protège l'endpoint via :class:`BearerAuthMiddleware`,
      aucun enforcement de scope (`require_scopes=False`).
    - `mode="oauth"` : câble le **vrai** SDK MCP (`token_verifier`/`auth`, cf.
      docstring de module) avec un :class:`~relay.jwt_auth.JWTTokenVerifier`
      HS256 ; l'app ASGI retournée est directement `mcp.streamable_http_app()`
      (déjà protégée par `BearerAuthBackend`/`RequireAuthMiddleware` côté
      SDK), sans couche `BearerAuthMiddleware` supplémentaire.

    Dans les deux modes, `client_token_store`/`audit_log` sont transmis à
    `create_mcp_server` pour activer `issue_client_token` / l'audit des refus
    de scope.
    """
    if mode == "oauth":
        verifier = JWTTokenVerifier(secret=jwt_secret or "", algorithm=jwt_algorithm)
        auth_settings = AuthSettings(
            issuer_url=issuer_url or DEFAULT_ISSUER_URL,
            resource_server_url=resource_server_url or DEFAULT_RESOURCE_SERVER_URL,
            required_scopes=[],
        )
        mcp = create_mcp_server(
            broker,
            require_scopes=True,
            client_token_store=client_token_store,
            client_token_ttl_seconds=client_token_ttl_seconds,
            audit_log=audit_log,
            token_verifier=verifier,
            auth_settings=auth_settings,
        )
        return mcp, mcp.streamable_http_app()

    # mode == "static_bearer" (défaut, comportement MVP inchangé)
    mcp = create_mcp_server(
        broker,
        client_token_store=client_token_store,
        client_token_ttl_seconds=client_token_ttl_seconds,
        audit_log=audit_log,
    )
    inner_app = mcp.streamable_http_app()
    return mcp, BearerAuthMiddleware(inner_app, bearer_token or "")
