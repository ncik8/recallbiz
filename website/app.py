"""Minimal Flask app for trce landing + future backend.
Serves the static site at / and adds API routes as the product grows.
"""
import os
from flask import Flask, send_from_directory, abort

app = Flask(__name__, static_folder=None)


# ============== STATIC FILES ==============
# Serve all static files from the same directory as app.py
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


@app.route("/<path:filename>")
def static_file(filename):
    # Serve any other file in the static dir (CSS, JS, images, etc.)
    try:
        return send_from_directory(STATIC_DIR, filename)
    except Exception:
        abort(404)


# ============== FUTURE API ROUTES ==============
# /api/signup, /api/auth/telegram, /api/contacts etc. go here as the product grows


# ============== HEALTHCHECK ==============
@app.route("/health")
def health():
    return {"status": "ok", "service": "trce-web"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)