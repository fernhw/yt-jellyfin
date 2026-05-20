/* stickers.js — board overlay stickers */
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

  // Map type → display html
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

  // ── Zoom-aware drag ──────────────────────────────────────────────────────
  // board.dataset.zoom is kept in sync by the zoom-control script in board.html.
  // sticker left/top percentages are relative to the NATURAL (pre-zoom) board
  // dimensions, but mouse coords are in viewport pixels.
  // Conversion: natural_offset = viewport_offset / zoom

  function getZoom() {
    return parseFloat(board.dataset.zoom || '1');
  }

  let dragging   = null;
  let dragOffset = { x: 0, y: 0 }; // viewport pixels from sticker visual top-left

  function startStickerDrag(sticker, e) {
    e.stopPropagation();
    dragging = sticker;
    const rect = sticker.getBoundingClientRect(); // always viewport coords
    dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    sticker.classList.add('sticker-dragging');
  }

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const z  = getZoom();
    const wr = board.getBoundingClientRect(); // wr.left/top are always viewport coords

    // Convert viewport offset to natural (pre-zoom) board coordinate space
    const nw = board.offsetWidth;   // natural width, unaffected by CSS zoom
    const nh = board.offsetHeight;  // natural height

    const bx = (e.clientX - wr.left - dragOffset.x) / z;
    const by = (e.clientY - wr.top  - dragOffset.y) / z;

    const x = (bx / nw) * 100;
    const y = (by / nh) * 100;

    dragging.style.left = Math.max(0, Math.min(94, x)) + '%';
    dragging.style.top  = Math.max(0, Math.min(94, y)) + '%';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    const id = dragging.dataset.id;
    const x  = parseFloat(dragging.style.left);
    const y  = parseFloat(dragging.style.top);
    dragging.classList.remove('sticker-dragging');
    dragging = null;
    if (!id) return;
    fetch(`/api/stickers/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
      body: JSON.stringify({ x, y, rotation: 0 }),
    });
  });

  // ── Wire existing stickers ───────────────────────────────────────────────
  function wireSticker(el) {
    el.addEventListener('mousedown', e => startStickerDrag(el, e));
    const del = el.querySelector('.sticker-del');
    if (del) {
      del.addEventListener('click', e => {
        e.stopPropagation();
        const id = del.dataset.id;
        fetch(`/api/stickers/${id}`, {
          method: 'DELETE',
          headers: { 'X-CSRF-Token': csrf() },
        }).then(r => { if (r.ok) el.remove(); });
      });
    }
  }

  document.querySelectorAll('.board-sticker').forEach(wireSticker);

  // ── Dropdown toggle ──────────────────────────────────────────────────────
  const menuToggle = document.getElementById('sticker-menu-toggle');
  const menu       = document.getElementById('sticker-menu');
  if (menuToggle && menu) {
    menuToggle.addEventListener('click', e => {
      e.stopPropagation();
      menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
    });
    document.addEventListener('click', () => { if (menu) menu.style.display = 'none'; });
    menu.addEventListener('click', e => e.stopPropagation());
  }

  // ── Add new sticker from dropdown ────────────────────────────────────────
  document.querySelectorAll('.sticker-btn[data-type]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (menu) menu.style.display = 'none';
      const type = btn.dataset.type;
      const x    = 10 + Math.random() * 40;
      const y    = 5  + Math.random() * 35;

      fetch('/api/stickers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() },
        body: JSON.stringify({
          project_id: parseInt(projectId),
          sprint: parseInt(sprint),
          type, x, y, rotation: 0,
        }),
      })
      .then(r => r.json())
      .then(data => {
        if (!data.id) return;
        const el      = document.createElement('div');
        el.className  = `board-sticker sticker-${type}`;
        el.dataset.id = data.id;
        el.style.left = x + '%';
        el.style.top  = y + '%';
        el.innerHTML  = (STICKER_HTML[type] || type) +
          `<button class="sticker-del" data-id="${data.id}" title="Remove">✕</button>`;
        layer.appendChild(el);
        wireSticker(el);
      });
    });
  });
})();
