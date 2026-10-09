"""NY Lease Review — a small server that keeps the OpenRouter key private.

The browser sends lease text here; this server adds the API key and forwards
it to OpenRouter. The key never leaves this machine.

Run:  python3 server.py
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"


def load_env(path):
    """Minimal .env loader so there are no dependencies."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(ROOT / ".env")

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-5.5")
PORT = int(os.environ.get("PORT", "8000"))
# On Render (or any host) listen on all interfaces; locally, only this machine.
HOST = os.environ.get("HOST", "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # optional; blank = open
MAX_REVIEWS_PER_HOUR = int(os.environ.get("MAX_REVIEWS_PER_HOUR", "10"))
MAX_REVIEWS_PER_DAY = int(os.environ.get("MAX_REVIEWS_PER_DAY", "100"))  # site-wide spending cap
MAX_LEASE_CHARS = 80_000

SYSTEM_PROMPT = (ROOT / "prompt.md").read_text()

# Rate limiting so strangers can't drain your OpenRouter credit:
# a per-visitor hourly limit plus a site-wide daily limit.
_hits = defaultdict(deque)
_all_hits = deque()
_lock = threading.Lock()


def _trim(q, now, window):
    while q and now - q[0] > window:
        q.popleft()


def rate_limited(ip):
    """Returns None if allowed, otherwise a message explaining the limit."""
    now = time.time()
    with _lock:
        q = _hits[ip]
        _trim(q, now, 3600)
        _trim(_all_hits, now, 86400)
        if len(_all_hits) >= MAX_REVIEWS_PER_DAY:
            return "The site has reached its daily review limit. Please try again tomorrow."
        if len(q) >= MAX_REVIEWS_PER_HOUR:
            return "You've hit the hourly review limit. Please try again later."
        q.append(now)
        _all_hits.append(now)
    return None


def build_user_message(lease, ctx):
    lines = ["Tenant-provided context:"]
    lines.append(f"- Location: {ctx.get('location') or 'not specified'}")
    lines.append(f"- Rent regulated (stabilized/controlled): {ctx.get('regulated') or 'unknown'}")
    lines.append(f"- Lease type: {ctx.get('leaseType') or 'not specified'}")
    if ctx.get("concerns"):
        lines.append(f"- Tenant's specific concerns: {ctx['concerns'][:1000]}")
    lines.append("\nLease text:\n<lease>\n" + lease + "\n</lease>")
    return "\n".join(lines)


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def call_openrouter(user_message):
    body = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.2,
            "max_tokens": 16000,
            # Keep thinking short so it can't use up the space for the answer.
            "reasoning": {"max_tokens": 2000},
        }
    ).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": PUBLIC_URL,
            "X-Title": "NY Lease Review",
        },
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.load(resp)
    choice = data["choices"][0]
    content = choice["message"].get("content") or ""
    if choice.get("finish_reason") == "length" or not content.strip():
        raise IncompleteResponse(choice.get("finish_reason"))
    return content


class IncompleteResponse(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (PUBLIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._send(200, {"passwordRequired": bool(APP_PASSWORD), "configured": bool(API_KEY)})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/api/review":
            return self._send(404, {"error": "Not found"})
        if not API_KEY:
            return self._send(500, {"error": "Server is missing OPENROUTER_API_KEY. Add it to the .env file and restart."})

        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 2_000_000:
                return self._send(413, {"error": "Request too large."})
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "Invalid request."})

        if APP_PASSWORD and payload.get("password") != APP_PASSWORD:
            return self._send(401, {"error": "Wrong access password."})

        lease = (payload.get("lease") or "").strip()
        if len(lease) < 200:
            return self._send(400, {"error": "That doesn't look like a full lease. Paste or upload the whole document."})
        if len(lease) > MAX_LEASE_CHARS:
            return self._send(400, {"error": f"Lease is too long (over {MAX_LEASE_CHARS:,} characters)."})

        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        limit_msg = rate_limited(ip)
        if limit_msg:
            return self._send(429, {"error": limit_msg})

        try:
            raw = call_openrouter(build_user_message(lease, payload.get("context") or {}))
            result = extract_json(raw)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            self.log_error("OpenRouter error %s: %s", e.code, detail)
            if e.code == 402:
                return self._send(503, {"error": "The review service is out of credit right now. Please try again later."})
            return self._send(502, {"error": f"The AI service returned an error ({e.code}). Check your key, credit and model name."})
        except IncompleteResponse as e:
            self.log_error("Incomplete AI response (finish_reason=%s)", e)
            return self._send(502, {"error": "The review came back incomplete. Please try again."})
        except (urllib.error.URLError, TimeoutError):
            return self._send(504, {"error": "Couldn't reach the AI service. Try again."})
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError):
            return self._send(502, {"error": "The AI response couldn't be read. Try again."})
        except Exception as e:  # never drop the connection; the browser would show "failed to fetch"
            self.log_error("Unexpected error: %r", e)
            return self._send(500, {"error": "Something went wrong on the server. Please try again."})

        self._send(200, result)


if __name__ == "__main__":
    if not API_KEY:
        print("WARNING: OPENROUTER_API_KEY is not set. Copy .env.example to .env and add your key.")
    print(f"NY Lease Review running at {PUBLIC_URL}  (model: {MODEL})", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
