"""Stripe billing service for TRCE.

One source of truth for Stripe API calls (Checkout Sessions + Webhook
verification). Uses inline `price_data` so we don't need pre-created
Stripe Products/Prices in the dashboard -- the Checkout Session
description still surfaces 'TRCE Pro -- Monthly' / 'TRCE Pro -- Annual'
on invoices, which is enough for the 'separate invoice per product'
requirement.

Pricing constants live here (not in DB) because:
  - Stripe is the source of truth for amounts
  - Changing them means editing one Python file + redeploying
  - Test vs live amounts are gated by which key is in env

Env vars consumed:
  STRIPE_SECRET_KEY        -- required. Test (sk_test_) or live (sk_live_).
  STRIPE_WEBHOOK_SECRET    -- required (web side). Starts with whsec_.
  STRIPE_PRICE_MONTHLY     -- optional. Stripe Price ID (price_...) to use
                             instead of inline price_data.
  STRIPE_PRICE_ANNUAL      -- optional. Same, for annual.
  STRIPE_SUCCESS_URL       -- defaults to https://trce.io/dashboard?upgraded=1
  STRIPE_CANCEL_URL        -- defaults to https://trce.io/dashboard?canceled=1
"""
from __future__ import annotations
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Lazy import -- stripe SDK loaded only when this module is used.
# Lets the bot boot on Railway even if STRIPE_SECRET_KEY isn't set yet
# (returns a friendly error from create_checkout_session instead of crashing).
_stripe = None


def _get_stripe():
    global _stripe
    if _stripe is None:
        import stripe  # imported here so missing deps don't break unrelated code paths
        key = os.environ.get("STRIPE_SECRET_KEY")
        if not key:
            raise RuntimeError(
                "STRIPE_SECRET_KEY is not set. Add it to Railway env vars "
                "(test key starts with sk_test_, live key with sk_live_)."
            )
        stripe.api_key = key
        _stripe = stripe
    return _stripe


# Plan -> (amount in cents, interval). Used when no pre-created
# Price ID is supplied. Change amounts here when prices move.
_INLINE_PRICES = {
    "monthly": {
        "amount": 999,
        "interval": "month",
        "product_name": "TRCE Pro -- Monthly",
        "description": "Unlimited contacts, web dashboard, paper card OCR, follow-up drafts, batch mode, sendContact viral share.",
    },
    "annual": {
        "amount": 9900,
        "interval": "year",
        "product_name": "TRCE Pro -- Annual",
        "description": "Unlimited contacts, web dashboard, paper card OCR, follow-up drafts, batch mode, sendContact viral share. Save $20 vs monthly.",
    },
}

DEFAULT_SUCCESS_URL = os.environ.get(
    "STRIPE_SUCCESS_URL",
    "https://trce.io/dashboard?upgraded=1",
)
DEFAULT_CANCEL_URL = os.environ.get(
    "STRIPE_CANCEL_URL",
    "https://trce.io/dashboard?canceled=1",
)


def is_configured() -> bool:
    """True if STRIPE_SECRET_KEY is set. Webhook side checks separately."""
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def create_checkout_session(
    user_id: str,
    interval: str = "monthly",
    customer_email: Optional[str] = None,
) -> str:
    """Create a Stripe Checkout Session and return the URL.

    Args:
        user_id: Internal TRCE users.id UUID. Stored in
                 `client_reference_id` so the webhook knows which user paid.
        interval: 'monthly' | 'annual'.
        customer_email: optional. Pre-fills Stripe Checkout. We pass the user's
                        email if we know it so the receipt goes to the right place.

    Returns:
        The Checkout Session URL (e.g. https://checkout.stripe.com/c/pay/...).

    Raises:
        RuntimeError if STRIPE_SECRET_KEY is missing.
        KeyError if interval is not 'monthly' or 'annual'.
        stripe.error.StripeError on API failure.
    """
    if interval not in _INLINE_PRICES:
        raise KeyError("Unknown interval {!r}. Use 'monthly' or 'annual'.".format(interval))

    stripe = _get_stripe()
    price_def = _INLINE_PRICES[interval]

    # Prefer a pre-created Price ID (env var) over inline price_data.
    # If both are missing, fall back to inline. This lets TRCE start without
    # any Stripe Dashboard setup AND lets ops manage pricing in Stripe later.
    env_price_key = os.environ.get("STRIPE_PRICE_" + interval.upper())
    if env_price_key:
        line_item = {"price": env_price_key, "quantity": 1}
    else:
        line_item = {
            "quantity": 1,
            "price_data": {
                "currency": "usd",
                "unit_amount": price_def["amount"],
                "recurring": {"interval": price_def["interval"]},
                "product_data": {
                    "name": price_def["product_name"],
                    "description": price_def["description"],
                },
            },
        }

    params = {
        "mode": "subscription",
        "line_items": [line_item],
        "success_url": DEFAULT_SUCCESS_URL,
        "cancel_url": DEFAULT_CANCEL_URL,
        "client_reference_id": user_id,
        "allow_promotion_codes": True,
    }
    if customer_email:
        params["customer_email"] = customer_email

    session = stripe.checkout.Session.create(**params)
    log.info(
        "Stripe checkout created for user=%s interval=%s session=%s",
        user_id, interval, session.id,
    )
    return str(session.url)


def verify_webhook(payload: bytes, signature: str):
    """Verify a Stripe webhook signature and return the Event object.

    Raises stripe.error.SignatureVerificationError on bad sig.
    Raises RuntimeError if STRIPE_WEBHOOK_SECRET is missing.
    """
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError(
            "STRIPE_WEBHOOK_SECRET is not set. Add it to Railway env vars."
        )
    stripe = _get_stripe()
    return stripe.Webhook.construct_event(payload, signature, secret)


def create_billing_portal_session(customer_id: str, return_url: str) -> str:
    """Return a Stripe-hosted URL where the user can manage / cancel their sub.

    The Stripe Customer Portal must be enabled in the dashboard
    (Settings -> Billing -> Customer portal -> Activate). If not enabled,
    Stripe returns an error and we surface it to the caller.
    """
    stripe = _get_stripe()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return str(session.url)