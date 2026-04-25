#!/bin/bash
# webTemplates.sh — HTML/CSS templates for media library sections
# Source this file in reportMaker.sh before building HTML.
# Provides: MEDIA_CSS, build_media_sections_html()

# ── CSS ───────────────────────────────────────────────────────────────────────
MEDIA_CSS=$(cat <<'MEDIA_CSS_EOF'
    .media-bar{display:flex;align-items:center;gap:8px;margin-bottom:8px}
    .media-bar-label{font:600 .68rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#8fa0b1}
    .media-bar-note{font:500 .62rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#4a5a68}
    .media-scroll{max-height:40vh;overflow-y:auto;-webkit-overflow-scrolling:touch}
    .media-secondary{display:flex;gap:8px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:2px;margin-bottom:4px}
    .msec{flex:0 0 auto;background:rgba(14,18,23,.5);border:1px solid rgba(255,255,255,.05);border-radius:12px;padding:9px 10px 10px}
    .msec-head{display:flex;align-items:center;gap:5px;margin-bottom:8px}
    .msec-icon{font-size:.7rem;opacity:.5;flex-shrink:0}
    .msec-head h3{font:600 .6rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#536170}
    .mcards{display:flex;gap:6px;overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:2px}
    .mcard{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;display:flex;flex-direction:column;flex:0 0 64px;width:64px;height:124px;text-decoration:none;color:inherit}
    .mcard:active{border-color:#5c6977}
    .mcard img{width:100%;flex:1;min-height:0;object-fit:cover;display:block;background:var(--panel-soft)}
    .mcard-nothumb{width:100%;flex:1;min-height:0;background:var(--panel-soft);display:flex;align-items:center;justify-content:center;font-size:.9rem;color:rgba(255,255,255,.15)}
    .mcard-body{padding:4px 5px 5px;display:grid;gap:1px}
    .mcard-title{font-size:.58rem;line-height:1.2;color:#b0bfcc;font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .mcard-sub{font:500 .54rem/1.1 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    #ptr{position:fixed;top:env(safe-area-inset-top);left:0;right:0;z-index:100;display:flex;align-items:center;justify-content:center;height:0;overflow:hidden;background:rgba(12,15,19,.9);transition:height .2s}
    #ptr.ready{border-bottom:1px solid rgba(255,255,255,.08)}
    #ptr-label{color:#c2a365;font:600 .72rem/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;letter-spacing:.1em;text-transform:uppercase}
MEDIA_CSS_EOF
)

# ── Helpers ───────────────────────────────────────────────────────────────────
_mcard_html() {
  local cat="$1" title="$2" sub="$3" thumb="$4" url="$5"
  local thtml

  if [ -n "$thumb" ]; then
    case "$cat" in
      show)  _fe='&#9654;' ;;
      movie) _fe='&#127909;' ;;
      music) _fe='&#9835;' ;;
      book)  _fe='&#128218;' ;;
      manga) _fe='&#128217;' ;;
      *)     _fe='&#9632;' ;;
    esac
    thtml="<img src=\"${thumb}\" onerror=\"this.outerHTML='<div class=&quot;mcard-nothumb&quot;>${_fe}</div>'\" alt=\"\">"
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

  if [ -n "$url" ]; then
    printf '<a class="mcard" href="#" data-app-urls="%s">%s<div class="mcard-body"><div class="mcard-title">%s</div>%s</div></a>' \
      "$url" "$thtml" "$title" "$subhtml"
  else
    printf '<div class="mcard">%s<div class="mcard-body"><div class="mcard-title">%s</div>%s</div></div>' \
      "$thtml" "$title" "$subhtml"
  fi
}

# cap_cards <html_list> <max_count> → truncated html + optional "…+N more" note
_cap_cards() {
  local cards="$1" max="$2"
  # count by splitting on '<div class="mcard'
  local count; count=$(printf '%s' "$cards" | grep -o 'class="mcard"' | wc -l | tr -d ' ')
  if [ "$count" -le "$max" ]; then
    printf '%s' "$cards"
    return
  fi
  # take only first $max cards
  local out="" n=0
  while IFS= read -r chunk; do
    [ -z "$chunk" ] && continue
    n=$((n+1))
    [ "$n" -gt "$max" ] && break
    out="${out}<div class=\"mcard\"${chunk}"
  done <<CARDS_EOF
$(printf '%s' "$cards" | sed 's/<div class="mcard">/\n/g')
CARDS_EOF
  local extra=$((count - max))
  [ "$extra" -gt 0 ] && out="${out}<div class=\"mcard-more\">+${extra}</div>"
  printf '%s' "$out"
}

_msection_html() {
  local label="$1" icon="$2" cards="$3"
  [ -z "$cards" ] && return
  printf '<section class="msec"><div class="msec-head"><span class="msec-icon">%s</span><h3>%s</h3></div><div class="mcards">%s</div></section>' \
    "$icon" "$label" "$cards"
}

# ── Main builder: call after mediaScan.sh has run ────────────────────────────
build_media_sections_html() {
  local scan_file="$1" jf_url="$2" abs_url="$3" still_url="$4" finer_url="$5"
  [ -f "$scan_file" ] && [ -s "$scan_file" ] || return

  local fs; fs=$(printf '\037')
  local shows="" movies="" music="" books="" manga=""
  local shows_c=0 movies_c=0 music_c=0 books_c=0 manga_c=0
  local MAX=5

  while IFS="$fs" read -r cat title subtitle thumb mtime; do
    [ -z "$cat" ] && continue
    case "$cat" in
      show)
        [ "$shows_c" -ge "$MAX" ] && continue
        local kind ep_label sub_text
        kind=$(printf '%s' "$subtitle" | cut -d: -f1)
        ep_label=$(printf '%s' "$subtitle" | cut -d: -f2-)
        [ "$kind" = "newshow" ] && sub_text="New Show${ep_label:+ · $ep_label}" || sub_text="New Eps${ep_label:+ · $ep_label}"
        shows="${shows}$(_mcard_html "$cat" "$title" "$sub_text" "$thumb" "$jf_url")"
        shows_c=$((shows_c+1))
        ;;
      movie)
        [ "$movies_c" -ge "$MAX" ] && continue
        movies="${movies}$(_mcard_html "$cat" "$title" "New Movie" "$thumb" "$jf_url")"
        movies_c=$((movies_c+1))
        ;;
      music) [ "$music_c" -lt "$MAX" ] && { music="${music}$(_mcard_html "$cat" "$title" "$subtitle" "$thumb" "$finer_url")"; music_c=$((music_c+1)); } ;;
      book)  [ "$books_c" -lt "$MAX" ] && { books="${books}$(_mcard_html "$cat" "$title" "$subtitle" "$thumb" "$abs_url")";   books_c=$((books_c+1)); } ;;
      manga) [ "$manga_c" -lt "$MAX" ] && { manga="${manga}$(_mcard_html "$cat" "$title" "$subtitle" "$thumb" "$still_url")"; manga_c=$((manga_c+1)); } ;;
    esac
  done < "$scan_file"

  [ -z "$shows$movies$music$books$manga" ] && return

  local sections=""
  local ms; ms=$(_msection_html "Shows"      "&#9654;"   "$shows");  sections="${sections}${ms}"
  ms=$(_msection_html "Movies"     "&#127909;" "$movies"); sections="${sections}${ms}"
  ms=$(_msection_html "Music"      "&#9835;"   "$music");  sections="${sections}${ms}"
  ms=$(_msection_html "Audiobooks" "&#128218;" "$books");  sections="${sections}${ms}"
  ms=$(_msection_html "Manga"      "&#128217;" "$manga");  sections="${sections}${ms}"

  printf '<div class="media-bar"><span class="media-bar-label">&#9654;&#xFE0E; Library</span><span class="media-bar-note">last 30 hours</span></div><div class="media-scroll"><div class="media-secondary">%s</div></div>' "$sections"
}
