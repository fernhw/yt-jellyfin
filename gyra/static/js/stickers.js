/* stickers.js — board overlay stickers with card-attach support */
(function () {
  'use strict';

  const csrf = () => {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  };

  const board = document.getElementById('board');
  const layer = document.getElementById('sticker-layer');
  if (!board || !layer) return;

  const projectId = board.dataset.project;
  const sprint    = board.dataset.sprint;

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
        fetch('/api/stickers/' + id, {
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
        fetch('/api/stickers/' + id, {
          method:  'PATCH',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
          body:    JSON.stringify({ card_story_id: null, x: x, y: y, rotation: 0 }),
        });
      }
    }
  });

  // ── Wire a sticker ───────────────────────────────────────────────────────────
  function wireSticker(el) {
    el.addEventListener('mousedown', function (e) { startStickerDrag(el, e); });
    var del = el.querySelector('.sticker-del');
    if (del) {
      del.addEventListener('click', function (e) {
        e.stopPropagation();
        var sid = del.dataset.id;
        fetch('/api/stickers/' + sid, {
          method:  'DELETE',
          headers: { 'X-CSRF-Token': csrf() },
        }).then(function (r) { if (r.ok) el.remove(); });
      });
    }
  }

  document.querySelectorAll('.board-sticker').forEach(wireSticker);

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

  // ── Create new sticker ───────────────────────────────────────────────────────
  document.querySelectorAll('.sticker-btn[data-type]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (menu) menu.style.display = 'none';
      var type = btn.dataset.type;
      var x    = 8  + Math.random() * 40;
      var y    = 4  + Math.random() * 35;

      fetch('/api/stickers', {
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
        el.className  = 'board-sticker sticker-' + type;
        el.dataset.id = data.id;
        el.style.left = x + '%';
        el.style.top  = y + '%';
        el.innerHTML  = (STICKER_HTML[type] || type) +
          '<button class="sticker-del" data-id="' + data.id + '" title="Remove">\u2715</button>';
        layer.appendChild(el);
        wireSticker(el);
      });
    });
  });
}());
