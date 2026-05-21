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


# ── GYRA story grammar — canonical option lists ──────────────────────────────
# These are the only allowed values for the structured fields. Used by the
# story builder selects (board modal, story page, bulk-add) AND by the
# server-side validator. If you add a verb here it appears everywhere.

ACTOR_OPTIONS = ['User', 'Developer', 'Admin', 'Guest', 'Manager',
                 'Player', 'Designer', 'Artist', 'Animator', 'QA']

VERB_OPTIONS = ['needs', 'wants', 'must', 'requires', 'should',
                'would like to', 'has to', 'expects to', 'can',
                'wishes to', 'should be able to', 'must be able to',
                'needs to be able to']

CONNECTOR_OPTIONS = ['to', 'because', 'so they', 'so we', 'in order to']


def build_story_title(actor, verb, z, x, for_conn, y) -> str:
    return " ".join(p for p in [actor, verb, z, x, for_conn, y] if p)


def validate_story_parts(actor, verb, z, x, for_conn, y):
    """Validate the 6-field GYRA grammar. Returns (errors, warnings).

    Errors block the save. Warnings are surfaced but do not block — they exist
    to nudge writers toward proper Action Words (gerunds) without breaking
    legacy stories.
    """
    errors, warnings = [], []
    a = (actor or '').strip()
    v = (verb or '').strip()
    z_ = (z or '').strip()
    x_ = (x or '').strip()
    fc = (for_conn or '').strip()
    y_ = (y or '').strip()

    if not a:
        errors.append("Actor is missing — pick who needs this "
                      "(User, Developer, Admin…).")
    if not v:
        errors.append("Verb is missing — pick one (needs, wants, must…).")
    elif v.lower() not in {opt.lower() for opt in VERB_OPTIONS}:
        errors.append(
            f"Verb \"{v}\" is not in the allowed list. "
            f"Use one of: {', '.join(VERB_OPTIONS)}."
        )

    if not z_:
        errors.append("Action Word is missing — the single gerund this story "
                      "is about (e.g. Walking, Killing, Building).")
    else:
        if " " in z_:
            errors.append(
                f"Action Word \"{z_}\" has a space — it must be one word. "
                "If you need two ideas, split into two stories."
            )
        elif not z_.replace("-", "").isalpha():
            errors.append(
                f"Action Word \"{z_}\" has non-letter characters — "
                "use a clean gerund like Walking, Building, Saving."
            )
        elif not z_.lower().endswith("ing"):
            warnings.append(
                f"Action Word \"{z_}\" does not end in -ing. "
                "Action Words should be gerunds (Walking, Saving, Banning) — "
                "consider rephrasing."
            )

    if not x_:
        errors.append("What/feature is missing — what is being built or "
                      "acted on?")

    if not fc:
        errors.append("Connector is missing — pick to, because, or so they.")
    elif fc.lower() not in {c.lower() for c in CONNECTOR_OPTIONS}:
        errors.append(
            f"Connector \"{fc}\" is not allowed. "
            f"Use one of: {', '.join(CONNECTOR_OPTIONS)}."
        )

    if not y_:
        errors.append("Outcome is missing — why does the Actor need this?")

    wc = count_words(a, v, z_, x_, fc, y_)
    if wc > 19:
        errors.append(
            f"Story is {wc} words — max is 19. "
            "Tighten the language or split the story."
        )

    return errors, warnings


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
