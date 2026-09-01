"""Streamable HTTP transport for the MCP server.

Wraps the *existing* low-level `Server` from mcp/server.py — StreamableHTTPSessionManager
accepts it directly, so all seven tool handlers, their schemas, and the dispatch chain
are reused verbatim. There is no FastMCP migration here and no second definition of the
tools that could drift from the stdio one.

archiver-rag deliberately terminates **no TLS and performs no authentication**. Both
belong to whatever layer the operator already trusts — a reverse proxy, a VPN, an SSH
tunnel — and choosing one for them is not this tool's job. Hence the loopback default,
and hence cli.py shouting before it binds anywhere else. Every tool is exposed to whoever
can reach the port: the whole vault is readable, and log_note / move_notes /
cluster_vault can modify it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from archiver_rag.mcp.server import app as mcp_server

DEFAULT_HOST = "127.0.0.1"
# Not the SDK's 8000, which collides with nearly every dev server on a working machine.
DEFAULT_PORT = 8077
DEFAULT_PATH = "/mcp"


def configured_endpoint() -> tuple[str, str, int, str]:
    """`(url, host, port, path)` from config with the module defaults.

    The same resolution `serve` performs at startup, extracted so the CLI's
    start-http flow and `status` describe the daemon by construction instead of
    re-deriving (and drifting from) what it actually listens on. A corrupt
    config degrades to loopback defaults via utils.load_config's `{}` contract.
    """
    from archiver_rag.utils import load_config

    cfg = load_config()
    host = cfg.get("http_host", DEFAULT_HOST)
    try:
        port = int(cfg.get("http_port", DEFAULT_PORT))
    except (TypeError, ValueError):
        # A malformed http_port degrades to the default, same as load_config's `{}`
        # contract for a corrupt/missing file — a bind address must never be garbage.
        port = DEFAULT_PORT
    path = cfg.get("http_path", DEFAULT_PATH)
    return f"http://{host}:{port}{path}", host, port, path


def _security_settings(
    allowed_hosts: list[str] | None,
) -> TransportSecuritySettings | None:
    """DNS-rebinding protection, but only once the operator has said what to allow.

    The SDK's two defaults pull in opposite directions and the gap between them is a
    trap: passing None disables protection entirely (backwards compatibility), while
    constructing TransportSecuritySettings with protection enabled and an empty
    allowed_hosts rejects *every* request — including one arriving at the perfectly
    legitimate hostname a proxy in front of it serves. So protection turns on only when
    there is an actual allowlist to enforce.
    """
    if not allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts),
        # A browser Origin is never how an MCP client reaches this; allowing the same
        # names keeps a browser-based client working without widening host checks.
        allowed_origins=[f"https://{h}" for h in allowed_hosts]
        + [f"http://{h}" for h in allowed_hosts],
    )


def build_app(
    *,
    stateless: bool = True,
    json_response: bool = False,
    allowed_hosts: list[str] | None = None,
    path: str = DEFAULT_PATH,
) -> Starlette:
    """ASGI app serving MCP over streamable HTTP at `path`.

    Stateless + SSE responses by default. The session is still per-request — no
    affinity, no reconnect handling — but in-flight notifications
    (notifications/message, notifications/progress emitted while a tool runs) must
    stream to the client, and JSON-response mode silently discards them: the SDK's
    POST loop consumes every non-response message at debug level and returns only the
    final result (verified in mcp/server/streamable_http.py::_handle_post_request).
    SSE responses — the SDK's original streamable-HTTP behavior — deliver the
    notifications inside the POST response body, before the final result frame.

    A fresh session manager is built per call because the SDK documents it as single-use
    — it cannot be restarted after its `run()` context exits.
    """
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        stateless=stateless,
        json_response=json_response,
        security_settings=_security_settings(allowed_hosts),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    # Route with a callable *object*, not Mount and not a plain function. Mount("/mcp")
    # only matches "/mcp/…", so a client POSTing the bare "/mcp" it was configured with
    # gets a 307 redirect instead of a response. And Starlette's Route treats a function
    # endpoint as `func(request) -> response`; only a non-function callable is used as a
    # raw ASGI app, which is what the session manager needs.
    return Starlette(
        routes=[
            Route(path, _Handler(session_manager), methods=["GET", "POST", "DELETE"])
        ],
        lifespan=lifespan,
    )


class _Handler:
    """ASGI callable delegating to the session manager (see build_app for why a class)."""

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self._session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._session_manager.handle_request(scope, receive, send)


def serve_http(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    stateless: bool = True,
    allowed_hosts: list[str] | None = None,
) -> None:
    """Blocking uvicorn run. Imported lazily by cli.py so stdio never pays for it."""
    import uvicorn

    # The bind address always joins the allowlist. Without this, naming any --allowed-host
    # makes the server unreachable at its own 127.0.0.1:8077 — measured: a
    # request with the default Host comes back 421 Misdirected Request. Adding a literal
    # address costs nothing defensively, since DNS rebinding works by pointing a
    # *hostname* at loopback, and such a request carries the attacker's hostname in Host,
    # not an IP.
    effective = list(allowed_hosts or [])
    if effective:
        effective += [host, f"{host}:{port}"]

    uvicorn.run(
        build_app(stateless=stateless, allowed_hosts=effective, path=path),
        host=host,
        port=port,
        log_level="info",
    )
