/* stickers.js — board overlay stickers with card-attach support */
(function () {
  'use strict';

  console.log('[stickers] IIFE start');

  const csrf = () => {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  };

  const appRoot = (() => {
    const m = document.querySelector('meta[name="app-root"]');
    return m ? m.getAttribute('content') : '';
  })();

  const board = document.getElementById('board');
  const layer = document.getElementById('sticker-layer');
  console.log('[stickers] board:', board, 'layer:', layer);
  if (!board || !layer) { console.warn('[stickers] early exit — board or layer missing'); return; }

  const projectId = board.dataset.project;
  const sprint    = board.dataset.sprint;
  console.log('[stickers] projectId:', projectId, 'sprint:', sprint);

  const STICKER_HTML = {
    exclamation: '!',
    arrow:       '→',
    question:    '?',
    star:        '★',
    fire:        '🔥',
    eyes:        '👀',
    check:       '✓',
    blocked:     '✕',
    up:          '↑',
    note:        '#',
  };

  // ── Zoom helper ──────────────────────────────────────────────────────────────
  // Use getBoundingClientRect / zoom — avoids offsetWidth ambiguity inside
  // CSS-zoomed parents.
  function getZoom() {
    return parseFloat(board.dataset.zoom || '1');
  }

  // ── Drag state ───────────────────────────────────────────────────────────────
  var dragging   = null;
  var dragOffset = { x: 0, y: 0 }; // viewport px: mouse minus sticker top-left

  function startStickerDrag(sticker, e) {
    e.stopPropagation();
    e.preventDefault();

    // If sticker is currently inside a card, lift it to the free layer first
    var parentCard = sticker.parentElement &&
                     sticker.parentElement.closest
                       ? sticker.parentElement.closest('.board-card')
                       : null;
    if (parentCard || sticker.classList.contains('sticker-on-card')) {
      var sRect = sticker.getBoundingClientRect();
      var bRect = board.getBoundingClientRect();
      var z     = getZoom();
      var nw    = bRect.width  / z;
      var nh    = bRect.height / z;
      var natX  = (sRect.left - bRect.left) / z;
      var natY  = (sRect.top  - bRect.top)  / z;

      sticker.classList.remove('sticker-on-card');
      sticker.style.left = Math.max(0, natX / nw * 100) + '%';
      sticker.style.top  = Math.max(0, natY / nh * 100) + '%';
      layer.appendChild(sticker);
    }

    dragging = sticker;
    var rect   = sticker.getBoundingClientRect();
    dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    sticker.classList.add('sticker-dragging');
  }

  document.addEventListener('mousemove', function (e) {
    if (!dragging) return;
    var z     = getZoom();
    var bRect = board.getBoundingClientRect();
    var nw    = bRect.width  / z;
    var nh    = bRect.height / z;
    var bx    = (e.clientX - bRect.left - dragOffset.x) / z;
    var by    = (e.clientY - bRect.top  - dragOffset.y) / z;

    dragging.style.left = Math.max(0, Math.min(95, bx / nw * 100)) + '%';
    dragging.style.top  = Math.max(0, Math.min(95, by / nh * 100)) + '%';
  });

  document.addEventListener('mouseup', function (e) {
    if (!dragging) return;
    var sticker = dragging;
    dragging    = null;
    sticker.classList.remove('sticker-dragging');

    var id = sticker.dataset.id;

    // Find what's under the sticker's centre (hide temporarily for hit-test)
    var sRect = sticker.getBoundingClientRect();
    var cx    = sRect.left + sRect.width  / 2;
    var cy    = sRect.top  + sRect.height / 2;
    sticker.style.display = 'none';
    var hit  = document.elementFromPoint(cx, cy);
    sticker.style.display = '';

    var targetCard = hit ? hit.closest('.board-card') : null;

    if (targetCard) {
      // ── Attach to card ───────────────────────────────────────────────────
      var z      = getZoom();
      var cr     = targetCard.getBoundingClientRect();
      var cardNW = cr.width  / z;
      var cardNH = cr.height / z;
      var cardX  = Math.round(((sRect.left - cr.left) / z) / cardNW * 100);
      var cardY  = Math.round(((sRect.top  - cr.top)  / z) / cardNH * 100);

      sticker.style.left = cardX + '%';
      sticker.style.top  = cardY + '%';
      sticker.classList.add('sticker-on-card');
      targetCard.appendChild(sticker);

      if (id) {
        fetch(appRoot + '/api/stickers/' + id, {
          method:  'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
          body:    JSON.stringify({
            card_story_id: parseInt(targetCard.dataset.storyId, 10),
            card_x: cardX,
            card_y: cardY,
          }),
        });
      }
    } else {
      // ── Free on board ────────────────────────────────────────────────────
      var x = parseFloat(sticker.style.left);
      var y = parseFloat(sticker.style.top);
      if (id) {
        fetch(appRoot + '/api/stickers/' + id, {
          method:  'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
          body:    JSON.stringify({ card_story_id: null, x: x, y: y, rotation: 0 }),
        });
      }
    }
  });

  // ── Single shared tooltip for all stickers ───────────────────────────────
  var _tip = document.createElement('div');
  _tip.className = 'sticker-tooltip';
  _tip.style.display = 'none';
  document.body.appendChild(_tip);

  function _showTip(text, mx, my) {
    _tip.textContent = text;
    _tip.style.display = 'block';
    _positionTip(mx, my);
  }
  function _positionTip(mx, my) {
    var tw = _tip.offsetWidth  || 140;
    var th = _tip.offsetHeight || 24;
    var x  = mx + 14;
    var y  = my - th - 8;
    if (x + tw + 4 > window.innerWidth)  x = mx - tw - 10;
    if (y < 4) y = my + 16;
    _tip.style.left = Math.max(4, x) + 'px';
    _tip.style.top  = Math.max(4, y) + 'px';
  }
  function _hideTip() { _tip.style.display = 'none'; }

  // ── Wire a sticker ───────────────────────────────────────────────────────────
  function wireSticker(el) {
    el.addEventListener('mousedown', function (e) { _hideTip(); startStickerDrag(el, e); });

    // ── Hover tooltip: who placed it ──────────────────────────────────────────
    el.addEventListener('mouseenter', function (e) {
      var creator = el.dataset.creator;
      if (!creator) return;
      _showTip('📌 Placed by ' + creator, e.clientX, e.clientY);
    });
    el.addEventListener('mousemove', function (e) {
      if (_tip.style.display === 'none') return;
      _positionTip(e.clientX, e.clientY);
    });
    el.addEventListener('mouseleave', _hideTip);

    var del = el.querySelector('.sticker-del');
    if (del) {
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        var sid = del.dataset.id;
        fetch(appRoot + '/api/stickers/' + sid, {
          method:  'DELETE',
          headers: { 'X-CSRF-Token': csrf() },
        }).then(function (r) { if (r.ok) el.remove(); });
      });
    }
  }

  document.querySelectorAll('.board-sticker').forEach(wireSticker);
  // Hide tooltip on any scroll or window blur
  document.addEventListener('scroll', _hideTip, true);
  window.addEventListener('blur', _hideTip);

  // ── Dropdown toggle ──────────────────────────────────────────────────────────
  var menuToggle = document.getElementById('sticker-menu-toggle');
  var menu       = document.getElementById('sticker-menu');
  if (menuToggle && menu) {
    menuToggle.addEventListener('click', function (e) {
      e.stopPropagation();
      menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
    });
    document.addEventListener('click', function () { if (menu) menu.style.display = 'none'; });
    menu.addEventListener('click', function (e) { e.stopPropagation(); });
  }

  // ── Fullscreen sticker dropdown toggle ───────────────────────────────────────
  var fsTog  = document.getElementById('fs-sticker-toggle');
  var fsMenu = document.getElementById('fs-sticker-menu');
  if (fsTog && fsMenu) {
    fsTog.addEventListener('click', function (e) {
      e.stopPropagation();
      fsMenu.style.display = fsMenu.style.display === 'none' ? 'block' : 'none';
    });
    document.addEventListener('click', function () { if (fsMenu) fsMenu.style.display = 'none'; });
    fsMenu.addEventListener('click', function (e) { e.stopPropagation(); });
  }

  // ── Create new sticker ───────────────────────────────────────────────────────
  const stickerBtns = document.querySelectorAll('.sticker-btn[data-type]');
  console.log('[stickers] sticker buttons found:', stickerBtns.length);
  stickerBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      console.log('[stickers] btn click fired, type:', btn.dataset.type);
      if (menu)   menu.style.display   = 'none';
      if (fsMenu) fsMenu.style.display = 'none';
      var type = btn.dataset.type;
      var x    = 8  + Math.random() * 40;
      var y    = 4  + Math.random() * 35;

      fetch(appRoot + '/api/stickers', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
        body:    JSON.stringify({
          project_id: parseInt(projectId, 10),
          sprint:     parseInt(sprint, 10),
          type: type, x: x, y: y, rotation: 0,
        }),
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.id) return;
        var el      = document.createElement('div');
        el.className    = 'board-sticker sticker-' + type;
        el.dataset.id      = data.id;
        el.dataset.creator = data.creator_name || '';
        el.style.left   = x + '%';
        el.style.top  = y + '%';
        el.innerHTML  = (STICKER_HTML[type] || type) +
          '<button class="sticker-del" data-id="' + data.id + '" title="Remove">\u2715</button>';
        layer.appendChild(el);
        wireSticker(el);
      });
    });
  });
}());
