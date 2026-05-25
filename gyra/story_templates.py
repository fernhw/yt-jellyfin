"""story_templates.py — GYRA story templates + composable AC blocks.

Templates here are *scaffolds* a writer can apply on the New-Story screen.
The catalogue is intentionally small and modular — each template references
zero or more **blocks** defined in ``story_blocks.md``. A block contributes
its acceptance-criteria list when included. Tasks are NOT part of blocks —
teams break work down their own way, so templates ship only a few generic
tasks (or none) and let humans add the rest.

UI navigation
-------------
The picker is filtered by two axes:

* ``type``    — feature, asset, design, bug, fix, chore, spike, docs, marketing
* ``domain``  — game, app, web, corporate, cicd, devops, tech, content

Both fields live on every template.

Block composition
-----------------
A template specifies ``blocks=[...]``. The user can toggle additional blocks
on/off in the apply modal — final AC list is the de-duplicated union of all
selected blocks' ACs (preserving block order, AC order within each block).

Placeholders
------------
Fields and tasks may contain ``{key}`` tokens. Each token must have a matching
``questions`` entry. Skipped answers render as ``[key]`` so the writer can spot
them.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List


# ── Block loader ────────────────────────────────────────────────────────────

_BLOCKS_PATH = os.path.join(os.path.dirname(__file__), "story_blocks.md")


def _parse_blocks(path: str) -> Dict[str, dict]:
    """Parse ``story_blocks.md`` into ``{id: {id, name, description, tasks, acs}}``.

    Recognised structure per block::

        ## block-id — Display name
        _Optional one-line description._
        ### Tasks
        - task line 1
        - task line 2
        ### ACs
        - acceptance criterion 1

    Either section may be omitted. Bullets that appear before any ``###``
    heading are treated as ACs (legacy single-section format).
    """
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    blocks: Dict[str, dict] = {}
    current = None
    section = "acs"  # default: bullets without a heading land in ACs
    in_fence = False
    # Accept "—", "–" or "-" as the separator between id and name.
    header_re = re.compile(r"^##\s+([a-z0-9][a-z0-9\-]*)\s+[—–\-]\s+(.+?)\s*$")
    desc_re = re.compile(r"^_(.+?)_\s*$")
    section_re = re.compile(r"^###\s+(.+?)\s*$")
    bullet_re = re.compile(r"^[-*]\s+(.+?)\s*$")

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = header_re.match(line)
        if m:
            bid, name = m.group(1), m.group(2)
            current = {"id": bid, "name": name, "description": "",
                       "tasks": [], "acs": []}
            blocks[bid] = current
            section = "acs"
            continue
        if current is None:
            continue
        m = section_re.match(line)
        if m:
            label = m.group(1).strip().lower()
            if label.startswith("task"):
                section = "tasks"
            elif label.startswith("ac"):
                section = "acs"
            else:
                section = "acs"
            continue
        m = desc_re.match(line)
        if m and not current["acs"] and not current["tasks"]:
            current["description"] = m.group(1)
            continue
        m = bullet_re.match(line)
        if m:
            current[section].append(m.group(1))
            continue
        # any other line (blank, prose) is ignored
    return blocks


BLOCKS: Dict[str, dict] = _parse_blocks(_BLOCKS_PATH)


# ── Types & domains ─────────────────────────────────────────────────────────

TYPES = [
    {"id": "feature",   "name": "Feature"},
    {"id": "asset",     "name": "Asset"},
    {"id": "design",    "name": "Design"},
    {"id": "bug",       "name": "Bug"},
    {"id": "fix",       "name": "Fix"},
    {"id": "chore",     "name": "Chore"},
    {"id": "spike",     "name": "Spike / Research"},
    {"id": "docs",      "name": "Docs"},
    {"id": "marketing", "name": "Marketing"},
]

DOMAINS = [
    {"id": "game",      "name": "Game"},
    {"id": "app",       "name": "App"},
    {"id": "web",       "name": "Web"},
    {"id": "corporate", "name": "Corporate"},
    {"id": "cicd",      "name": "CI / CD"},
    {"id": "devops",    "name": "DevOps"},
    {"id": "tech",      "name": "Tech / Infra"},
    {"id": "content",   "name": "Content"},
]


# ── Helpers ─────────────────────────────────────────────────────────────────

# Inline question syntax: {Question text?}  — the text (incl. the ?) is the key.
# Same exact text in two places = one question, asked once, substituted everywhere.
_INLINE_Q_RE = re.compile(r"\{([^{}]+?\?)\}")


def _collect_inline_questions(strings: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for s in strings:
        if not s:
            continue
        for m in _INLINE_Q_RE.finditer(s):
            q = m.group(1).strip()
            if q in seen:
                continue
            seen.add(q)
            out.append(q)
    return out


def _t(id, name, type, domain, type_hint, actor, verb, z, x, for_conn, y,
       description, blocks=None, tasks=None, questions=None,
       os_field=False, version_field=False):
    block_ids = list(blocks or [])
    task_list = list(tasks or [])
    # Gather every string a user might see, so inline {question?} placeholders
    # found inside block tasks or ACs are surfaced alongside template-level ones.
    scan = [name, description, z, x, for_conn, y] + task_list
    for bid in block_ids:
        b = BLOCKS.get(bid)
        if not b:
            continue
        scan.append(b.get("name", ""))
        scan.append(b.get("description", ""))
        scan.extend(b.get("tasks", []))
        scan.extend(b.get("acs", []))
    inline_qs = _collect_inline_questions(scan)
    return dict(
        id=id, name=name, type=type, domain=domain, type_hint=type_hint,
        actor=actor, verb=verb, z=z, x=x, for_conn=for_conn, y=y,
        description=description,
        blocks=block_ids,
        tasks=task_list,
        questions=list(questions or []),
        inline_questions=inline_qs,
        os_field=os_field, version_field=version_field,
        # Resolved on demand by clients; included pre-resolved for convenience.
        acceptance=_resolve_acs(block_ids),
        # Legacy alias kept for older UI code.
        subtasks=task_list,
        category=name.split("—")[0].strip() if "—" in name else name,
    )


def _q(key, label, placeholder=""):
    return dict(key=key, label=label, placeholder=placeholder)


def _resolve_acs(block_ids: List[str]) -> List[str]:
    """Flatten the AC lists of the given blocks (in order, de-duped)."""
    seen = set()
    out: List[str] = []
    for bid in block_ids:
        b = BLOCKS.get(bid)
        if not b:
            continue
        for ac in b["acs"]:
            if ac in seen:
                continue
            seen.add(ac)
            out.append(ac)
    return out


# ── Templates ───────────────────────────────────────────────────────────────
#
# Question wording in each template field MUST exactly match the wording used
# in the referenced block's tasks / ACs — same text inside `{...?}` braces =
# one prompt that fills every occurrence.

TEMPLATES = [

    # ── GAME — ASSETS ─────────────────────────────────────────────────────
    _t("char-3d", "3D Character", "asset", "game", "Art",
       "User", "needs", "Seeing", "{character name?} in-game", "to",
       "have a character in the world",
       "Model, rig, animate and place {character name?} in-game. "
       "Under {how many tris for model?} tris.",
       blocks=["3d-base", "3d-rig-anim", "3d-place"]),

    _t("prop-3d", "3D Prop", "asset", "game", "Art",
       "User", "needs", "Seeing", "{prop name?} in-game", "to",
       "have an object in the world",
       "Model and place {prop name?} in-game. No rig or animation. "
       "Under {how many tris for model?} tris.",
       blocks=["3d-base", "3d-place"]),

    _t("env-kit", "Environment Kit", "asset", "game", "Art",
       "User", "needs", "Seeing", "a {kit name?} built in {target area?}", "to",
       "have a place in the world",
       "Modular {kit name?} kit ready to assemble {target area?}.",
       blocks=["env-modular"]),

    _t("vfx-shader", "VFX / Shader", "asset", "game", "Art",
       "User", "needs", "Seeing", "{effect name?} fire on {trigger?}", "to",
       "have visual flare in the world",
       "Author the {effect name?} VFX / shader, triggered by {trigger?}.",
       blocks=["shader-base"]),

    _t("audio-sfx", "Audio / SFX", "asset", "game", "Art",
       "User", "needs", "Hearing", "{sound name?}", "to",
       "have sound in the world",
       "Author SFX bundle for {sound name?} with {how many variations?} variations.",
       blocks=["audio-base"]),

    _t("video-trailer", "Trailer / Cinematic", "content", "game", "Art",
       "User", "needs", "Watching", "{video name?}", "to",
       "have a video about the product",
       "Produce the {video name?} video.",
       blocks=["video-base"]),

    # ── GAME — FEATURES / DESIGN ──────────────────────────────────────────
    _t("ui-screen", "UI Screen", "feature", "game", "Feature",
       "User", "needs", "Using", "the {screen name?} screen", "to",
       "have access to that feature",
       "Build the {screen name?} UI screen.",
       blocks=["ui-screen"]),

    _t("mechanic", "Gameplay Mechanic", "feature", "game", "Feature",
       "User", "needs", "Performing", "{mechanic name?}", "to",
       "have a new action available",
       "Design and build the {mechanic name?} mechanic.",
       blocks=["mechanic-design"]),

    _t("level-design", "Level Design", "design", "game", "Feature",
       "User", "needs", "Playing", "{level name?}", "to",
       "have a level to play",
       "Design and ship the {level name?} level.",
       blocks=["level-base"]),

    _t("balance-pass", "Balance / Tuning Pass", "design", "game", "Feature",
       "User", "needs", "Playing", "a re-tuned {system to rebalance?}", "to",
       "have balanced play",
       "Targeted balance pass on {system to rebalance?} to fix "
       "{symptom we are fixing?}.",
       blocks=["balance-base"]),

    _t("game-bug", "Game Bug", "bug", "game", "Bug",
       "User", "needs", "Playing", "without {what is broken?}", "to",
       "have a working game",
       "Reproduce and fix: {what is broken?}.",
       blocks=["bug-repro"],
       os_field=True, version_field=True),

    # ── WEB / APP — FEATURES ──────────────────────────────────────────────
    _t("web-page", "Web Page", "feature", "web", "Feature",
       "User", "needs", "Reading", "the {page path or name?} page", "to",
       "have the information on that page",
       "Build and ship the {page path or name?} page.",
       blocks=["web-page"]),

    _t("ui-component", "UI Component", "feature", "web", "Feature",
       "User", "needs", "Using", "the {component name?} across the product", "to",
       "have a consistent interface",
       "Build reusable {component name?} component.",
       blocks=["component-base"]),

    _t("api-endpoint", "API Endpoint", "feature", "app", "Feature",
       "User", "needs", "Calling", "{HTTP method?} {endpoint path?}", "to",
       "have the action that endpoint provides",
       "Ship the {HTTP method?} {endpoint path?} endpoint.",
       blocks=["api-base"]),

    _t("app-bug", "App / Web Bug", "bug", "app", "Bug",
       "User", "needs", "Using", "the product without {what is broken?}", "to",
       "have a working product",
       "Reproduce and fix: {what is broken?}.",
       blocks=["bug-repro"],
       os_field=True, version_field=True),

    # ── INFRA / DEVOPS / CI ───────────────────────────────────────────────
    _t("db-mig", "DB Migration", "chore", "tech", "Chore",
       "Admin", "needs", "Running", "data on {table or collection?} on the new schema", "to",
       "have the data ready for new features",
       "Schema migration for {table or collection?}.",
       blocks=["db-mig"]),

    _t("pipeline-step", "Pipeline Step", "feature", "cicd", "Chore",
       "Admin", "needs", "Running", "the {pipeline step name?} step in the {pipeline name?} pipeline", "to",
       "have an automated step in the process",
       "Add or harden the {pipeline step name?} step in the {pipeline name?} pipeline. "
       "Any pipeline \u2014 build, asset cook, lint, test, deploy.",
       blocks=["pipeline-base"]),

    _t("cicd-online", "CI/CD Online Build & Checks", "feature", "cicd", "Chore",
       "Admin", "needs", "Running", "{check name?} on every push in CI/CD", "to",
       "have automated checks online before merge",
       "Wire {check name?} into the cloud CI/CD runner. Gates merges, "
       "publishes build artefacts.",
       blocks=["cicd-online-base"]),

    _t("deploy-env", "Deploy Environment", "chore", "devops", "Chore",
       "Admin", "needs", "Deploying", "builds to {environment name?}", "to",
       "have a place to run them",
       "Stand up the {environment name?} deploy environment.",
       blocks=["deploy-base"]),

    _t("monitor-alert", "Monitoring / Alert", "chore", "devops", "Chore",
       "Admin", "needs", "Being paged", "when {what are we alerting on?}", "to",
       "have eyes on the system",
       "Configure alerting for {what are we alerting on?}.",
       blocks=["monitor-base"]),

    _t("incident-pm", "Incident Postmortem", "fix", "devops", "Bug",
       "Admin", "needs", "Reading", "the {incident name or date?} postmortem", "to",
       "have a record of what happened",
       "Postmortem for incident: {incident name or date?}.",
       blocks=["incident-base"]),

    _t("hotfix", "Hotfix (live)", "fix", "tech", "Bug",
       "User", "needs", "Using", "production without {what is broken?}", "to",
       "have a working product",
       "Targeted hotfix for {what is broken?}.",
       blocks=["hotfix-base"],
       version_field=True),

    # ── DESIGN / RESEARCH ─────────────────────────────────────────────────
    _t("brand-asset", "Brand Asset", "asset", "corporate", "Art",
       "Admin", "needs", "Shipping", "the {asset name?} brand asset", "to",
       "have the asset in the brand library",
       "Produce the {asset name?} brand asset for {where will this be used?}.",
       blocks=["brand-base"]),

    _t("ux-research", "UX Research Spike", "spike", "app", "Spike",
       "Admin", "needs", "Reading", "a written answer on {what are we trying to learn?}", "to",
       "have a basis for the decision",
       "Time-boxed research spike: {what are we trying to learn?}. "
       "Timebox: {timebox in days?} days.",
       blocks=["research-base"]),

    # ── CHORE / TECH-DEBT ─────────────────────────────────────────────────
    _t("refactor", "Tech-Debt Refactor", "chore", "tech", "Chore",
       "Admin", "needs", "Working", "in a cleaner {area to refactor?}", "to",
       "have a maintainable codebase",
       "Refactor pass on {area to refactor?}.",
       blocks=["refactor-base"]),

    _t("dep-upgrade", "Dependency Upgrade", "chore", "tech", "Chore",
       "Admin", "needs", "Running", "on {dependency?} {target version?}", "to",
       "have a current dependency",
       "Upgrade {dependency?} to {target version?}.",
       blocks=["deps-base"]),

    # ── DOCS / ONBOARDING ─────────────────────────────────────────────────
    _t("docs-page", "Documentation Page", "docs", "tech", "Docs",
       "User", "needs", "Reading", "the {doc topic?} doc", "to",
       "have a guide for {doc topic?}",
       "Write / refresh the {doc topic?} documentation page.",
       blocks=["docs-base"]),

    _t("onboarding", "Onboarding Doc", "docs", "corporate", "Docs",
       "User", "needs", "Following", "the {role or team?} onboarding", "to",
       "have a path into the team",
       "Onboarding guide for {role or team?}.",
       blocks=["onboarding-base"]),

    # ── MARKETING / CONTENT ───────────────────────────────────────────────
    _t("campaign", "Marketing Campaign", "marketing", "content", "Feature",
       "Admin", "needs", "Launching", "the {campaign name?} campaign", "to",
       "have the campaign in market",
       "Plan and ship the {campaign name?} campaign asset set.",
       blocks=["campaign-base"]),

    _t("social-series", "Social Post Series", "marketing", "content", "Feature",
       "Admin", "needs", "Running", "the {series name?} social series", "to",
       "have content in the feed",
       "Plan and schedule the {series name?} social series.",
       blocks=["social-base"]),
]


# ── Sanity check: every block id referenced must exist in BLOCKS ────────────
_missing = sorted({
    bid
    for t in TEMPLATES for bid in t["blocks"]
    if bid not in BLOCKS
})
if _missing:  # pragma: no cover — caught at import time during dev
    raise RuntimeError(
        "story_templates.py references unknown block ids: " + ", ".join(_missing)
    )
