"""Tiny demo login app: password -> OTP or secret question -> session cookie.

Run:  python demo/server.py
Open: http://127.0.0.1:8765/login

Demo creds:
  user: demo@example.com
  pass: demo-pass
  otp:  123456
  security answer for "What city were you born in?": springfield
"""

from __future__ import annotations

import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

HOST, PORT = "127.0.0.1", 8765
USER, PASS = "demo@example.com", "demo-pass"
OTP = "123456"
QUESTION = "What city were you born in?"
ANSWER = "springfield"

# pending_token -> {"user": str, "step": "otp"|"secret"}
PENDING: dict[str, dict] = {}
# session_id -> user
SESSIONS: dict[str, str] = {}


LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Demo Login</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:360px;margin:4rem auto}
  label{display:block;margin:.75rem 0 .25rem}
  input,button{width:100%;padding:.5rem;box-sizing:border-box}
  .err{color:#b00;margin-top:1rem}
</style></head><body>
<h1>Demo Login</h1>
<form method="post" action="/login">
  <label>Email</label>
  <input name="username" type="email" autocomplete="username" required>
  <label>Password</label>
  <input name="password" type="password" autocomplete="current-password" required>
  <p style="margin-top:1rem"><button type="submit">Sign in</button></p>
</form>
{err}
</body></html>"""

OTP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Enter OTP</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:360px;margin:4rem auto}
  label{display:block;margin:.75rem 0 .25rem}
  input,button{width:100%;padding:.5rem;box-sizing:border-box}
  .err{color:#b00;margin-top:1rem}
</style></head><body>
<h1>Two-factor</h1>
<p>Enter the one-time code</p>
<form method="post" action="/otp">
  <input type="hidden" name="token" value="{token}">
  <label>OTP</label>
  <input name="otp" id="otp" autocomplete="one-time-code" inputmode="numeric" required>
  <p style="margin-top:1rem"><button type="submit">Verify</button></p>
</form>
{err}
</body></html>"""

SECRET_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Security Question</title>
<style>
  body{font-family:system-ui,sans-serif;max-width:360px;margin:4rem auto}
  label{display:block;margin:.75rem 0 .25rem}
  input,button{width:100%;padding:.5rem;box-sizing:border-box}
  .err{color:#b00;margin-top:1rem}
</style></head><body>
<h1>Security question</h1>
<p>{question}</p>
<form method="post" action="/secret">
  <input type="hidden" name="token" value="{token}">
  <label>Answer</label>
  <input name="answer" id="answer" required>
  <p style="margin-top:1rem"><button type="submit">Continue</button></p>
</form>
{err}
</body></html>"""

DASH_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Dashboard</title>
<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:4rem auto}</style>
</head><body>
<h1>Dashboard</h1>
<p>Signed in as <strong>{user}</strong>.</p>
<p><a href="/logout">Sign out</a></p>
</body></html>"""


def _err(msg: str) -> str:
    return f'<p class="err">{msg}</p>' if msg else ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter; never log bodies
        print("%s - %s" % (self.address_string(), fmt % args))

    def _cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part.split("=", 1)[1]
        return None

    def _set_session(self, sid: str) -> None:
        self.send_header("Set-Cookie", f"demo_session={sid}; Path=/; HttpOnly; SameSite=Lax")

    def _html(self, code: int, body: str, set_sid: str | None = None) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if set_sid:
            self._set_session(set_sid)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc: str, set_sid: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", loc)
        if set_sid:
            self._set_session(set_sid)
        self.end_headers()

    def _read_form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def do_GET(self) -> None:
        if self.path in ("/", "/login"):
            sid = self._cookie("demo_session")
            if sid and sid in SESSIONS:
                return self._redirect("/dashboard")
            return self._html(200, LOGIN_HTML.format(err=""))
        if self.path == "/dashboard":
            sid = self._cookie("demo_session")
            user = SESSIONS.get(sid or "")
            if not user:
                return self._redirect("/login")
            return self._html(200, DASH_HTML.format(user=user))
        if self.path == "/logout":
            sid = self._cookie("demo_session")
            if sid:
                SESSIONS.pop(sid, None)
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "demo_session=; Path=/; Max-Age=0")
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self) -> None:
        form = self._read_form()
        if self.path == "/login":
            user, password = form.get("username", ""), form.get("password", "")
            if user != USER or password != PASS:
                return self._html(401, LOGIN_HTML.format(err=_err("Invalid username or password")))
            token = secrets.token_urlsafe(16)
            # Alternate challenge: odd token length -> OTP, even -> secret (both work; we pick OTP first).
            PENDING[token] = {"user": user, "step": "otp"}
            return self._html(200, OTP_HTML.format(token=token, err=""))

        if self.path == "/otp":
            token, otp = form.get("token", ""), form.get("otp", "").strip()
            pend = PENDING.get(token)
            if not pend or pend.get("step") != "otp":
                return self._html(400, LOGIN_HTML.format(err=_err("Session expired; sign in again")))
            if otp != OTP:
                return self._html(401, OTP_HTML.format(token=token, err=_err("Incorrect code")))
            # Second factor: security question
            pend["step"] = "secret"
            return self._html(200, SECRET_HTML.format(token=token, question=QUESTION, err=""))

        if self.path == "/secret":
            token, answer = form.get("token", ""), form.get("answer", "").strip().lower()
            pend = PENDING.get(token)
            if not pend or pend.get("step") != "secret":
                return self._html(400, LOGIN_HTML.format(err=_err("Session expired; sign in again")))
            if answer != ANSWER:
                return self._html(
                    401,
                    SECRET_HTML.format(token=token, question=QUESTION, err=_err("Incorrect answer")),
                )
            PENDING.pop(token, None)
            sid = secrets.token_urlsafe(24)
            SESSIONS[sid] = pend["user"]
            return self._redirect("/dashboard", set_sid=sid)

        self.send_error(404)


if __name__ == "__main__":
    print(f"Demo login at http://{HOST}:{PORT}/login")
    print(f"  user={USER}  pass={PASS}  otp={OTP}  answer={ANSWER}")
    HTTPServer((HOST, PORT), Handler).serve_forever()
