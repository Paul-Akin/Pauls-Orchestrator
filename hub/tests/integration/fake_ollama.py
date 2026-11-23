"""Minimal stand-in for the Ollama HTTP API (``/api/tags``, ``/api/chat``, ``/api/embed``).

Speaks Ollama's real wire format (NDJSON streaming with ``prompt_eval_count`` /
``eval_count`` in the final line), so the *real* LiteLLM ``ollama_chat/`` code
path is exercised end to end without installing Ollama or a model.

Run standalone for manual testing::

    uv run python hub/tests/integration/fake_ollama.py --port 11434
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

MODELS = ["llama3.2:latest", "llama3.2"]
REPLY = "Local model reply: this text never left the machine."


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-ollama"
    requests_seen: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, *_: Any) -> None:
        pass

    def _json(self, code: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/tags":
            self._json(200, {"models": [{"name": m, "model": m} for m in MODELS]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Handler.requests_seen.append({"_path": self.path, **body})
        if self.path == "/api/embed":
            inputs = body.get("input", [])
            inputs = [inputs] if isinstance(inputs, str) else inputs
            self._json(200, {"model": body.get("model"), "embeddings": [_embed(t) for t in inputs]})
            return
        if self.path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        model = body.get("model", "")
        if model.split(":")[0] not in {m.split(":")[0] for m in MODELS}:
            self._json(404, {"error": f"model '{model}' not found, try pulling it first"})
            return
        words = REPLY.split(" ")
        if not body.get("stream", True):
            self._json(200, self._final(model, REPLY))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for i, w in enumerate(words):
            chunk = {
                "model": model,
                "created_at": "2026-11-23T00:00:00Z",
                "message": {"role": "assistant", "content": w if i == 0 else " " + w},
                "done": False,
            }
            self.wfile.write(json.dumps(chunk).encode() + b"\n")
            self.wfile.flush()
        self.wfile.write(json.dumps(self._final(model, "")).encode() + b"\n")

    @staticmethod
    def _final(model: str, content: str) -> dict[str, Any]:
        return {
            "model": model,
            "created_at": "2026-11-23T00:00:00Z",
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": "stop",
            "total_duration": 1_000_000,
            "prompt_eval_count": 12,
            "eval_count": len(REPLY.split(" ")),
        }


def _embed(text: str, dim: int = 32) -> list[float]:
    """Deterministic bag-of-words vector: shared words => higher cosine."""
    import hashlib
    import math
    import re

    vec = [0.0] * dim
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(word.rstrip("s").encode(), usedforsecurity=False).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class FakeOllama:
    def __init__(self, port: int = 0) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return _Handler.requests_seen

    def requests_to(self, path: str) -> list[dict[str, Any]]:
        return [r for r in _Handler.requests_seen if r["_path"] == path]

    def __enter__(self) -> FakeOllama:
        _Handler.requests_seen = []
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11434)
    args = parser.parse_args()
    srv = FakeOllama(args.port)
    print(f"fake Ollama on {srv.url}")
    srv.server.serve_forever()
