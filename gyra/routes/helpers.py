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

# Actors are RECEIVERS of value, not the people building it.
# Never write a story from the perspective of internal staff
# (Designer/Developer/Artist/QA) unless the deliverable is a tool
# another staffer will use. Example of the right reframe:
#   ❌ Designer needs tuning enemy damage for balanced combat
#   ✅ User needs tuned enemies for balanced game combat
ACTOR_OPTIONS = ['User', 'Admin']

VERB_OPTIONS = ['needs', 'wants', 'must', 'requires', 'should',
                'would like to', 'has to', 'expects to', 'can',
                'wishes to', 'should be able to', 'must be able to',
                'needs to be able to']

# 'to' / 'for' are the everyday choices; the rest are kept for
# longer-form outcome phrasing.
CONNECTOR_OPTIONS = ['to', 'for', 'because', 'so they', 'so we', 'in order to']


# ── Sutherland / INVEST quality smells ───────────────────────────────────────
# Sutherland (Scrum) + Cohn's INVEST: stories should be Independent,
# Negotiable, Valuable, Estimable, Small, Testable. The checks below are
# WARNINGS only — they teach without blocking sloppy/lazy authors.

# Verbs that mean the author is prescribing implementation work rather than
# describing a user-facing action. "Add a button" / "implement endpoint" are
# tasks, not stories.
_SOLUTIONING_VERBS = {
    'add', 'create', 'build', 'implement', 'design', 'develop', 'make',
    'code', 'refactor', 'rewrite', 'wire', 'hook', 'integrate', 'setup',
    'configure', 'install', 'deploy',
}

# Vague filler that hides the real business/user value.
_VAGUE_OUTCOME_WORDS = {
    'stuff', 'things', 'thing', 'work', 'works', 'working', 'better',
    'nicer', 'nice', 'good', 'great', 'reasons', 'ux', 'usability',
    'cleanliness', 'cleaner', 'fun', 'cool', 'wow', 'magic',
}

_VAGUE_OUTCOME_PHRASES = (
    'it work', 'work better', 'feel better', 'be better', 'be nice',
    'be good', 'look nice', 'look good', 'be cool', 'good ux',
    'better ux', 'nice ux', 'just because',
)


def _significant_words(s: str):
    """Lowercased words minus stopwords/punctuation — for overlap checks."""
    stop = {
        'a', 'an', 'the', 'to', 'for', 'of', 'in', 'on', 'and', 'or',
        'so', 'they', 'their', 'them', 'we', 'us', 'our', 'is', 'be',
        'can', 'will', 'have', 'has', 'do', 'does', 'with', 'by', 'at',
        'as', 'this', 'that', 'it', 'its', 'i', 'my', 'your', 'you',
    }
    out = []
    for w in s.lower().split():
        w = w.strip('.,;:!?"\'()[]{}')
        if w and w not in stop:
            out.append(w)
    return out


def build_story_title(actor, verb, z, x, for_conn, y) -> str:
    return " ".join(p for p in [actor, verb, z, x, for_conn, y] if p)


def validate_story_parts(actor, verb, z, x, for_conn, y,
                          points=None, description=None):
    """Validate the 6-field GYRA grammar. Returns (errors, warnings).

    Errors block the save. Warnings are surfaced but do not block.

    Optional kwargs power Sutherland/INVEST quality warnings:
        points       — story point estimate (int); >8 → split-the-story nudge.
        description  — story description/acceptance text; empty + sized story
                       triggers a 'Testable' nudge.
    """
    errors, warnings = [], []
    a = (actor or '').strip()
    v = (verb or '').strip()
    z_ = (z or '').strip()
    x_ = (x or '').strip()
    fc = (for_conn or '').strip()
    y_ = (y or '').strip()

    if not a:
        errors.append("Actor is missing — pick who RECEIVES the value "
                      "(User, Admin).")
    elif a.lower() not in {opt.lower() for opt in ACTOR_OPTIONS}:
        errors.append(
            f"Actor \"{a}\" is not allowed. "
            f"Use one of: {', '.join(ACTOR_OPTIONS)}."
        )
    if not v:
        errors.append("Needs phrase is missing — pick one (needs, wants, must…).")
    elif v.lower() not in {opt.lower() for opt in VERB_OPTIONS}:
        errors.append(
            f"Needs phrase \"{v}\" is not in the allowed list. "
            f"Use one of: {', '.join(VERB_OPTIONS)}."
        )

    if not z_:
        errors.append("This is missing — name a single observable action "
                      "(verb), e.g. crawl, press button, save, jump.")
    else:
        # Soft nudge — encourage a verb/observable action over a noun phrase.
        first = z_.split()[0].lower().rstrip('.,;:')
        nounish_starters = {
            'a', 'an', 'the', 'my', 'their', 'his', 'her', 'its',
            'tuning', 'tuned',
        }
        if first in nounish_starters:
            warnings.append(
                f"\"{z_}\" looks like a noun phrase. "
                "Try a verb/observable action — crawl, press button, "
                "save progress, jump."
            )
        if len(z_) > 80:
            warnings.append(
                f"This field is long ({len(z_)} chars). "
                "Consider tightening it for readability."
            )

    if not x_:
        errors.append("Where is missing — where does this happen?")

    if not fc:
        errors.append("To is missing — pick to, because, or so they.")
    elif fc.lower() not in {c.lower() for c in CONNECTOR_OPTIONS}:
        errors.append(
            f"To \"{fc}\" is not allowed. "
            f"Use one of: {', '.join(CONNECTOR_OPTIONS)}."
        )

    if not y_:
        errors.append("What is missing — what result does the Actor need?")

    wc = count_words(a, v, z_, x_, fc, y_)
    if wc > 19:
        errors.append(
            f"Story is {wc} words — max is 19. "
            "Tighten the language or split the story."
        )

    # ── Sutherland / INVEST quality nudges (warnings only) ──────────────────
    z_low = z_.lower()
    y_low = y_.lower()

    # Independent / Small — "and" usually means two stories smashed together.
    for field_name, val in (("this", z_low), ("what", y_low)):
        if ' and ' in val or '&' in val or ',' in val:
            warnings.append(
                f"\"{field_name}\" contains 'and'/','/'&' — looks like two "
                "stories in one. Split it (INVEST: Independent, Small)."
            )
            break

    # Negotiable — solutioning verb at the start of `this` (prescribing
    # implementation work rather than describing the user's action).
    if z_:
        first = z_.split()[0].lower().rstrip('.,;:')
        if first in _SOLUTIONING_VERBS:
            warnings.append(
                f"\"this\" starts with \"{first}\" — that's a build task, not "
                "a user action. Describe what the user DOES (press, save, "
                "browse), not what the team builds."
            )

    # Valuable — outcome `what` is vague filler.
    if y_:
        y_tokens = {w.strip('.,;:!?"\'()[]{}').lower() for w in y_.split()}
        vague_hits = y_tokens & _VAGUE_OUTCOME_WORDS
        vague_phrase = next((p for p in _VAGUE_OUTCOME_PHRASES if p in y_low), None)
        if vague_hits or vague_phrase:
            warnings.append(
                f"\"what\" is vague (\"{y_}\"). State the concrete user/business "
                "value — what changes for them when this ships?"
            )

    # Valuable — circular (action just restated as outcome).
    if z_ and y_:
        z_sig = set(_significant_words(z_))
        y_sig = set(_significant_words(y_))
        if z_sig and y_sig:
            overlap = len(z_sig & y_sig) / max(1, min(len(z_sig), len(y_sig)))
            if overlap >= 0.7:
                warnings.append(
                    "\"what\" mostly restates \"this\". The outcome should "
                    "explain WHY the user needs it, not repeat the action."
                )

    # Small / Estimable — Sutherland: anything over ~8 points should be split.
    if isinstance(points, int) and points > 8:
        warnings.append(
            f"Story is {points} points — Sutherland's rule of thumb: split "
            "anything over 8. Big stories hide unknowns."
        )

    # Testable — sized work with no description/acceptance criteria.
    desc_txt = (description or '').strip()
    if isinstance(points, int) and points >= 3 and not desc_txt:
        warnings.append(
            "No description/acceptance criteria. A story isn't Testable "
            "without them — add at least one 'done when…' line."
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
