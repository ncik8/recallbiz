"""TRCE web app: landing page, login, dashboard, edit, CSV download.

Imports the bot's `db.py` for Supabase access. Both services share the same
Supabase project — the bot writes contacts via Telegram, the web dashboard
reads + edits them via bcrypt-verified login at trce.io/login.

Route map:
  GET  /                              -> landing page
  GET  /login                         -> login form (email + password)
  POST /login                         -> verify password, set session cookie
  GET  /logout                        -> clear session
  GET  /dashboard                     -> contact list (auth-gated)
  GET  /dashboard/edit/<id>           -> edit form (auth-gated)
  POST /dashboard/edit/<id>           -> save edits (auth-gated)
  GET  /dashboard/download.csv        -> all contacts as CSV (auth-gated)
  GET  /<path:filename>               -> static files (CSS, JS, images, blog)
"""
import os
import sys
import csv
import io
import secrets
from datetime import datetime, timezone
from flask import (
    Flask, request, session, redirect, url_for, render_template, render_template_string,
    send_from_directory, abort, Response, jsonify,
)

# Make the bot's db.py importable from the website service (Railway monorepo).
# WORKDIR varies across deploys (Nixpacks sets /app/, but the bot dir may be at
# /app/bot/ or elsewhere). Search for the bot directory by looking for db.py.
_bot_dir_candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot"),
    "/app/bot",
]
for _candidate in _bot_dir_candidates:
    _candidate_abs = os.path.abspath(_candidate)
    if os.path.isfile(os.path.join(_candidate_abs, "db.py")):
        sys.path.insert(0, _candidate_abs)
        break

import db  # noqa: E402
# Explicit top-level import of stripe so Railway Nixpacks installs it.
# stripe_billing is the actual user; this is just to make the dep visible.
import stripe  # noqa: E402, F401
from services import stripe_billing  # noqa: E402

app = Flask(__name__, static_folder=None)

# Session secret. Use FLASK_SECRET in env; fallback is dev-only random per-process
# (sessions invalidate on restart — fine for dev, NOT for production).
app.secret_key = os.environ.get("FLASK_SECRET") or secrets.token_hex(32)
app.permanent_session_lifetime = 60 * 60 * 24 * 30  # 30 days


# ============== STATIC FILES ==============
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/blog/")
@app.route("/blog")
def blog_index():
    return send_from_directory(STATIC_DIR, "blog/index.html")


@app.route("/blog/<path:filename>")
def blog_post(filename):
    return send_from_directory(os.path.join(STATIC_DIR, "blog"), filename)


@app.route("/design-preview.html")
def design_preview():
    return send_from_directory(STATIC_DIR, "design-preview.html")


@app.route("/pricing.html")
def pricing_page():
    return send_from_directory(STATIC_DIR, "pricing.html")


@app.route("/referral.html")
def referral_page():
    return send_from_directory(STATIC_DIR, "referral.html")


@app.route("/card-detail.html")
def card_detail_page():
    return send_from_directory(STATIC_DIR, "card-detail.html")


# ============== AUTH ==============

def _current_user_id() -> str | None:
    """Resolve the session user_id, or None if not logged in."""
    return session.get("user_id")


def _require_login():
    """Redirect to /login if not authenticated."""
    if not _current_user_id():
        return redirect(url_for("login_get", next=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login_get():
    """GET: show form. POST: verify password and set session."""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        result = db.verify_user_password(email, password)
        if result.get("success"):
            session.permanent = True
            session["user_id"] = result["user_id"]
            session["email"] = email.lower()
            next_url = request.args.get("next") or "/dashboard"
            return redirect(next_url)
        return render_template("login.html", error=result.get("error", "Login failed."))
    # GET — if already logged in, go straight to dashboard
    if _current_user_id():
        return redirect("/dashboard")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ============== DASHBOARD ==============

def _humanize_when(iso_str: str | None) -> str:
    """Compact relative time for the dashboard table."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        if secs < 604800:
            return f"{secs // 86400}d ago"
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso_str[:10] if iso_str else "—"


def _initials(name: str | None) -> str:
    if not name:
        return "?"
    parts = [p for p in name.replace("@", "").split() if p][:2]
    return "".join(p[0].upper() for p in parts) if parts else "?"


@app.route("/dashboard")
def dashboard():
    if (r := _require_login()):
        return r
    user_id = _current_user_id()
    user = db.get_user_by_id(user_id) or {}
    plan = user.get("plan") or "free"
    plan_label = "Pro" if plan in ("pro", "tester") else "Free"
    contacts_raw = db.list_user_contacts(user_id)
    contacts = [
        {
            **c,
            "initials": _initials(c.get("name")),
            "saved_rel": _humanize_when(c.get("saved_at")),
        }
        for c in contacts_raw
    ]
    show_deleted_flash = request.args.get("deleted") == "1"
    return render_template(
        "dashboard.html",
        user_email=user.get("email") or session.get("email") or "",
        user_initial=_initials(user.get("display_name") or user.get("email") or "?"),
        plan_label=plan_label,
        contacts=contacts,
        show_deleted_flash=show_deleted_flash,
    )


@app.route("/dashboard/edit/<contact_id>", methods=["GET", "POST"])
def edit_contact(contact_id):
    if (r := _require_login()):
        return r
    user_id = _current_user_id()
    client = db.get_client()

    if request.method == "POST":
        updates = {
            "name": (request.form.get("name") or "").strip(),
            "handle": (request.form.get("handle") or "").strip().lstrip("@") or None,
            "company": (request.form.get("company") or "").strip() or None,
            "title": (request.form.get("title") or "").strip() or None,
            "email": (request.form.get("email") or "").strip() or None,
            "phone": (request.form.get("phone") or "").strip() or None,
            "website": (request.form.get("website") or "").strip() or None,
            "notes": (request.form.get("notes") or "").strip() or None,
        }
        try:
            client.table("contacts").update(updates).eq("id", contact_id).eq("user_id", user_id).execute()
            return render_template("edit.html", contact={**updates, "id": contact_id}, saved=True)
        except Exception as e:
            app.logger.exception("edit_contact failed")
            return render_template("edit.html", contact={"id": contact_id, **updates},
                                   saved=False, error=str(e))

    res = client.table("contacts").select("*").eq("id", contact_id).eq("user_id", user_id).limit(1).execute()
    if not res.data:
        abort(404)
    return render_template("edit.html", contact=res.data[0])


@app.route("/dashboard/delete/<contact_id>", methods=["GET", "POST"])
def delete_contact_route(contact_id):
    """Dashboard-only delete. Bot is forbidden from deleting — see ai.py system prompt.

    GET: shows a confirmation page with the contact's name + a red Delete button.
    POST: actually performs the delete, then redirects to /dashboard with a flash.
    Two-step on purpose: forces a deliberate click, no accidental deletes.
    """
    if (r := _require_login()):
        return r
    user_id = _current_user_id()

    # Verify the contact belongs to this user (security: don't reveal other users' data)
    res = db.get_client().table("contacts").select("id, name").eq("id", contact_id).eq("user_id", user_id).limit(1).execute()
    if not res.data:
        abort(404)
    contact_name = res.data[0].get("name") or "this contact"

    if request.method == "POST":
        ok = db.delete_contact(contact_id, user_id)
        if ok:
            return redirect("/dashboard?deleted=1")
        return render_template_string(
            DELETE_CONFIRM_HTML,
            contact={"id": contact_id, "name": contact_name},
            error="Delete failed. Try again or refresh.",
        ), 500

    return render_template_string(DELETE_CONFIRM_HTML, contact={"id": contact_id, "name": contact_name})


# Inline confirm page — kept in app.py to avoid another template file.
# Stylistically matches login.html / dashboard.html (same dark theme tokens).
DELETE_CONFIRM_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Delete contact — trce</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root { --bg:#0a0a0e; --surface:#16161e; --surface-2:#1e1e28; --border:#2a2a35;
          --text:#e8e8ee; --text-2:#8a8a95; --text-3:#5a5a65; --accent:#00ff88;
          --danger:#ff5555; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family:'Inter',sans-serif; background:var(--bg); color:var(--text);
         min-height:100vh; display:flex; align-items:center; justify-content:center;
         padding:24px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:10px;
          padding:32px; max-width:440px; width:100%; }
  h1 { font-size:20px; font-weight:500; margin-bottom:8px; }
  p { color:var(--text-2); font-size:14px; margin-bottom:24px; line-height:1.6; }
  .name { color:var(--text); font-weight:500; }
  .actions { display:flex; gap:12px; justify-content:flex-end; margin-top:24px;
             padding-top:24px; border-top:1px solid var(--border); }
  .btn { padding:10px 18px; border-radius:6px; font-size:14px; font-weight:500;
         text-decoration:none; border:1px solid var(--border-strong); background:var(--surface-2);
         color:var(--text); cursor:pointer; }
  .btn-danger { background:var(--danger); color:#fff; border-color:var(--danger); font-weight:600; }
  .btn-danger:hover { background:#ff3333; border-color:#ff3333; }
  .err { background:rgba(255,85,85,0.12); color:var(--danger); padding:10px 14px;
         border-radius:6px; font-size:13px; margin-bottom:16px; border:1px solid rgba(255,85,85,0.3); }
</style>
</head><body>
<div class="card">
  <h1>Delete this contact?</h1>
  <p>This permanently removes <span class="name">{{ contact.name }}</span> from your account. This can't be undone.</p>
  <p style="font-size:13px; color:var(--text-3);">Tip: download a CSV first if you want a backup.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form action="/dashboard/delete/{{ contact.id }}" method="POST">
    <div class="actions">
      <a class="btn" href="/dashboard/edit/{{ contact.id }}">Cancel</a>
      <button type="submit" class="btn btn-danger">Delete permanently</button>
    </div>
  </form>
</div>
</body></html>"""


@app.route("/dashboard/download.csv")
def download_csv():
    if (r := _require_login()):
        return r
    user_id = _current_user_id()
    contacts = db.list_user_contacts(user_id, limit=10000)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "name", "handle", "company", "title", "email", "phone",
        "website", "notes", "source", "saved_at",
    ])
    for c in contacts:
        writer.writerow([
            c.get("name") or "",
            c.get("handle") or "",
            c.get("company") or "",
            c.get("title") or "",
            c.get("email") or "",
            c.get("phone") or "",
            c.get("website") or "",
            c.get("notes") or "",
            c.get("source") or "",
            c.get("saved_at") or "",
        ])

    out = buf.getvalue()
    return Response(
        out,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="trce-contacts-{datetime.now(timezone.utc).strftime("%Y%m%d")}.csv"',
        },
    )


# ============== STATIC FALLBACK ==============
@app.route("/<path:filename>")
def static_file(filename):
    try:
        return send_from_directory(STATIC_DIR, filename)
    except Exception:
        abort(404)


# ============== HEALTHCHECK ==============
@app.route("/health")
def health():
    return {"status": "ok", "service": "trce-web"}


# ============== STRIPE ==============
# Two endpoints:
#   POST /stripe/webhook   - Stripe -> us. Idempotent. Handles 4 event types.
#   POST /upgrade          - Pricing page form. Creates Checkout Session, redirects.
# Webhook is the SOURCE OF TRUTH for plan changes. /upgrade only generates links.

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events.

    Stripe sends raw bytes with a signature header. We MUST verify the signature
    using STRIPE_WEBHOOK_SECRET before trusting any payload. Returns 200 fast so
    Stripe doesn't retry (Stripe retries on 4xx/5xx within 24h).
    """
    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe_billing.verify_webhook(payload, signature)
    except Exception as e:
        app.logger.warning("Stripe webhook signature verify failed: %s", e)
        return {"error": "invalid signature"}, 400

    etype = event["type"] if isinstance(event, dict) else event.type
    obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    app.logger.info("Stripe webhook: type=%s id=%s", etype, getattr(obj, "id", "?"))

    try:
        _handle_stripe_event(etype, obj)
    except Exception as e:
        app.logger.exception("Stripe webhook handler failed for type=%s", etype)
        # Still 200 -- Stripe will not retry if we ack. Better to lose this event
        # than to retry forever on a logic bug. Logged for manual fix.
    return {"received": True}, 200


def _handle_stripe_event(etype: str, obj):
    """Apply a verified Stripe event to the database.

    Events we care about:
      - checkout.session.completed        : new subscription -> plan='pro'
      - customer.subscription.updated     : renewal / status change
      - customer.subscription.deleted     : cancellation -> plan='free'
      - invoice.payment_failed            : card declined -> status='past_due'
    """
    if etype == "checkout.session.completed":
        # Session has client_reference_id (our internal user_id) and customer/subscription IDs.
        user_id = getattr(obj, "client_reference_id", None)
        customer_id = getattr(obj, "customer", None)
        subscription_id = getattr(obj, "subscription", None)
        if not user_id:
            app.logger.warning("checkout.session.completed missing client_reference_id")
            return
        # Fetch the subscription details for period_end (need a separate API call).
        period_end = None
        if subscription_id and stripe_billing.is_configured():
            try:
                sub = stripe_billing._get_stripe().Subscription.retrieve(subscription_id)
                if sub and getattr(sub, "current_period_end", None):
                    from datetime import datetime, timezone
                    period_end = datetime.fromtimestamp(
                        sub.current_period_end, tz=timezone.utc
                    ).isoformat()
            except Exception as e:
                app.logger.warning("Could not fetch subscription period_end: %s", e)
        db.set_user_plan(
            user_id,
            "pro",
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
            subscription_status="active",
            subscription_period_end=period_end,
            pro_since=_now_iso(),
        )

    elif etype == "customer.subscription.updated":
        sub_id = getattr(obj, "id", None)
        status = getattr(obj, "status", None)  # active, trialing, past_due, canceled, ...
        period_end = None
        if getattr(obj, "current_period_end", None):
            from datetime import datetime, timezone
            period_end = datetime.fromtimestamp(
                obj.current_period_end, tz=timezone.utc
            ).isoformat()
        # Look up our user by stripe_subscription_id
        user_id = _user_id_for_subscription(sub_id)
        if not user_id:
            app.logger.warning("subscription.updated for unknown sub_id=%s", sub_id)
            return
        new_plan = "pro" if status in ("active", "trialing") else "free"
        db.set_user_plan(
            user_id,
            new_plan,
            stripe_subscription_id=sub_id,
            subscription_status=status,
            subscription_period_end=period_end,
        )

    elif etype == "customer.subscription.deleted":
        sub_id = getattr(obj, "id", None)
        user_id = _user_id_for_subscription(sub_id)
        if not user_id:
            app.logger.warning("subscription.deleted for unknown sub_id=%s", sub_id)
            return
        db.set_user_plan(
            user_id,
            "free",
            stripe_subscription_id=None,  # clear so user_id_for_subscription won't match stale subs
            subscription_status="canceled",
        )

    elif etype == "invoice.payment_failed":
        sub_id = getattr(obj, "subscription", None)
        user_id = _user_id_for_subscription(sub_id)
        if user_id:
            db.set_user_plan(user_id, "free", subscription_status="past_due")


def _user_id_for_subscription(stripe_subscription_id):
    """Look up our internal user_id by stripe_subscription_id. Returns None if not found."""
    if not stripe_subscription_id:
        return None
    try:
        res = (
            db.get_client()
            .table("users")
            .select("id")
            .eq("stripe_subscription_id", stripe_subscription_id)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]["id"]
    except Exception as e:
        app.logger.warning("_user_id_for_subscription failed: %s", e)
    return None


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@app.route("/upgrade", methods=["POST"])
def upgrade_web():
    """Pricing-page form POST: create a Stripe Checkout Session and redirect.

    Available to both logged-in users (will pre-fill their email) and anon
    visitors (Stripe collects email at checkout, then webhook sets plan by
    client_reference_id only when the user is logged in -- see note below).
    """
    interval = (request.form.get("interval") or "monthly").lower()
    if interval not in ("monthly", "annual"):
        return {"error": "interval must be monthly or annual"}, 400

    user_id = _current_user_id()  # may be None for anon visitors
    customer_email = None
    if user_id:
        user = db.get_user_by_id(user_id) or {}
        customer_email = user.get("email")

    if not stripe_billing.is_configured():
        return render_template_string(
            "<h1>Stripe not configured</h1>"
            "<p>Tell the founder to set STRIPE_SECRET_KEY on the server.</p>"
            "<p><a href='/'>Back to home</a></p>"
        ), 503

    try:
        url = stripe_billing.create_checkout_session(
            user_id=user_id or "anon",
            interval=interval,
            customer_email=customer_email,
        )
    except Exception as e:
        app.logger.exception("create_checkout_session failed")
        return {"error": "Could not create checkout session"}, 500

    return redirect(url, code=303)


# ============== MAIN ==============
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)