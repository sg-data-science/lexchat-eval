"""
A stand-in for AILA for the container smoke test: the endpoints lexchat-eval
calls, with canned answers and no model behind them. Standard library only.

    python stub_aila.py                     # serve on port 8080
    python stub_aila.py --set delay=15      # change a setting of the running stub
    python stub_aila.py --set refuse=1      # answer every question with a spend-cap refusal
    python stub_aila.py --get               # the settings, and answers in progress

Signs in USERNAME "eval" with PASSWORD "smoke-password" only. Every answer
costs $0.05.
"""

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("STUB_PORT", "8080"))
TOKEN = "stub-token"
MODEL = "stub/model"
COST_USD = 0.05
CAP_MESSAGE = (
    "This request has been stopped because your daily spending limit of $20.00 "
    "has been reached. It resets at midnight UTC."
)

settings = {"delay": 0.0, "refuse": 0}
in_progress = 0
lock = threading.Lock()


def audit_event(question: str, chat_mode: str, research_mode: str, refused: bool):
    answer = (
        "Section 1 of the Example Act 2020 applies "
        "(https://www.legislation.gov.uk/ukpga/2020/1/section/1)."
    )
    return {
        "type": "audit",
        "schema_version": 6,
        "request_id": "smoke",
        "chat_mode": chat_mode,
        "research_mode": research_mode,
        "provider": "openrouter",
        "model": MODEL,
        "answer": None if refused else answer,
        "delegations": (
            []
            if refused
            else [
                {
                    "id": "d1",
                    "kind": "delegation",
                    "brief": question,
                    "report": "**Summary Answer (BLUF):**\nSection 1 applies.",
                    "error": None,
                    "tools": [
                        {
                            "id": "t1",
                            "name": "search_legislation",
                            "args": {"query": question[:60]},
                            "raw_result": "Example Act 2020",
                            "final_result": "Example Act 2020",
                            "summarised": False,
                            "api_calls": [],
                        },
                        {
                            "id": "t2",
                            "name": "search_legislation_sections",
                            "args": {"legislation_id": "ukpga/2020/1"},
                            "raw_result": "Section 1: This Act applies.",
                            "final_result": "Section 1: This Act applies.",
                            "summarised": False,
                            "api_calls": [],
                        },
                    ],
                }
            ]
        ),
        "timings": {"total_ms": 1000, "llm_calls": 2, "total_cost_usd": COST_USD},
        "error": CAP_MESSAGE if refused else None,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _signed_in(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        self._json(401, {"detail": "Not authenticated"})
        return False

    def do_GET(self):
        if self.path == "/stub/settings":
            return self._json(200, {**settings, "in_progress": in_progress})
        if not self._signed_in():
            return
        if self.path == "/api/models":
            return self._json(
                200, [{"name": MODEL, "provider": "openrouter", "active": True}]
            )
        if self.path == "/api/system/models":
            return self._json(
                200,
                {
                    "active_provider": "openrouter",
                    "model": MODEL,
                    "summarisation_model": "stub/summariser",
                },
            )
        if self.path == "/api/version":
            return self._json(200, {"build": "v2026.12.0-smoke"})
        self._json(404, {"detail": "Not Found"})

    def do_POST(self):
        if self.path == "/stub/settings":
            settings.update({k: float(v) for k, v in self._body().items()})
            return self._json(200, settings)
        if self.path == "/api/auth/login":
            body = self._body()
            if (body.get("username"), body.get("password")) != (
                "eval",
                "smoke-password",
            ):
                return self._json(401, {"detail": "Invalid username or password"})
            return self._json(
                200,
                {
                    "token": TOKEN,
                    "user": {
                        "username": "eval",
                        "role": "eval",
                        "must_change_password": False,
                    },
                },
            )
        if not self._signed_in():
            return
        if self.path == "/api/system/chat":
            return self._chat(self._body())
        self._json(404, {"detail": "Not Found"})

    def _chat(self, body: dict) -> None:
        global in_progress
        with lock:
            in_progress += 1
        try:
            time.sleep(settings["delay"])
            refused = bool(settings["refuse"])
            question = body["messages"][-1]["content"]
            events = []
            if not refused:
                events.append({"type": "token", "content": "Section 1 applies."})
            events.append(
                audit_event(
                    question, body.get("chat_mode"), body.get("research_mode"), refused
                )
            )
            if refused:
                events.append({"type": "error", "error": CAP_MESSAGE})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        finally:
            with lock:
                in_progress -= 1

    def log_message(self, fmt, *args):
        sys.stderr.write("stub: " + fmt % args + "\n")


def control(argv: list[str]) -> None:
    url = f"http://localhost:{PORT}/stub/settings"
    if argv[0] == "--set":
        body = dict(a.split("=", 1) for a in argv[1:])
        req = urllib.request.Request(
            url, json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
    else:
        req = urllib.request.Request(url)
    print(urllib.request.urlopen(req, timeout=5).read().decode())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        control(sys.argv[1:])
    else:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
