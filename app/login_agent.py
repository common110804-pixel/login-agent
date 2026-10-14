"""Playwright login driver: fill creds, classify next step, export session."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .job_manager import Job, JobManager
from .models import JobStatus

log = logging.getLogger("login_agent")

# Optional per-host overrides. Add selectors for sites you own / have approval for.
SITE_SELECTORS: dict[str, dict[str, str]] = {
    # "login.example.com": {
    #     "user": "#email",
    #     "pass": "#password",
    #     "submit": "button[type=submit]",
    #     "success": "text=Sign out",
    # },
}

USER_SELECTORS = [
    'input[autocomplete="username"]',
    'input[autocomplete="email"]',
    'input[type="email"]',
    'input[name*="user" i]',
    'input[name*="email" i]',
    'input[name*="login" i]',
    'input[id*="user" i]',
    'input[id*="email" i]',
    'input[type="text"]',
]

PASS_SELECTORS = [
    'input[autocomplete="current-password"]',
    'input[type="password"]',
]

SUBMIT_SELECTORS = [
    'button[type="submit"]',
    'input[type="submit"]',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'button:has-text("Login")',
    'button:has-text("Continue")',
]

OTP_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp" i]',
    'input[name*="totp" i]',
    'input[name*="mfa" i]',
    'input[name*="token" i]',
    'input[name*="code" i]',
    'input[id*="otp" i]',
    'input[id*="code" i]',
    'input[inputmode="numeric"]',
]

SECRET_ANSWER_SELECTORS = [
    'input[name*="answer" i]',
    'input[id*="answer" i]',
    'input[name*="secret" i]',
]

SUCCESS_HINTS = [
    'a:has-text("Sign out")',
    'a:has-text("Log out")',
    'button:has-text("Sign out")',
    'button:has-text("Log out")',
    '[data-testid*="logout" i]',
]

ERROR_PATTERNS = re.compile(
    r"(incorrect|invalid|wrong).{0,40}(password|credentials|username|email)|"
    r"login failed|authentication failed|access denied",
    re.I,
)

CAPTCHA_HINTS = re.compile(r"captcha|recaptcha|hcaptcha|cf-turnstile|challenge-platform", re.I)
QUESTION_HINTS = re.compile(
    r"(security question|secret question|mother.?s maiden|first pet|what (is|was|city))",
    re.I,
)


async def _first_visible(page: Page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def fill_credentials(page: Page, username: str, password: str, host: str) -> bool:
    overrides = SITE_SELECTORS.get(host, {})
    user_sel = overrides.get("user")
    pass_sel = overrides.get("pass")
    submit_sel = overrides.get("submit")

    user = page.locator(user_sel).first if user_sel else await _first_visible(page, USER_SELECTORS)
    pwd = page.locator(pass_sel).first if pass_sel else await _first_visible(page, PASS_SELECTORS)
    if not user or not pwd:
        return False

    await user.fill(username)
    await pwd.fill(password)

    submit = page.locator(submit_sel).first if submit_sel else await _first_visible(page, SUBMIT_SELECTORS)
    if submit:
        await submit.click()
    else:
        await pwd.press("Enter")
    await page.wait_for_timeout(1500)
    return True


async def detect_otp_field(page: Page):
    return await _first_visible(page, OTP_SELECTORS)


async def detect_secret_question(page: Page) -> tuple[Optional[Any], Optional[str]]:
    answer = await _first_visible(page, SECRET_ANSWER_SELECTORS)
    if not answer:
        return None, None
    question = None
    try:
        for sel in ["label", "legend", "h1", "h2", "h3", "p", "[role='heading']"]:
            texts = await page.locator(sel).all_inner_texts()
            for t in texts:
                t = t.strip()
                if t and QUESTION_HINTS.search(t):
                    question = t
                    break
            if question:
                break
    except Exception:
        pass
    if not question:
        body = (await page.inner_text("body"))[:2000]
        m = QUESTION_HINTS.search(body)
        question = m.group(0) if m else "Security question (see page)"
    return answer, question


async def looks_like_captcha(page: Page) -> bool:
    html = await page.content()
    if CAPTCHA_HINTS.search(html):
        return True
    for frame in page.frames:
        if CAPTCHA_HINTS.search(frame.url):
            return True
    return False


async def looks_like_error(page: Page) -> bool:
    try:
        text = await page.inner_text("body")
    except Exception:
        return False
    return bool(ERROR_PATTERNS.search(text[:4000]))


async def looks_like_success(page: Page, login_host: str, overrides: dict[str, str]) -> bool:
    if overrides.get("success"):
        loc = page.locator(overrides["success"]).first
        try:
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            pass

    for sel in SUCCESS_HINTS:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            continue

    cookies = await page.context.cookies()
    authish = any(
        re.search(r"session|auth|token|sid|jwt", c.get("name", ""), re.I) for c in cookies
    )
    still_on_login = bool(await page.locator('input[type="password"]').count()) and (
        login_host in urlparse(page.url).netloc
    )
    return authish and not still_on_login


async def classify_after_submit(page: Page, login_host: str) -> tuple[JobStatus, Optional[str]]:
    overrides = SITE_SELECTORS.get(login_host, {})
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(800)

    if await looks_like_captcha(page):
        return JobStatus.FAILED, "CAPTCHA_REQUIRED"

    if await looks_like_success(page, login_host, overrides):
        return JobStatus.SUCCEEDED, None

    otp = await detect_otp_field(page)
    if otp:
        return JobStatus.NEEDS_OTP, "Enter the one-time code"

    answer, question = await detect_secret_question(page)
    if answer and question:
        name = (await answer.get_attribute("name") or "").lower()
        if QUESTION_HINTS.search(question) or "answer" in name:
            return JobStatus.NEEDS_SECRET_ANSWER, question

    if await looks_like_error(page):
        return JobStatus.FAILED, "LOGIN_REJECTED"

    if await page.locator('input[type="password"]').count():
        return JobStatus.FAILED, "STILL_ON_LOGIN"

    if await looks_like_success(page, login_host, overrides):
        return JobStatus.SUCCEEDED, None

    return JobStatus.FAILED, "UNCLASSIFIED_PAGE"


async def _fill_and_submit_field(page: Page, locator, value: str) -> None:
    await locator.fill(value)
    submit = await _first_visible(page, SUBMIT_SELECTORS)
    if submit:
        await submit.click()
    else:
        await locator.press("Enter")
    await page.wait_for_timeout(1500)


def _cookie_expiry_hint(state: dict[str, Any]) -> Optional[str]:
    cookies = state.get("cookies") or []
    expiries = [
        c.get("expires")
        for c in cookies
        if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0
    ]
    if not expiries:
        return None
    return str(int(min(expiries)))


async def run_login_job(job: Job, jobs: JobManager) -> None:
    """Drive one login job to completion. Never logs secrets."""
    host = urlparse(job.url).netloc
    log.info("job %s started host=%s", job.id, host)

    pw = await async_playwright().start()
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(job.url, wait_until="domcontentloaded", timeout=60_000)

        filled = await fill_credentials(page, job.username, job.password, host)
        job.password = ""
        if not filled:
            job.status = JobStatus.FAILED
            job.error = "CREDENTIAL_FIELDS_NOT_FOUND"
            return

        status, prompt = await classify_after_submit(page, host)

        for _ in range(3):
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED
                return

            if status == JobStatus.SUCCEEDED:
                state = await context.storage_state()
                job.storage_state = state
                job.expires_hint = _cookie_expiry_hint(state)
                job.status = JobStatus.SUCCEEDED
                job.prompt = None
                log.info("job %s succeeded", job.id)
                return

            if status == JobStatus.FAILED:
                job.status = JobStatus.FAILED
                job.error = prompt or "FAILED"
                return

            job.status = status
            job.prompt = prompt
            incoming = await jobs.wait_for_input(job, timeout=float(job.timeout_seconds))
            if incoming is None:
                job.status = (
                    JobStatus.TIMED_OUT if not job.cancel_requested else JobStatus.CANCELLED
                )
                job.error = "INPUT_TIMEOUT"
                return

            if status == JobStatus.NEEDS_OTP:
                otp_field = await detect_otp_field(page)
                if not otp_field or "otp" not in incoming:
                    job.status = JobStatus.FAILED
                    job.error = "OTP_FIELD_MISSING"
                    return
                await _fill_and_submit_field(page, otp_field, incoming["otp"])
            elif status == JobStatus.NEEDS_SECRET_ANSWER:
                answer_field, _ = await detect_secret_question(page)
                if not answer_field or "secret_answer" not in incoming:
                    job.status = JobStatus.FAILED
                    job.error = "SECRET_ANSWER_FIELD_MISSING"
                    return
                await _fill_and_submit_field(page, answer_field, incoming["secret_answer"])

            job.status = JobStatus.RUNNING
            job.prompt = None
            status, prompt = await classify_after_submit(page, host)

        job.status = JobStatus.FAILED
        job.error = "TOO_MANY_CHALLENGES"
    except asyncio.CancelledError:
        job.status = JobStatus.CANCELLED
        raise
    except Exception:
        log.exception("job %s failed", job.id)
        job.status = JobStatus.FAILED
        job.error = "INTERNAL_ERROR"
    finally:
        if context:
            await context.close()
        if browser:
            await browser.close()
        await pw.stop()
