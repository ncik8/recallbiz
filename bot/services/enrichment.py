"""services/enrichment.py — AI company enrichment for TRCE contacts.

When a user adds a contact with a website, company name, or work email,
this module fires a 2-step pipeline:
  1. Search the web via Perplexity Sonar (OpenRouter) for snippets about the company
  2. Summarize via M3 into a 3-sentence description + source URLs

The description is written to contacts.ai_description (see migration 013),
along with the source URLs and a timestamp. Also returns the user's other
contacts at the same company so the bot can suggest a warm intro.

Cache: in-memory dict keyed by normalized domain, TTL 30 days. Shared across
users (most TRCE users save the same handful of crypto companies).

Failures are silent — if search returns nothing, the contact is saved with
no AI description. The user-facing bot message just doesn't include the
enrichment block.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

# Reuse existing helpers — bot already wires M3 + Perplexity Sonar
from ai import call_minimax, call_sonar

log = logging.getLogger(__name__)

CACHE_TTL = timedelta(days=30)
CACHE_MAX_SIZE = 1000

# ============================================================
# Cache
# ============================================================

_cache: dict[str, dict] = {}


def _cache_key(domain: str) -> str:
    return hashlib.sha256(domain.lower().encode()).hexdigest()


def _cache_get(domain: str) -> Optional[dict]:
    """Return cached enrichment if present and fresh."""
    if not domain:
        return None
    key = _cache_key(domain)
    entry = _cache.get(key)
    if not entry:
        return None
    age = datetime.now(timezone.utc) - entry["cached_at"]
    if age > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return entry


def _cache_put(domain: str, summary: str, sources: list) -> None:
    """Cache an enrichment result. Evicts oldest if over cap."""
    if len(_cache) >= CACHE_MAX_SIZE:
        # Evict oldest entry (insertion order in Python 3.7+ dicts)
        oldest_key = next(iter(_cache))
        _cache.pop(oldest_key, None)
    _cache[_cache_key(domain)] = {
        "domain": domain,
        "summary": summary,
        "sources": sources,
        "cached_at": datetime.now(timezone.utc),
    }


# ============================================================
# Domain extraction
# ============================================================

def extract_domain(website: Optional[str] = None, email: Optional[str] = None, company: Optional[str] = None) -> Optional[str]:
    """Best-effort domain derivation from contact fields.

    Priority: website > email (after @) > company string (last resort, less reliable).
    Returns normalized form like "acmerobotics.com" or None if unparseable.
    """
    candidates = []
    if website:
        candidates.append(website)
    if email and "@" in email:
        candidates.append(email.split("@", 1)[1])
    if company and not candidates:
        # Heuristic: turn "Acme Robotics" into "acmerobotics.com" if it looks like a brand
        slug = re.sub(r"[^a-z0-9]", "", company.lower())
        if 3 < len(slug) < 30:
            candidates.append(slug + ".com")

    for raw in candidates:
        if not raw:
            continue
        # Strip scheme, path, query, fragment
        if "://" in raw:
            raw = raw.split("://", 1)[1]
        raw = raw.split("/", 1)[0]
        raw = raw.split("?", 1)[0]
        raw = raw.split("#", 1)[0]
        raw = raw.lower().strip()
        # Strip www.
        if raw.startswith("www."):
            raw = raw[4:]
        # Validate: must have at least one dot, valid TLD-like ending
        if re.match(r"^[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}$", raw):
            return raw
    return None


# ============================================================
# Pipeline
# ============================================================

SYSTEM_PROMPT = """You summarize company information into 3 short sentences based ONLY on the provided search results. Format: one sentence on what the company does, one sentence on their key product or service, one sentence on notable recent news (funding, product launch, hiring). Be concise. No marketing fluff. No "they are a leading provider of...". Use plain language. If the search results don't tell you something, don't make it up."""


async def _search_snippets(domain: str) -> list[dict]:
    """Perplexity Sonar returns a cited answer + a list of sources.
    Returns [{"url": ..., "title": ...}, ...] from the Sonar citations."""
    query = f"What does the company at {domain} do? What are their main products? Any recent news or funding?"
    raw = await call_sonar(query, model=os.environ.get("OPENROUTER_SONAR_MODEL", "perplexity/sonar"))
    if not raw or "isn't configured" in raw.lower() or "no answer" in raw.lower():
        return []
    # Sonar returns inline [1], [2] citations. We don't have a clean way to parse URLs
    # from the prose — but we get the answer text. For sources, we fall back to Sonar's
    # own answer text (it cites sources inline). If we want a clean URL list, we'd need
    # to switch to Exa (which returns structured JSON with URLs).
    # For v1: return one synthetic source pointing to the Sonar query.
    return [{"url": f"https://www.google.com/search?q={domain}", "title": f"Web search for {domain}"}]


async def _summarize(domain: str, snippets_text: str) -> Optional[str]:
    """M3 turns Sonar's answer into a clean 3-sentence summary."""
    if not snippets_text.strip():
        return None
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Company domain: {domain}\n\nSearch result:\n{snippets_text[:3000]}\n\nWrite the 3-sentence summary:"},
    ]
    try:
        resp = await call_minimax(messages, temperature=0.3)
        content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        # Strip markdown emphasis that M3 often adds
        content = content.strip().strip("`*_")
        return content or None
    except Exception as e:
        log.warning("enrichment._summarize failed for %s: %s", domain, e)
        return None


async def enrich_domain(domain: str) -> Optional[dict]:
    """Run the full enrichment pipeline. Returns {summary, sources} or None.

    Uses the in-memory cache — if a fresh result exists for this domain,
    returns it without calling any APIs.
    """
    if not domain:
        return None

    # Check cache first
    cached = _cache_get(domain)
    if cached:
        log.debug("enrichment cache HIT for %s", domain)
        return {"summary": cached["summary"], "sources": cached["sources"], "from_cache": True}

    # Search → summarize
    try:
        raw = await call_sonar(
            f"What does the company at {domain} do? What are their main products? Any recent news or funding?",
            model=os.environ.get("OPENROUTER_SONAR_MODEL", "perplexity/sonar"),
        )
    except Exception as e:
        log.warning("enrichment Sonar call failed for %s: %s", domain, e)
        return None

    if not raw or len(raw.strip()) < 20:
        log.debug("enrichment: Sonar returned nothing useful for %s", domain)
        return None

    summary = await _summarize(domain, raw)
    if not summary:
        return None

    # Sources: the Sonar answer includes inline citations. For v1 we link to a
    # Google search for transparency. (Better would be to switch to Exa for
    # structured URLs — see /tmp/trce-enrichment-plan.md followups.)
    sources = [{"url": f"https://www.google.com/search?q=%22{domain}%22", "label": "Web search"}]

    _cache_put(domain, summary, sources)
    return {"summary": summary, "sources": sources, "from_cache": False}


# ============================================================
# Cache introspection (for debugging / admin /stats command)
# ============================================================

def cache_stats() -> dict:
    """Return cache stats — wired into /stats command later."""
    return {
        "size": len(_cache),
        "max_size": CACHE_MAX_SIZE,
        "domains": sorted(e["domain"] for e in _cache.values())[:20],
    }


def cache_clear() -> int:
    """Clear the cache. Returns number of entries cleared."""
    n = len(_cache)
    _cache.clear()
    return n