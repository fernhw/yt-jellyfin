#!/usr/bin/env python3
"""
fix_titles.py — Fix all-caps titles in wiki .md files.

Fixes both `title=` in [META] and the first `# Heading` in the article body.

Usage:
  python3 fix_titles.py           # dry run — prints what would change
  python3 fix_titles.py --apply   # apply changes in-place
"""

import os
import re
import sys

ARTICLES_DIR = os.path.join(os.path.dirname(__file__), "wiki_content", "AMY", "articles")

# Words that should stay lowercase in title case (unless first/last word)
SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "if", "in",
               "nor", "of", "on", "or", "so", "the", "to", "up", "via", "yet"}

# Known abbreviations / proper nouns that must stay exactly as given
PRESERVE_EXACT = {
    "WIP]", "WIP", "ROI", "DLC", "AMY", "NPC", "NPC's", "RPG", "UI", "UX",
    "AENavigator", "SYSM", "VIP", "ARG", "CEO",
}

# Trailing punctuation to strip from title (colons, trailing spaces)
TRAILING_STRIP = re.compile(r"[:\s]+$")

# A title needs fixing if applying smart_title to it would produce a different result
def needs_fix(text: str) -> bool:
    if len(text.strip()) < 2:
        return False
    return smart_title(text) != text


def smart_title(text: str) -> str:
    """Convert a string to proper title case with small-word rules."""
    # Strip trailing colon/spaces
    text = TRAILING_STRIP.sub("", text).strip()

def smart_title(text: str) -> str:
    """Convert a string to proper title case — every word capitalised."""
    # Strip trailing colon/spaces
    text = TRAILING_STRIP.sub("", text).strip()

    words = text.split()
    result = []
    for word in words:
        # Split off leading/trailing punctuation to capitalise just the inner word
        m = re.match(r"^([^\w']*)(\w[\w']*)([^\w']*)$", word)
        if m:
            prefix, inner, suffix = m.group(1), m.group(2), m.group(3)
        else:
            prefix, inner, suffix = "", word, ""

        # Preserve known abbreviations / proper nouns exactly
        if inner in PRESERVE_EXACT or inner.upper() in {p.upper() for p in PRESERVE_EXACT}:
            result.append(word)
            continue

        word_no_punct = re.sub(r"[^\w']", "", word)
        if word_no_punct.upper() in {p.upper() for p in PRESERVE_EXACT}:
            result.append(word)
            continue

        # Capitalise every word — no small-word exceptions for wiki hierarchy
        result.append(prefix + inner.capitalize() + suffix)

    return " ".join(result)


def fix_file(path: str, apply: bool) -> list[tuple[str, str]]:
    """Return list of (old_line, new_line) changes. If apply=True, write file."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()

    changes = []
    new_lines = []
    h1_fixed = False  # only fix the first h1 per file

    for line in lines:
        stripped = line.rstrip("\n")

        # Fix title= in [META]
        if stripped.startswith("title="):
            value = stripped[6:]
            if needs_fix(value):
                new_value = smart_title(value)
                new_line = "title=" + new_value + "\n"
                if new_line.rstrip("\n") != stripped:
                    changes.append((stripped, new_line.rstrip("\n")))
                    new_lines.append(new_line)
                    continue

        # Fix first # heading in the body
        if not h1_fixed and stripped.startswith("# "):
            value = stripped[2:]
            if needs_fix(value):
                new_value = smart_title(value)
                new_line = "# " + new_value + "\n"
                if new_line.rstrip("\n") != stripped:
                    changes.append((stripped, new_line.rstrip("\n")))
                    new_lines.append(new_line)
                    h1_fixed = True
                    continue
            h1_fixed = True  # already correct or no fix needed

        new_lines.append(line)

    if apply and changes:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    return changes


def main():
    apply = "--apply" in sys.argv

    if not apply:
        print("DRY RUN — pass --apply to write changes\n")

    md_files = sorted(
        os.path.join(ARTICLES_DIR, f)
        for f in os.listdir(ARTICLES_DIR)
        if f.endswith(".md")
    )

    total_files = 0
    total_changes = 0

    for path in md_files:
        changes = fix_file(path, apply)
        if changes:
            total_files += 1
            total_changes += len(changes)
            print(f"\n{'FIXED' if apply else 'WOULD FIX'}: {os.path.basename(path)}")
            for old, new in changes:
                print(f"  - {old!r}")
                print(f"  + {new!r}")

    print(f"\n{'Applied' if apply else 'Pending'}: {total_changes} change(s) across {total_files} file(s).")


if __name__ == "__main__":
    main()
