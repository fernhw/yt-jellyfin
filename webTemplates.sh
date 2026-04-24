#!/bin/bash
# webTemplates.sh — HTML/CSS templates for media library sections
# Source this file in reportMaker.sh before building HTML.
# Provides: MEDIA_CSS, build_media_sections_html()

# ── CSS ───────────────────────────────────────────────────────────────────────
MEDIA_CSS=$(cat <<'MEDIA_CSS_EOF'
    .media-head-bar{display:flex;align-items:baseline;gap:10px;margin-top:28px;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,.07)}
    .media-head-bar h2{font-size:1.1rem;color:#d8e1ea;font-weight:600}
    .media-head-bar span{font:500 .72rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--muted)}
    .media-sections{display:grid;gap:14px}
    .msec{background:var(--panel-tint);border:1px solid rgba(255,255,255,.06);border-radius:22px;padding:18px 20px 20px;box-shadow:0 18px 60px rgba(0,0,0,.12)}
    .msec-head{display:flex;align-items:center;gap:8px;margin-bottom:14px}
    .msec-icon{font-size:.9rem;opacity:.75;flex-shrink:0}
    .msec-head h3{font:600 .74rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.18em;text-transform:uppercase;color:#8fa0b1}
    .mcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px}
    .mcard{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column}
    .mcard img{width:100%;aspect-ratio:2/3;object-fit:cover;display:block;background:var(--panel-soft)}
    .mcard img.sq{aspect-ratio:1}
    .mcard-nothumb{width:100%;aspect-ratio:2/3;background:var(--panel-soft);display:flex;align-items:center;justify-content:center;font-size:1.6rem;color:rgba(255,255,255,.2)}
    .mcard-body{padding:7px 9px 9px;display:grid;gap:2px}
    .mcard-title{font-size:.78rem;line-height:1.25;color:#f0f4f8;font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .mcard-sub{font:500 .66rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    @media(max-width:720px){.mcards{grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px}.msec{padding:14px}}
MEDIA_CSS_EOF
)

# ── Helpers ───────────────────────────────────────────────────────────────────
_mcard_html() {
  local cat="$1" title="$2" sub="$3" thumb="$4"
  local thtml

  if [ -n "$thumb" ]; then
    case "$cat" in
      music) thtml="<img class=\"sq\" src=\"${thumb}\" onerror=\"this.style.display='none'\" alt=\"\">" ;;
      *)     thtml="<img src=\"${thumb}\" onerror=\"this.style.display='none'\" alt=\"\">" ;;
    esac
  else
    case "$cat" in
      show)  thtml="<div class=\"mcard-nothumb\">&#9654;</div>" ;;
      movie) thtml="<div class=\"mcard-nothumb\">&#127909;</div>" ;;
      music) thtml="<div class=\"mcard-nothumb\">&#9835;</div>" ;;
      book)  thtml="<div class=\"mcard-nothumb\">&#128218;</div>" ;;
      manga) thtml="<div class=\"mcard-nothumb\">&#128217;</div>" ;;
      *)     thtml="<div class=\"mcard-nothumb\">&#9632;</div>" ;;
    esac
  fi

  local subhtml=""
  [ -n "$sub" ] && subhtml="<div class=\"mcard-sub\">${sub}</div>"

  printf '<div class="mcard">%s<div class="mcard-body"><div class="mcard-title">%s</div>%s</div></div>' \
    "$thtml" "$title" "$subhtml"
}

_msection_html() {
  local label="$1" icon="$2" cards="$3"
  [ -z "$cards" ] && return
  printf '<section class="msec"><div class="msec-head"><span class="msec-icon">%s</span><h3>%s</h3></div><div class="mcards">%s</div></section>' \
    "$icon" "$label" "$cards"
}

# ── Main builder: call after mediaScan.sh has run ────────────────────────────
build_media_sections_html() {
  local scan_file="$1"
  [ -f "$scan_file" ] && [ -s "$scan_file" ] || return

  local fs; fs=$(printf '\037')
  local shows="" movies="" music="" books="" manga=""

  while IFS="$fs" read -r cat title subtitle thumb mtime; do
    [ -z "$cat" ] && continue
    local card; card=$(_mcard_html "$cat" "$title" "$subtitle" "$thumb")
    case "$cat" in
      show)  shows="${shows}${card}" ;;
      movie) movies="${movies}${card}" ;;
      music) music="${music}${card}" ;;
      book)  books="${books}${card}" ;;
      manga) manga="${manga}${card}" ;;
    esac
  done < "$scan_file"

  [ -z "$shows$movies$music$books$manga" ] && return

  local out=""
  out="${out}$(_msection_html "TV Shows"    "&#9654;&#xFE0E;" "$shows")"
  out="${out}$(_msection_html "Movies"      "&#127909;"       "$movies")"
  out="${out}$(_msection_html "Music"       "&#9835;"         "$music")"
  out="${out}$(_msection_html "Audiobooks"  "&#128218;"       "$books")"
  out="${out}$(_msection_html "Manga"       "&#128217;"       "$manga")"

  printf '<div class="media-head-bar"><h2>Library Updates</h2><span>new since last scan</span></div><div class="media-sections">%s</div>' "$out"
}
