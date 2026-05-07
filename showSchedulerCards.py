#!/usr/bin/env python3
"""
showSchedulerCards.py — Render HTML card fragments for show-scheduler episodes.
Called by reportMaker.sh to avoid quoting nightmares with inline heredocs.

Usage: python3 showSchedulerCards.py <showSchedulerToday.json>
Outputs raw HTML card fragments to stdout (no newline at end).
"""
import html as html_mod
import json
import sys

def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(1)
    try:
        with open(sys.argv[1]) as f:
            data = json.load(f)
    except Exception:
        sys.exit(1)

    parts = []
    for ep in data.get('episodes', []):
        show   = html_mod.escape(ep.get('show', ''))
        epcode = html_mod.escape(ep.get('ep', ''))
        total  = ep.get('total', '?')
        thumb  = html_mod.escape(ep.get('thumb', ''))

        if thumb:
            fallback = '<div class=\\"mcard-nothumb\\">&#9654;</div>'
            t_img = (
                f'<img src="{thumb}" '
                f'onerror="this.outerHTML=\'{fallback}\'" alt="">'
            )
        else:
            t_img = '<div class="mcard-nothumb">&#9654;</div>'

        parts.append(
            f'<a class="card-link sched-card" href="#" '
            f'data-app-urls="org.jellyfin.expo://|jellyfin://|https://jellyfin.fernhw.com" '
            f'aria-label="Open {show} {epcode}">'
            f'<article class="mcard">'
            f'{t_img}'
            f'<div class="mcard-body">'
            f'<div class="mcard-title">{show}</div>'
            f'<div class="mcard-sub">{epcode} / {total}</div>'
            f'</div>'
            f'</article></a>'
        )

    print(''.join(parts), end='')

if __name__ == '__main__':
    main()
