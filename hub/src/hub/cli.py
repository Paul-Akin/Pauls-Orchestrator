"""``hub`` command-line interface.

``hub serve`` runs the server. Every other command is a thin HTTP client of the
*running* hub, so there is exactly one process that owns the usage database
(and, in stage 2, the memory store), and the CLI starts fast because it never
imports LiteLLM.
"""

from __future__ import annotations

import json
import sys
from typing import Annotated, Any

import httpx
import typer

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Personal AI orchestrator hub.")


def _load() -> tuple[Any, Any]:
    from hub.config import ConfigError, load_config
    from hub.settings import Settings

    settings = Settings()
    try:
        cfg = load_config(settings.config_dir)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    return settings, cfg


def _base_url(settings: Any, cfg: Any) -> str:
    host = settings.host_override or cfg.hub.server.host
    if host in {"0.0.0.0", "::", ""}:  # noqa: S104 - client side: wildcard bind means "connect locally"
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = settings.port_override or cfg.hub.server.port
    return f"http://{host}:{port}"


def _headers(settings: Any) -> dict[str, str]:
    token = settings.hub_api_token
    return {"Authorization": f"Bearer {token.get_secret_value()}"} if token else {}


def _not_running(url: str) -> typer.Exit:
    typer.secho(
        f"hub is not reachable at {url}. Start it with:  uv run hub serve",
        fg=typer.colors.RED,
        err=True,
    )
    return typer.Exit(3)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Override server.host")] = None,
    port: Annotated[int | None, typer.Option(help="Override server.port")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes (dev)")] = False,
) -> None:
    """Run the hub server (web chat at http://127.0.0.1:8765 by default)."""
    import os

    import uvicorn

    from hub.config import ConfigError, check_runtime
    from hub.log import configure_logging

    if host:
        os.environ["HUB_HOST"] = host
    if port:
        os.environ["HUB_PORT"] = str(port)
    settings, cfg = _load()
    try:
        check_runtime(cfg, settings)  # fail fast with a readable message
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    configure_logging(cfg.hub.log_level, cfg.hub.log_format)
    bind_host = settings.host_override or cfg.hub.server.host
    bind_port = settings.port_override or cfg.hub.server.port
    typer.secho(f"hub web chat: http://{bind_host}:{bind_port}/", fg=typer.colors.GREEN, err=True)
    uvicorn.run(
        "hub.api.app:app_factory",
        factory=True,
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_level=cfg.hub.log_level.lower(),
        server_header=False,
    )


@app.command()
def chat(
    question: Annotated[str, typer.Argument(help="Your question. Use '-' to read stdin.")],
    route: Annotated[str, typer.Option("--route", "-r", help="Route name or 'auto'")] = "auto",
    stream: Annotated[bool, typer.Option(help="Stream tokens as they arrive")] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full JSON response")] = False,
    system: Annotated[str | None, typer.Option(help="Optional system prompt")] = None,
) -> None:
    """Ask the running hub a question."""
    settings, cfg = _load()
    url = _base_url(settings, cfg)
    text = sys.stdin.read() if question == "-" else question
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": text}
    ]
    body = {"messages": messages, "route": route, "stream": stream and not as_json}
    try:
        with httpx.Client(
            base_url=url, headers=_headers(settings), timeout=httpx.Timeout(10.0, read=600.0)
        ) as client:
            if body["stream"]:
                _chat_stream(client, body)
            else:
                _chat_once(client, body, as_json)
    except httpx.ConnectError:
        raise _not_running(url) from None


def _chat_once(client: httpx.Client, body: dict[str, Any], as_json: bool) -> None:
    r = client.post("/chat", json=body)
    data = r.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        raise typer.Exit(0 if r.status_code == 200 else 1)
    if r.status_code != 200:
        _print_error(data.get("message") or data.get("detail"), data.get("attempts", []))
        raise typer.Exit(1)
    typer.echo(data["content"])
    _print_badge(data["metadata"])


def _chat_stream(client: httpx.Client, body: dict[str, Any]) -> None:
    with client.stream("POST", "/chat", json=body) as r:
        if r.status_code != 200:
            r.read()
            _print_error(r.json().get("detail", f"HTTP {r.status_code}"), [])
            raise typer.Exit(1)
        event = ""
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "delta":
                    sys.stdout.write(data["text"])
                    sys.stdout.flush()
                elif event == "done":
                    sys.stdout.write("\n")
                    _print_badge(data["metadata"])
                elif event == "error":
                    sys.stdout.write("\n")
                    _print_error(data["message"], data["attempts"])
                    raise typer.Exit(1)


def _print_badge(meta: dict[str, Any]) -> None:
    usage = meta.get("usage")
    tokens = (
        f"{'~' if usage['estimated'] else ''}{usage['input_tokens']} in / "
        f"{usage['output_tokens']} out"
        if usage
        else "tokens n/a"
    )
    cost = meta["cost"]
    if cost["status"] != "ok":
        cost_s = cost["status"]
    else:
        cost_s = "$0" if cost["usd"] == 0 else f"${cost['usd']:.5f}"
    parts = [
        f"{meta['model_alias']} ({meta['provider']}/{meta['model']}, {meta['locality']})",
        f"route {meta['route']} [{meta['route_reason']}]",
        tokens,
        cost_s,
        f"{meta['latency_ms'] / 1000:.1f} s",
    ]
    if meta["fell_back"]:
        tried = ", ".join(f"{a['model_alias']}:{a['outcome']}" for a in meta["attempts"])
        parts.append(f"FELL BACK ({tried})")
    typer.secho("── " + " · ".join(parts), fg=typer.colors.BRIGHT_BLACK, err=True)


def _print_error(message: str | None, attempts: list[dict[str, Any]]) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    for a in attempts:
        typer.secho(
            f"  {a['outcome']:<7} {a['model_alias']} ({a['provider']}/{a['model']})"
            f"{': ' + a['reason'] if a.get('reason') else ''}",
            fg=typer.colors.RED,
            err=True,
        )


@app.command()
def providers() -> None:
    """Show which providers are available and the configured routes."""
    settings, cfg = _load()
    url = _base_url(settings, cfg)
    try:
        r = httpx.get(f"{url}/providers", headers=_headers(settings), timeout=10)
    except httpx.ConnectError:
        raise _not_running(url) from None
    r.raise_for_status()
    data = r.json()
    for p in data["providers"]:
        mark = typer.style("●", fg=typer.colors.GREEN) if p["available"] else "○"
        models = ", ".join(m["alias"] for m in p["models"]) or "-"
        typer.echo(f"{mark} {p['provider']:<10} {p['detail']}  [models: {models}]")
    typer.echo("")
    for rt in data["routes"]:
        chain = " → ".join([rt["primary"], *rt["fallback"]])
        lock = " (local only)" if rt["local_only"] else ""
        typer.echo(f"  {rt['name']:<22} {chain}{lock}")
    for w in data.get("warnings", []):
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW, err=True)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
