# Login Agent (Playwright + FastAPI)

Starter service that logs into a web app in a headless browser, pauses for OTP or security questions, then returns a Playwright `storage_state` session (cookies + localStorage).

Use only on apps you own or have written approval to automate.

## Setup

```bash
cd login-agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open docs at http://127.0.0.1:8000/docs

## API flow

### 1. Start a job

```bash
curl -s -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/login",
    "username": "you@example.com",
    "password": "REPLACE_ME",
    "timeout_seconds": 180
  }'
```

Response:

```json
{"job_id":"…","status":"RUNNING"}
```

### 2. Poll status

```bash
curl -s http://127.0.0.1:8000/jobs/JOB_ID
```

Statuses: `RUNNING` | `NEEDS_OTP` | `NEEDS_SECRET_ANSWER` | `SUCCEEDED` | `FAILED` | `TIMED_OUT` | `CANCELLED`

When waiting, `prompt` holds the OTP hint or question text.

### 3. Submit OTP or secret answer

```bash
# OTP
curl -s -X POST http://127.0.0.1:8000/jobs/JOB_ID/input \
  -H 'Content-Type: application/json' \
  -d '{"otp":"123456"}'

# Security question
curl -s -X POST http://127.0.0.1:8000/jobs/JOB_ID/input \
  -H 'Content-Type: application/json' \
  -d '{"secret_answer":"blue"}'
```

### 4. Read the session on success

```bash
curl -s http://127.0.0.1:8000/jobs/JOB_ID
```

On `SUCCEEDED`, `storage_state` is a Playwright storage state object. Reuse it later with:

```python
context = await browser.new_context(storage_state=storage_state)
```

### 5. Cancel / cleanup

```bash
curl -s -X DELETE http://127.0.0.1:8000/jobs/JOB_ID
```

Jobs are also reaped ~10 minutes after creation.

## Project layout

```
login-agent/
  app/
    main.py           # FastAPI routes
    models.py         # request/response models
    job_manager.py    # in-memory jobs + input wait
    login_agent.py    # Playwright fill / classify / export
  requirements.txt
  .env.example
```

## Extending for your sites

Add host-specific selectors in `SITE_SELECTORS` inside `app/login_agent.py`:

```python
SITE_SELECTORS = {
    "login.yourcompany.com": {
        "user": "#email",
        "pass": "#password",
        "submit": "button[type=submit]",
        "success": "text=Sign out",
    },
}
```

## Production harderning (do these before real use)

1. **Vault, not request body** — accept only `vault_ref`, resolve secrets server-side (1Password / Vault / AWS SM).
2. **Encrypt `storage_state`** in transit (TLS + app-level) and at rest; treat it like a password.
3. **Do not log** passwords, OTPs, answers, or full cookie jars.
4. **Isolate** each job (fresh context; ideally one container / network namespace per tenant).
5. **Whitelist egress IPs** with the app owners; automated login often trips fraud detection.
6. **Authn the API** (mTLS or tokens) so random clients cannot start login jobs.
7. **Human for captchas / passkeys** — this starter fails with `CAPTCHA_REQUIRED` instead of bypassing.

## Limitations of this starter

- Heuristic selectors will miss unusual login UIs; prefer `SITE_SELECTORS`.
- Success detection is best-effort (logout control / auth-ish cookies).
- In-memory job store only (no Redis); single-process.
- No vault integration yet (`vault_ref` is a placeholder).
- Captchas and passkeys are not automated.

## Practice with the demo login page

Terminal 1 — demo site (password → OTP → security question → dashboard):

```bash
python demo/server.py
# http://127.0.0.1:8765/login
# user: demo@example.com
# pass: demo-pass
# otp:  123456
# answer: springfield
```

Terminal 2 — login agent:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then:

```bash
JOB=$(curl -s -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:8765/login","username":"demo@example.com","password":"demo-pass"}')
echo "$JOB"
# poll GET /jobs/{id} → when NEEDS_OTP:
curl -s -X POST http://127.0.0.1:8000/jobs/JOB_ID/input -H 'Content-Type: application/json' -d '{"otp":"123456"}'
# when NEEDS_SECRET_ANSWER:
curl -s -X POST http://127.0.0.1:8000/jobs/JOB_ID/input -H 'Content-Type: application/json' -d '{"secret_answer":"springfield"}'
# GET again for storage_state
```

