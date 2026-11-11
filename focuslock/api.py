"""A small REST API built only on the standard library (http.server).

Security model: it listens on 127.0.0.1 only, every route except /health needs
`Authorization: Bearer <token>` (token stored in the data folder), the Host header must
be a loopback name (blocks DNS-rebinding attacks from web pages), and request bodies are
capped at 64 KiB.
"""

import hmac
import json
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .errors import (
    FocusLockError,
    InvalidDomainError,
    InvalidTransitionError,
    PasswordError,
    RewardError,
    SessionConflictError,
)

if TYPE_CHECKING:
    from .app import FocusLockApp

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
MAX_BODY_BYTES = 64 * 1024
TOKEN_FILE = "api_token"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
PASSWORD_HEADER = "X-FocusLock-Password"


class ApiError(Exception):
    def __init__(
        self, status: HTTPStatus, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str]
    query: dict[str, list[str]]
    body: Any
    password: str | None = None

    def query_int(self, name: str, default: int, low: int, high: int) -> int:
        values = self.query.get(name)
        if not values:
            return default
        try:
            value = int(values[0])
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be an integer") from None
        if not low <= value <= high:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be between {low} and {high}")
        return value

    def query_bool(self, name: str, default: bool) -> bool:
        values = self.query.get(name)
        if not values:
            return default
        return values[0].lower() in {"1", "true", "yes"}

    def field(self, name: str, kind: type, *, required: bool = True, default: Any = None) -> Any:
        if self.body is None:
            body: dict[str, Any] = {}
        elif isinstance(self.body, dict):
            body = self.body
        else:
            raise ApiError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        value = body.get(name)
        if value is None:
            if required:
                raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"missing field: {name}")
            return default
        wrong_bool = isinstance(value, bool) and kind is not bool
        if wrong_bool or not isinstance(value, kind):
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, f"{name} must be {kind.__name__}")
        return value


Response = tuple[int, Any]
Handler = Callable[[Request], Response]


class Router:
    """Maps (method, path pattern) to a handler. '/sessions/{id}' becomes a regex with a
    named group, so the router can tell 404 (no such path) from 405 (wrong method)."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Handler, bool]] = []

    def add(self, method: str, pattern: str, handler: Handler, *, auth: bool = True) -> None:
        regex = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$"
        self._routes.append((method, re.compile(regex), handler, auth))

    def resolve(self, method: str, path: str) -> tuple[Handler, dict[str, str], bool]:
        allowed = set()
        for route_method, regex, handler, auth in self._routes:
            match = regex.match(path)
            if match is None:
                continue
            if route_method == method:
                return handler, {k: unquote(v) for k, v in match.groupdict().items()}, auth
            allowed.add(route_method)
        if allowed:
            raise ApiError(
                HTTPStatus.METHOD_NOT_ALLOWED,
                f"{method} is not allowed here",
                {"Allow": ", ".join(sorted(allowed))},
            )
        raise ApiError(HTTPStatus.NOT_FOUND, f"no route for {path}")


def build_router(app: "FocusLockApp") -> Router:
    router = Router()
    p = API_PREFIX

    def health(_: Request) -> Response:
        return 200, {"status": "ok", "version": __version__}

    def status(_: Request) -> Response:
        return 200, app.status()

    def get_blocklist(_: Request) -> Response:
        return 200, {"domains": app.blocklist(), "enforcing": app.enforcing()}

    def add_domain(req: Request) -> Response:
        domain = req.field("domain", str)
        include_www = req.field("include_www", bool, required=False, default=True)
        added = app.add_domains([domain], include_www=include_www)
        return 201, {"added": added, "domains": app.blocklist()}

    def remove_domain(req: Request) -> Response:
        include_www = req.query_bool("include_www", False)
        removed = app.remove_domains(
            [req.params["domain"]], include_www=include_www, password=req.password
        )
        if not removed:
            raise ApiError(HTTPStatus.NOT_FOUND, "that domain is not in the blocklist")
        return 204, None

    def start_session(req: Request) -> Response:
        mode = req.field("mode", str)
        minutes = req.field("minutes", int, required=False)
        goal = req.field("goal", str, required=False)
        if minutes is not None and not 1 <= minutes <= 600:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "minutes must be between 1 and 600")
        return 201, app.start_session(mode, minutes=minutes, goal=goal)

    actions: dict[str, Callable[[Request], dict[str, Any]]] = {
        "stop": lambda req: app.stop(req.password),
        "complete": lambda req: app.complete_goal(),
        "break": lambda req: app.take_break(),
        "skip-break": lambda req: app.skip_break(),
    }

    def session_action(req: Request) -> Response:
        action = actions.get(req.params["action"])
        if action is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"unknown action: {req.params['action']}")
        return 200, action(req)

    def list_cheats(req: Request) -> Response:
        return 200, {"cheats": app.cheats(req.query_int("limit", 50, 1, 500))}

    def list_sessions(req: Request) -> Response:
        limit = req.query_int("limit", 20, 1, 200)
        offset = req.query_int("offset", 0, 0, 1_000_000)
        return 200, {"sessions": app.sessions(limit, offset), "limit": limit, "offset": offset}

    def get_rewards(_: Request) -> Response:
        return 200, app.rewards()

    def spend(req: Request) -> Response:
        minutes = req.field("minutes", int)
        if not 1 <= minutes <= 1440:
            raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "minutes must be between 1 and 1440")
        return 200, app.spend_reward(minutes)

    def stats(req: Request) -> Response:
        return 200, app.stats(req.query_int("days", 7, 1, 365))

    router.add("GET", f"{p}/health", health, auth=False)
    router.add("GET", f"{p}/status", status)
    router.add("GET", f"{p}/blocklist", get_blocklist)
    router.add("POST", f"{p}/blocklist", add_domain)
    router.add("DELETE", f"{p}/blocklist/{{domain}}", remove_domain)
    router.add("GET", f"{p}/sessions", list_sessions)
    router.add("POST", f"{p}/sessions", start_session)
    router.add("POST", f"{p}/sessions/current/{{action}}", session_action)
    router.add("GET", f"{p}/rewards", get_rewards)
    router.add("POST", f"{p}/rewards/spend", spend)
    router.add("GET", f"{p}/stats", stats)
    router.add("GET", f"{p}/cheats", list_cheats)
    return router


class _RequestHandler(BaseHTTPRequestHandler):
    server: "ApiHTTPServer"
    server_version = f"FocusLock/{__version__}"

    def do_GET(self) -> None:
        self._dispatch()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), format % args)

    def _dispatch(self) -> None:
        try:
            status, payload = self._handle()
            self._send(status, payload)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message}, exc.headers)
        except InvalidDomainError as exc:
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except PasswordError as exc:
            self._send(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except (InvalidTransitionError, SessionConflictError, RewardError) as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (FocusLockError, ValueError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError:
            message = "cannot write the hosts file; run FocusLock as administrator"
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": message})
        except Exception:
            logger.exception("unhandled API error")
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

    def _handle(self) -> Response:
        url = urlsplit(self.path)
        self._check_host()
        path = url.path.rstrip("/") or "/"
        handler, params, auth = self.server.router.resolve(self.command, path)
        if auth:
            self._check_token()
        body = self._read_body()
        password = self.headers.get(PASSWORD_HEADER)
        query = parse_qs(url.query)
        return handler(Request(self.command, path, params, query, body, password))

    def _check_host(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
        if name not in self.server.allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "host not allowed")

    def _check_token(self) -> None:
        scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
        valid = scheme.lower() == "bearer" and hmac.compare_digest(
            token.strip().encode(), self.server.token.encode()
        )
        if not valid:
            raise ApiError(
                HTTPStatus.UNAUTHORIZED,
                "missing or invalid API token",
                {"WWW-Authenticate": "Bearer"},
            )

    def _read_body(self) -> Any:
        header = self.headers.get("Content-Length")
        if not header:
            return None
        try:
            length = int(header)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from None
        if length < 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
        if length == 0:
            return None
        raw = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send Content-Type: application/json")
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}") from None

    def _send(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = b""
        if status != HTTPStatus.NO_CONTENT and payload is not None:
            body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)


class ApiHTTPServer(ThreadingHTTPServer):
    """One thread per request; daemon threads don't block shutdown."""

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], router: Router, token: str, allowed_hosts: set[str]
    ) -> None:
        super().__init__(address, _RequestHandler)
        self.router = router
        self.token = token
        self.allowed_hosts = allowed_hosts


def load_or_create_token(home: str | Path) -> str:
    path = Path(home) / TOKEN_FILE
    if path.exists():
        token = path.read_text(encoding="ascii").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
    return token


class ApiServer:
    def __init__(
        self,
        app: "FocusLockApp",
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str | None = None,
    ) -> None:
        self.token = token or load_or_create_token(app.home)
        allowed = set(LOOPBACK_HOSTS) | {host.lower()}
        self.httpd = ApiHTTPServer((host, port), build_router(app), self.token, allowed)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.httpd.server_address[0]}:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="focuslock-api",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "ApiServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
