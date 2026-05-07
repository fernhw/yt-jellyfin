#!/usr/bin/env python3
"""
showSchedulerCards.py — Render HTML fragments for the show-scheduler sections.

Modes:
  python3 showSchedulerCards.py today  <showSchedulerToday.json>
      → cards for episodes downloaded today

  python3 showSchedulerCards.py status <showSchedulerStatus.json>
      → tracker rows for all scheduled shows

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
    rows = []
    for s in data.get('shows', []):
        show      = html_mod.escape(s.get('show', ''))
        season    = s.get('season', 1)
        dl        = s.get('downloaded', 0)
        total     = s.get('total', 0)
        next_ep   = s.get('next_ep', dl + 1)
        week      = html_mod.escape(s.get('week', '?'))
        status    = s.get('status', 'pending')
        days      = html_mod.escape(s.get('release_days', ''))
        thumb     = html_mod.escape(s.get('thumb', ''))

        # Progress bar: downloaded / total
        pct = int(dl / total * 100) if total > 0 else 0
        if '✓' in week:
            week_cls = 'trk-week trk-done'
        elif 'due' in week:
            week_cls = 'trk-week trk-due'
        else:
            week_cls = 'trk-week'

        if thumb:
            fallback = '<div class=\\"trk-nothumb\\"></div>'
            img_html = (
                f'<img class="trk-thumb" src="{thumb}" '
                f'onerror="this.outerHTML=\'{fallback}\'" alt="">'
            )
        else:
            img_html = '<div class="trk-nothumb"></div>'

        ep_text = f'S{season:02d} · Ep {dl}/{total}' if dl > 0 else f'S{season:02d} · none yet'

        rows.append(
            f'<div class="trk-row">'
            f'{img_html}'
            f'<div class="trk-info">'
            f'<div class="trk-name">{show}</div>'
            f'<div class="trk-eps">{ep_text}</div>'
            f'<div class="trk-bar"><div class="trk-fill" style="width:{pct}%"></div></div>'
            f'</div>'
            f'<div class="{week_cls}">{week}<br><span class="trk-days">{days}</span></div>'
            f'</div>'
        )
    return ''.join(rows)


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
