# Custom Dockerfile for the TRCE web service.
#
# Nixpacks auto-detection was failing two ways:
#   1. It only saw flask+gunicorn in synthetic requirements.txt, missing
#      supabase, bcrypt, stripe that website/requirements.txt explicitly lists.
#   2. The build context was website/ ONLY, so the bot/ subdirectory (needed
#      for `import db` and `from services import stripe_billing`) wasn't in
#      the container at /app/bot/.
#
# This Dockerfile copies BOTH website/ and bot/ into /app/ so:
#   - /app/website/app.py is the entry point
#   - /app/bot/db.py, /app/bot/services/stripe_billing.py are importable
#   - All 5 deps from website/requirements.txt get installed explicitly

FROM python:3.12-slim

WORKDIR /app

# Install ALL Python deps in one shot (web + bot — bot deps are harmless to web)
COPY website/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the entire repo so bot/ is available alongside website/
COPY . /app

# gunicorn needs WORKDIR=/app/website so `app:app` resolves
WORKDIR /app/website

# Railway injects $PORT at runtime; gunicorn binds there
CMD exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60