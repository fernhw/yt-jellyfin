"""routes/helpers.py — Shared helper functions used across route modules."""
import base64
import io

import qrcode
from markupsafe import Markup, escape

ALLOWED_AVATAR_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_IMAGE_EXT  = {"png", "jpg", "jpeg", "webp"}


def allowed_avatar(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_AVATAR_EXT


def allowed_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXT


def count_words(*parts) -> int:
    return sum(len(str(p).split()) for p in parts if p)


def build_story_title(actor, verb, z, x, for_conn, y) -> str:
    return " ".join(p for p in [actor, verb, z, x, for_conn, y] if p)


def bold_verb_in_title(title: str, verb: str) -> Markup:
    """Return HTML-safe Markup with the action word wrapped in <strong>."""
    if not verb or not title:
        return Markup(escape(title or ""))
    idx = title.find(verb)
    if idx < 0:
        return Markup(escape(title))
    return Markup(
        escape(title[:idx]) +
        Markup("<strong>") +
        escape(verb) +
        Markup("</strong>") +
        escape(title[idx + len(verb):])
    )


def make_qr_png(uri: str) -> str:
    """Return a data-URI PNG of the TOTP QR code."""
    qr = qrcode.QRCode(box_size=6, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
