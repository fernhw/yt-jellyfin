/* stickers.js — board overlay stickers (arrows + exclamation) */
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

  // ── Drag-move sticker ────────────────────────────────────────────────────
  let dragging   = null;
  let dragOffset = { x: 0, y: 0 };

  function pct(px, dim) { return (px / dim) * 100; }

  function startStickerDrag(sticker, e) {
    e.stopPropagation();
    dragging = sticker;
    const rect = sticker.getBoundingClientRect();
    dragOffset = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    sticker.classList.add('sticker-dragging');
  }

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const wr = board.getBoundingClientRect(); // use board, not wrap
    const x  = pct(e.clientX - wr.left - dragOffset.x, wr.width);
    const y  = pct(e.clientY - wr.top  - dragOffset.y, wr.height);
    dragging.style.left = Math.max(0, Math.min(95, x)) + '%';
    dragging.style.top  = Math.max(0, Math.min(95, y)) + '%';
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

  // ── Add new sticker from toolbar ─────────────────────────────────────────
  document.querySelectorAll('.sticker-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const type = btn.dataset.type;
      const x    = 10 + Math.random() * 40;
      const y    = 10 + Math.random() * 40;

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
        const el        = document.createElement('div');
        el.className    = `board-sticker sticker-${type}`;
        el.dataset.id   = data.id;
        el.style.left   = x + '%';
        el.style.top    = y + '%';
        el.innerHTML    = (type === 'exclamation' ? '!' : '→') +
          `<button class="sticker-del" data-id="${data.id}" title="Remove">✕</button>`;
        layer.appendChild(el);
        wireSticker(el);
      });
    });
  });
})();
