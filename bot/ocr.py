"""Image processing for RecallBiz bot.

Two paths:
1. QR code decode (OpenCV cv2) — pure Python wheel, no system deps
2. Paper card OCR (pytesseract + heuristic parser) — needs tesseract binary

For v0.1 we use local Tesseract for OCR + OpenCV for QR (no system libzbar).
MiniMax Vision can be added later as an accuracy upgrade.
"""
import io
import re
import logging
from typing import Optional

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# Lazy imports — these are heavy and we don't want to import unless needed
_cv2 = None


def _get_cv2():
    global _cv2
    if _cv2 is None:
        import cv2
        _cv2 = cv2
    return _cv2


def try_decode_qr(image_bytes: bytes) -> Optional[str]:
    """Try to decode a QR code from image bytes. Returns the URL or None.

    Uses OpenCV's QRCodeDetector — pure Python wheel, no system deps needed.
    """
    try:
        cv2 = _get_cv2()
        import numpy as np
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(img)
        if data:
            log.info("QR decoded: %s", data[:100])
            return data
    except Exception as e:
        log.warning("cv2 QR decode failed: %s", e)
    return None


def try_ocr_card(image_bytes: bytes) -> Optional[dict]:
    """OCR a paper business card. Returns structured dict or None.

    Returns: {name, title, company, email, phone, handle} or None if extraction fails.
    """
    try:
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        # Upscale small images (improves OCR accuracy)
        if img.width < 1000:
            scale = 1000 / img.width
            new_size = (int(img.width * scale), int(img.height * scale))
            img = img.resize(new_size, Image.LANCZOS)
        # Convert to grayscale for OCR
        gray = ImageOps.grayscale(img)
        text = pytesseract.image_to_string(gray)
        log.info("OCR extracted %d chars: %r", len(text), text[:200])
        if not text.strip():
            return None
        return parse_card_text(text)
    except Exception as e:
        log.warning("OCR failed: %s", e)
        return None


def parse_card_text(text: str) -> dict:
    """Heuristic parser for business card text -> structured fields.

    Tries to extract: name, title, company, email, phone, handle.
    """
    result = {
        "name": None,
        "title": None,
        "company": None,
        "email": None,
        "phone": None,
        "handle": None,
    }

    # Work on stripped non-empty lines
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # 1. Extract email
    email_re = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    for i, line in enumerate(lines):
        m = email_re.search(line)
        if m:
            result["email"] = m.group(0)
            lines[i] = (line[:m.start()] + line[m.end():]).strip()
            break

    # 2. Extract phone (international-friendly)
    phone_re = re.compile(r"(\+?\d[\d\s\-\(\)\.]{7,}\d)")
    for i, line in enumerate(lines):
        m = phone_re.search(line)
        if m:
            result["phone"] = m.group(0).strip()
            lines[i] = (line[:m.start()] + line[m.end():]).strip()
            break

    # 3. Extract Telegram handle
    handle_re = re.compile(r"@([a-zA-Z][a-zA-Z0-9_]{4,31})")
    for i, line in enumerate(lines):
        m = handle_re.search(line)
        if m:
            result["handle"] = m.group(1)
            lines[i] = (line[:m.start()] + line[m.end():]).strip()
            break

    # 4. Filter remaining empty lines
    lines = [l for l in lines if l and len(l) > 1]

    # 5. Heuristic field assignment from remaining lines
    title_keywords = (
        "CEO", "CTO", "CFO", "COO", "CMO", "CRO", "CPO",
        "Founder", "Co-Founder", "Co-founder",
        "Director", "Manager", "Engineer", "Developer", "Designer",
        "Lead", "Head", "VP", "President", "Partner", "Analyst",
        "Consultant", "Architect", "Chief", "Officer", "Principal",
        "Specialist", "Associate", "Researcher", "Scientist",
        "Product", "Marketing", "Sales", "Operations",
    )

    if lines:
        result["name"] = lines[0]
    if len(lines) > 1:
        if any(kw in lines[1] for kw in title_keywords):
            result["title"] = lines[1]
            if len(lines) > 2:
                result["company"] = lines[2]
        else:
            # If second line has typical company markers
            company_markers = ("Inc", "Ltd", "LLC", "Corp", "Co.", "GmbH", "S.A.",
                               "Group", "Labs", "Studio", "Capital", "Ventures",
                               "Partners", "Foundation", "Holdings")
            if any(m in lines[1] for m in company_markers):
                result["company"] = lines[1]
                if len(lines) > 2:
                    result["title"] = lines[2]
            else:
                result["company"] = lines[1]
                if len(lines) > 2:
                    result["title"] = lines[2]

    # Drop empty values
    return {k: v for k, v in result.items() if v}


def parse_telegram_qr(qr_url: str) -> Optional[str]:
    """Extract handle from Telegram QR URL.

    Handles: https://t.me/username, tg://resolve?domain=username,
             tg://user?id=12345
    Returns: handle (without @) or None.
    """
    if not qr_url:
        return None
    if "t.me/" in qr_url:
        after = qr_url.split("t.me/")[-1]
        handle = after.split("?")[0].split("/")[0].strip()
        if handle and handle != "joinchat":
            return handle
    if "domain=" in qr_url:
        return qr_url.split("domain=")[-1].split("&")[0].strip()
    if qr_url.startswith("@"):
        return qr_url[1:].strip()
    return None
