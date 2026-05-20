/* board.js — HTML5 drag-and-drop kanban */
(function () {
  'use strict';

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

  let dragCard = null;
  let sourceCol = null;

  function updateCount(col) {
    const cards = col.querySelectorAll('.board-card');
    const badge = col.querySelector('.col-count');
    if (badge) badge.textContent = cards.length;
  }

  function initCard(card) {
    card.setAttribute('draggable', 'true');

    card.addEventListener('dragstart', e => {
      dragCard = card;
      sourceCol = card.closest('.board-column');
      card.classList.add('dragging');
      // Amplify rotation and scale on drag
      const baseRot = parseFloat(getComputedStyle(card).getPropertyValue('--rot') || '0');
      card.style.transform = `rotate(${baseRot * 2}deg) scale(1.08)`;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', card.dataset.storyId);
    });

    card.addEventListener('dragend', () => {
      card.classList.remove('dragging');
      card.style.transform = '';
      dragCard = null;
    });
  }

  function initColumn(col) {
    const zone = col.querySelector('.board-cards');
    if (!zone) return;

    zone.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      col.classList.add('drag-over');
    });

    zone.addEventListener('dragleave', e => {
      if (!col.contains(e.relatedTarget)) {
        col.classList.remove('drag-over');
      }
    });

    zone.addEventListener('drop', e => {
      e.preventDefault();
      col.classList.remove('drag-over');
      if (!dragCard) return;

      const storyId  = dragCard.dataset.storyId;
      const statusId = col.dataset.statusId;
      const sprint   = col.dataset.sprint || null;

      // Compute optimistic new order_index
      const cards     = Array.from(zone.querySelectorAll('.board-card'));
      const afterCard = getCardAfterDrop(zone, e.clientY);
      let newIndex;
      if (afterCard) {
        newIndex = parseInt(afterCard.dataset.orderIndex || '0', 10) - 1;
      } else {
        const lastCard = cards[cards.length - 1];
        newIndex = lastCard ? parseInt(lastCard.dataset.orderIndex || '0', 10) + 1 : 0;
      }

      // Optimistic DOM update
      const prevSibling = dragCard.nextSibling;
      const prevParent  = dragCard.parentNode;
      if (afterCard) {
        zone.insertBefore(dragCard, afterCard);
      } else {
        zone.appendChild(dragCard);
      }
      if (sourceCol) updateCount(sourceCol);
      updateCount(col);

      // If dragged card is part of a multi-selection, move ALL selected cards
      const selectedCards = Array.from(document.querySelectorAll('.board-card.card-selected'));
      const isMulti = selectedCards.length > 1 && selectedCards.includes(dragCard);

      if (isMulti) {
        // Move every selected card (other than the one already moved) to the same zone
        const otherSelected = selectedCards.filter(c => c !== dragCard);
        const affectedCols  = new Set();
        otherSelected.forEach(c => {
          const oc = c.closest('.board-column');
          if (oc) affectedCols.add(oc);
          zone.appendChild(c);
        });
        affectedCols.forEach(c => updateCount(c));
        updateCount(col);

        const allIds = selectedCards.map(c => parseInt(c.dataset.storyId, 10));
        fetch('/api/stories/bulk-move', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
          body: JSON.stringify({
            story_ids: allIds,
            status_id: parseInt(statusId, 10),
            sprint:    sprint ? parseInt(sprint, 10) : null,
          }),
        })
        .then(r => { if (!r.ok) throw new Error('API error ' + r.status); })
        .catch(() => {
          // On failure revert the primary card only (minor UX trade-off)
          if (prevParent) {
            if (prevSibling) prevParent.insertBefore(dragCard, prevSibling);
            else prevParent.appendChild(dragCard);
          }
          if (sourceCol) updateCount(sourceCol);
          updateCount(col);
        });
        return;
      }

      // Single-card move
      fetch(`/api/story/${storyId}/move`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken,
        },
        body: JSON.stringify({
          status_id:   parseInt(statusId, 10),
          sprint:      sprint ? parseInt(sprint, 10) : null,
          order_index: newIndex,
        }),
      })
      .then(r => {
        if (!r.ok) throw new Error('API error ' + r.status);
      })
      .catch(() => {
        // Revert DOM on failure
        if (prevParent) {
          if (prevSibling) prevParent.insertBefore(dragCard, prevSibling);
          else prevParent.appendChild(dragCard);
        }
        if (sourceCol) updateCount(sourceCol);
        updateCount(col);
      });
    });
  }

  function getCardAfterDrop(zone, y) {
    const cards = Array.from(zone.querySelectorAll('.board-card:not(.dragging)'));
    for (const card of cards) {
      const rect = card.getBoundingClientRect();
      if (y < rect.top + rect.height / 2) return card;
    }
    return null;
  }

  // Init on load
  document.querySelectorAll('.board-card').forEach(initCard);
  document.querySelectorAll('.board-column').forEach(initColumn);

  // ── Shift multi-select ───────────────────────────────────────────────────
  let lastSelected = null;
  const selected   = new Set();

  function getCardIndex(card, col) {
    return Array.from(col.querySelectorAll('.board-card')).indexOf(card);
  }

  function selectCard(card) {
    selected.add(card);
    card.classList.add('card-selected');
  }
  function deselectCard(card) {
    selected.delete(card);
    card.classList.remove('card-selected');
  }
  function clearSelection() {
    selected.forEach(function (c) { c.classList.remove('card-selected'); });
    selected.clear();
    lastSelected = null;
    updateBulkBar();
  }

  function updateBulkBar() {
    var bar = document.getElementById('bulk-action-bar');
    if (!bar) return;
    if (selected.size > 0) {
      bar.style.display = 'flex';
      var lbl = bar.querySelector('.bulk-count');
      if (lbl) lbl.textContent = selected.size + ' selected';
    } else {
      bar.style.display = 'none';
    }
  }

  // Inject bulk bar (once)
  (function () {
    var bar = document.createElement('div');
    bar.id = 'bulk-action-bar';
    bar.style.cssText = 'display:none;position:fixed;bottom:20px;left:50%;transform:translateX(-50%);' +
      'background:#1A1208;color:#fff;padding:10px 20px;border-radius:3px;' +
      'gap:12px;align-items:center;z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,.4);' +
      'font-size:13px;font-weight:600;';
    bar.innerHTML = '<span class="bulk-count"></span>' +
      '<button onclick="window._clearBulk()" style="background:none;border:1px solid rgba(255,255,255,.3);color:#fff;padding:4px 10px;cursor:pointer;border-radius:2px;font-size:12px;">Clear</button>';
    document.body.appendChild(bar);
  }());

  window._clearBulk = clearSelection;

  // Handle click-to-select on cards (with Shift support)
  document.getElementById('board').addEventListener('click', function (e) {
    var card = e.target.closest('.board-card');
    if (!card) {
      // Click on empty board area clears selection
      if (!e.target.closest('#bulk-action-bar')) clearSelection();
      return;
    }

    // Only intercept when Shift is held OR at least one card is already selected
    if (!e.shiftKey && selected.size === 0) {
      lastSelected = card;
      return; // normal click navigates to story
    }

    e.preventDefault();
    e.stopPropagation();

    if (e.shiftKey && lastSelected) {
      // Range-select within same column
      var col = card.closest('.board-column');
      var lastCol = lastSelected.closest('.board-column');
      if (col === lastCol) {
        var cards = Array.from(col.querySelectorAll('.board-card'));
        var a = cards.indexOf(lastSelected);
        var b = cards.indexOf(card);
        var lo = Math.min(a, b);
        var hi = Math.max(a, b);
        for (var i = lo; i <= hi; i++) selectCard(cards[i]);
      } else {
        selectCard(card);
      }
    } else if (selected.has(card)) {
      deselectCard(card);
      if (card === lastSelected) lastSelected = null;
    } else {
      selectCard(card);
    }

    lastSelected = card;
    updateBulkBar();
  }, true);

  // Add CSS for selected cards inline
  (function () {
    var s = document.createElement('style');
    s.textContent = '.board-card.card-selected{outline:3px solid #7C4A1E !important;outline-offset:2px;}';
    document.head.appendChild(s);
  }());
})();
