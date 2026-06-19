"""Email signup via magic link (Resend).

Flow:
1. User types /signup email@x.com in DM
2. We generate a random token, store it in magic_link_tokens
3. We send a Resend email with a t.me/trceiobot?start=verify_<token> link
4. User clicks → opens Telegram → taps /start verify_<token>
5. Bot consumes the token, sets users.email + email_verified=true

DEV MODE: if RESEND_API_KEY isn't set, the link is logged to the bot's
output instead of emailed. Lets us test locally without burning Resend quota.
"""
import os
import secrets
import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
APP_URL = os.environ.get("APP_URL", "https://trce.io")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "trceiobot")  # without @

# FROM_EMAIL behavior:
#   - If set in env: use it as-is (e.g. "TRCE <hello@trce.io>" after domain verify)
#   - If NOT set: fall back to Resend's sandbox sender "onboarding@resend.dev"
#     which only delivers to the email tied to your Resend account (good for
#     local testing while DNS records at worldnic.com are still propagating).
_DEFAULT_FROM = "TRCE <hello@trce.io>"
_SANDBOX_FROM = "TRCE <onboarding@resend.dev>"
FROM_EMAIL = os.environ.get("FROM_EMAIL") or _SANDBOX_FROM
USING_SANDBOX = FROM_EMAIL == _SANDBOX_FROM

if RESEND_API_KEY and USING_SANDBOX:
    log.warning("RESEND: using sandbox sender (FROM_EMAIL not set). Magic links only deliver to the email tied to your Resend account. Set FROM_EMAIL=TRCE <hello@trce.io> after verifying trce.io at resend.com/domains.")
elif RESEND_API_KEY:
    log.info("RESEND: sending from %s", FROM_EMAIL)

DEV_MODE = not bool(RESEND_API_KEY)


def _validate_email(email: str) -> Optional[str]:
    """Basic email sanity check. Returns the cleaned email or None."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        return None
    return email


async def start_signup(user_id: str, raw_email: str) -> dict:
    """Begin the magic-link flow for `user_id` with `raw_email`.

    Returns:
        {"success": True, "email": "...", "dev_mode": bool, "link": "..."}
        {"error": "..."}
    """
    from db import create_magic_token

    email = _validate_email(raw_email)
    if not email:
        return {"error": "That doesn't look like an email. Try `/signup you@gmail.com`."}

    token = secrets.token_urlsafe(32)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: create_magic_token(user_id, email, token))
    if not ok:
        return {"error": "Couldn't start signup. Try again."}

    # Magic link: opens Telegram with /start verify_<token>
    # Works on mobile (Telegram app) and desktop (Telegram Web).
    link = f"https://t.me/{BOT_USERNAME}?start=verify_{token}"

    if DEV_MODE:
        log.warning("DEV MODE (no RESEND_API_KEY) — magic link for %s: %s", email, link)
        return {"success": True, "email": email, "dev_mode": True, "link": link}

    # Send via Resend
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": FROM_EMAIL,
                    "to": [email],
                    "subject": "Confirm your TRCE signup",
                    "html": (
                        f'<div style="font-family:-apple-system,sans-serif;max-width:480px;margin:0 auto;padding:24px;">'
                        f'<h2 style="margin:0 0 16px;">Welcome to TRCE</h2>'
                        f'<p>Click below to confirm your email and activate your account:</p>'
                        f'<p style="margin:24px 0;">'
                        f'<a href="{link}" style="background:#0A0A0A;color:#F4F2EC;padding:12px 24px;'
                        f'text-decoration:none;border-radius:6px;display:inline-block;">Confirm Email</a></p>'
                        f'<p style="color:#8A8A85;font-size:13px;">Link expires in 15 minutes. '
                        f'If you didn\'t request this, ignore the email.</p>'
                        f'</div>'
                    ),
                },
            )
            r.raise_for_status()
            return {"success": True, "email": email, "dev_mode": False}
    except httpx.HTTPStatusError as e:
        log.error("Resend HTTP %s for %s", e.response.status_code, email)
        try:
            log.error("Resend response body: %s", e.response.json())
        except Exception:
            log.error("Resend response text: %s", e.response.text[:500])
        return {"error": "Couldn't send the email right now. Try again in a moment, or /help if it keeps failing."}
    except Exception as e:
        log.exception("Resend send failed (network/parse): %s", e)
        return {"error": "Couldn't send the email right now. Try again in a moment, or /help if it keeps failing."}


async def complete_signup(token: str) -> dict:
    """Consume a magic-link token. Returns {"success": True, "email": "..."} or {"error": "..."}."""
    from db import consume_magic_token
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: consume_magic_token(token))
