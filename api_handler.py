"""
REST API handler for 窮遊.

POST /plan          — build a new travel plan
POST /update        — incrementally update an existing plan
GET  /health        — health check

All responses are JSON. Input is parsed from the request body as JSON.
"""

from __future__ import annotations

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable
from urllib.parse import urlparse

from core.shared_schemas import (
    Status,
    TravelInput,
    TravelOutput,
)
from app.validator import validate
from core.routing_engine import RoutingEngine

# ─── Singleton engine ──────────────────────────────────────────────────────────
_engine = RoutingEngine()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _parse_body(body: bytes) -> TravelInput | dict:
    try:
        d = json.loads(body.decode("utf-8"))
    except Exception as e:
        return {"error": f"Invalid JSON: {e}"}

    try:
        return TravelInput.from_dict(d)
    except Exception as e:
        return {"error": f"Failed to build TravelInput: {e}"}


def _output(resp: TravelOutput) -> tuple[dict, int]:
    return resp.to_dict(), 200 if resp.status == Status.SUCCESS else 400


# ─── Handler ───────────────────────────────────────────────────────────────────

class _RequestHandler(BaseHTTPRequestHandler):

    # ── routing ───────────────────────────────────────────────────────────────

    def dispatch(self) -> None:
        parsed = urlparse(self.path)
        key = (self.command.upper(), parsed.path)

        handler = _ROUTES.get(key, _RequestHandler._not_found)
        self._respond(handler)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    # ── endpoints ─────────────────────────────────────────────────────────────

    def _health(self):
        return {"status": "ok", "service": "qiongyou-api"}, 200

    def _plan(self):
        print(">>> /plan HIT")

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        print("RAW BODY:", body)

        parsed = _parse_body(body)
        if isinstance(parsed, dict) and "error" in parsed:
            return parsed, 400

        inp = parsed

        print(">>> VALIDATION START")
        vresult = validate(inp)
        if vresult.status == Status.FAILED:
            print(">>> VALIDATION FAILED")
            return _output(vresult)

        print(">>> ROUTING START")
        output = _engine.plan(inp)
        print(">>> ROUTING DONE")

        return _output(output)

    def _update(self):
        print(">>> /update HIT")

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        parsed = _parse_body(body)
        if isinstance(parsed, dict) and "error" in parsed:
            return parsed, 400

        base_dict = parsed.get("base")
        input_dict = parsed.get("input")

        if not base_dict or not input_dict:
            return {"error": "Missing 'base' and/or 'input'"}, 400

        try:
            base = TravelOutput.from_dict(base_dict)
            inp = TravelInput.from_dict(input_dict)
        except Exception as e:
            return {"error": str(e)}, 400

        vresult = validate(inp)
        if vresult.status == Status.FAILED:
            return _output(vresult)

        updated = _engine.update(base, inp)
        return _output(updated)

    def _not_found(self):
        return {"error": f"Route {self.command} {self.path} not found"}, 404

    # ── response ──────────────────────────────────────────────────────────────

    def _respond(self, handler: Callable):
        try:
            body_dict, status = handler(self)
        except Exception as e:
            print("!!! SERVER ERROR:", e)
            body_dict, status = {"error": str(e)}, 500

        raw = json.dumps(body_dict, ensure_ascii=False, indent=2)
        encoded = raw.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        self.wfile.write(encoded)


# ─── Route table ───────────────────────────────────────────────────────────────

_ROUTES = {
    ("GET", "/health"): _RequestHandler._health,
    ("POST", "/plan"): _RequestHandler._plan,
    ("POST", "/update"): _RequestHandler._update,
}


# ─── Server bootstrap ──────────────────────────────────────────────────────────

def run(port: int = 8080):
    server = HTTPServer(("0.0.0.0", port), _RequestHandler)
    print(f"窮遊 API listening on http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
