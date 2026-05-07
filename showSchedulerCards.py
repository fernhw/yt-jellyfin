#!/usr/bin/env python3
"""
showSchedulerCards.py — Render HTML fragments for the show-scheduler sections.

Modes:
  python3 showSchedulerCards.py today  <showSchedulerToday.json>
      → cards for episodes downloaded today

  python3 showSchedulerCards.py status <showSchedulerStatus.json>
      → horizontal scroll tracker cards for all scheduled shows

Outputs raw HTML to stdout.
"""
import html as html_mod
import json
import sys


def render_today(data: dict) -> str:
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
    return ''.join(parts)


def render_status(data: dict) -> str:
    cards = []
    for s in data.get('shows', []):
        show        = html_mod.escape(s.get('show', ''))
        season      = s.get('season', 1)
        dl          = s.get('downloaded', 0)
        total       = s.get('total', 0)
        next_ep     = s.get('next_ep', dl + 1)
        week        = s.get('week', '')
        days_until  = s.get('days_until')        # may be None
        is_complete = s.get('is_complete', False) or (total > 0 and dl >= total)
        thumb       = html_mod.escape(s.get('thumb', ''))

        # Flags
        is_new   = '✓' in week and not is_complete
        is_final = (not is_complete) and (next_ep >= total > 0)

        # Progress
        pct = int(dl / total * 100) if total > 0 else 0

        # Poster HTML
        if thumb:
            poster_inner = f'<img src="{thumb}" onerror="this.remove()" alt="">'
        else:
            poster_inner = ''

        # Badge — priority: done > new > final
        if is_complete:
            badge = '<span class="trk-badge trk-badge-done">done</span>'
        elif is_new:
            badge = '<span class="trk-badge trk-badge-new">new</span>'
        elif is_final:
            badge = '<span class="trk-badge trk-badge-final">final ep</span>'
        else:
            badge = ''

        # Episode text
        ep_text = f'S{season:02d} &middot; {dl}/{total}'

        # Countdown text
        if is_complete:
            next_text = 'complete'
            next_cls  = 'trk-card-next'
        elif days_until is None:
            next_text = ''
            next_cls  = 'trk-card-next'
        elif days_until == 0:
            next_text = 'today'
            next_cls  = 'trk-card-next trk-next-today'
        elif days_until == 1:
            next_text = 'tomorrow'
            next_cls  = 'trk-card-next trk-next-soon'
        else:
            next_text = f'in {days_until} days'
            next_cls  = 'trk-card-next'

        cards.append(
            f'<div class="trk-card">'
            f'<div class="trk-poster">{poster_inner}{badge}</div>'
            f'<div class="trk-card-name" title="{show}">{show}</div>'
            f'<div class="trk-card-eps">{ep_text}</div>'
            f'<div class="trk-bar"><div class="trk-fill" style="width:{pct}%"></div></div>'
            f'<div class="{next_cls}">{next_text}</div>'
            f'</div>'
        )
    return ''.join(cards)


def main() -> None:
    if len(sys.argv) < 3:
        # Legacy single-arg call: treat as "today" mode
        if len(sys.argv) == 2:
            try:
                with open(sys.argv[1]) as f:
                    data = json.load(f)
                print(render_today(data), end='')
            except Exception:
                pass
        sys.exit(0)

    mode, path = sys.argv[1], sys.argv[2]
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        sys.exit(1)

    if mode == 'today':
        print(render_today(data), end='')
    elif mode == 'status':
        print(render_status(data), end='')


if __name__ == '__main__':
    main()

