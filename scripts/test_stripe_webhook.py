"""Smoke test: simulate a Stripe webhook hitting the live site.

Use this AFTER running migration 009 in Supabase. Steps:
  1. Run migration 009 in Supabase SQL Editor (adds stripe_customer_id etc.)
  2. Log into trce.io/dashboard as your test user, copy user_id from /dashboard
  3. Run: python3 scripts/test_stripe_webhook.py <user_id>
  4. Check that users.plan changed to 'pro' in Supabase

We construct a fake checkout.session.completed event, sign it with the
live STRIPE_WEBHOOK_SECRET, and POST it to https://trce.io/stripe/webhook.
"""
import os
import sys
import json
import time
import hmac
import hashlib
import requests

WEBHOOK_URL = "https://trce.io/stripe/webhook"
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    print("ERROR: STRIPE_WEBHOOK_SECRET not set in env. Load it from .secrets/stripe.env")
    sys.exit(1)

user_id = sys.argv[1] if len(sys.argv) > 1 else None
if not user_id:
    print("Usage: python3 scripts/test_stripe_webhook.py <user_id>")
    print("Get user_id from Supabase: SELECT id FROM users WHERE email = 'your@email.com';")
    sys.exit(1)

# Construct a fake checkout.session.completed event
event = {
    "id": f"evt_test_{int(time.time())}",
    "object": "event",
    "type": "checkout.session.completed",
    "data": {
        "object": {
            "id": f"cs_test_{int(time.time())}",
            "object": "checkout.session",
            "client_reference_id": user_id,
            "customer": "cus_test_fake_customer_id",
            "subscription": "sub_test_fake_subscription_id",
            "mode": "subscription",
            "payment_status": "paid",
            "status": "complete",
        }
    },
    "created": int(time.time()),
    "livemode": False,
    "api_version": "2026-05-27.dahlia",
}

payload = json.dumps(event, separators=(",", ":"))
timestamp = int(time.time())

# Stripe signature: t=<ts>,v1=<hmac of "ts.payload">
signed_payload = f"{timestamp}.{payload}"
signature = hmac.new(
    WEBHOOK_SECRET.encode(),
    signed_payload.encode(),
    hashlib.sha256,
).hexdigest()

sig_header = f"t={timestamp},v1={signature}"

print(f"POSTing fake event to {WEBHOOK_URL}")
print(f"  user_id (client_reference_id): {user_id}")
print(f"  signature: {sig_header[:50]}...")

r = requests.post(WEBHOOK_URL, data=payload, headers={
    "Content-Type": "application/json",
    "Stripe-Signature": sig_header,
})

print(f"\nResponse: {r.status_code}")
print(f"Body: {r.text}")

if r.status_code == 200:
    print("\nWebhook accepted. Check Supabase: users.plan should now be 'pro'")
    print("  SELECT email, plan, stripe_customer_id, stripe_subscription_id, subscription_status FROM users WHERE id = '" + user_id + "';")
else:
    print("\nFAILED. Check Railway logs for the recallbiz service.")
    sys.exit(1)