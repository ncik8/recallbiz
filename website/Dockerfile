# Custom Dockerfile for the TRCE web service (Railway).
#
# Why this exists: Nixpacks auto-detection builds from the website/ subdir only,
# which means bot/ (and its db.py / services/) are missing from the container.
# Our code imports bot/db.py and bot/services/stripe_billing.py -- fails at runtime.
#
# This Dockerfile copies the whole repo so bot/ is alongside website/ at /app/.

FROM python:3.12-slim

WORKDIR /app

# Install ALL Python deps explicitly (web needs flask, gunicorn, supabase,
# bcrypt, stripe -- bot deps are harmless on the web image).
COPY website/requirements.txt /app/website-requirements.txt
RUN pip install --no-cache-dir -r /app/website-requirements.txt

# Copy the whole repo so bot/ ends up at /app/bot/, alongside website/ at /app/website/
COPY . /app/

# gunicorn needs to find app:app -- chdir into website/ so app.py resolves
WORKDIR /app/website

# Railway injects $PORT; gunicorn binds there
CMD exec gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60