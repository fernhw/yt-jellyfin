/* board.js — HTML5 drag-and-drop kanban */
(function () {
  'use strict';

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
  const rootMeta = document.querySelector('meta[name="app-root"]');
  const appRoot = rootMeta ? rootMeta.getAttribute('content') : '';

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

  // Each visual sub-column is its own drop zone
  function initSubcol(subcol, col) {
    subcol.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      col.classList.add('drag-over');
      subcol.classList.add('subcol-drag-over');
    });

    subcol.addEventListener('dragleave', e => {
      subcol.classList.remove('subcol-drag-over');
      if (!col.contains(e.relatedTarget)) {
        col.classList.remove('drag-over');
      }
    });

    subcol.addEventListener('drop', e => {
      e.preventDefault();
      subcol.classList.remove('subcol-drag-over');
      col.classList.remove('drag-over');
      if (!dragCard) return;

      const storyId  = dragCard.dataset.storyId;
      const statusId = col.dataset.statusId;
      const sprint   = col.dataset.sprint || null;
      const isCross  = sourceCol && sourceCol !== col;

      const afterCard   = getCardAfterDrop(subcol, e.clientY);
      const prevSibling = dragCard.nextSibling;
      const prevParent  = dragCard.parentNode;

      if (afterCard) {
        subcol.insertBefore(dragCard, afterCard);
      } else {
        subcol.appendChild(dragCard);
      }

      refreshColWidths();
      if (isCross) updateCount(sourceCol);
      updateCount(col);

      // Multi-select: move all selected cards into this same subcol
      const selectedCards = Array.from(document.querySelectorAll('.board-card.card-selected'));
      const isMulti = selectedCards.length > 1 && selectedCards.includes(dragCard);

      if (isMulti) {
        const otherSelected = selectedCards.filter(c => c !== dragCard);
        const affectedCols  = new Set([col]);
        if (sourceCol && sourceCol !== col) affectedCols.add(sourceCol);
        otherSelected.forEach(c => {
          const oc = c.closest('.board-column');
          if (oc) affectedCols.add(oc);
          subcol.appendChild(c);
        });
        refreshColWidths();
        affectedCols.forEach(c => updateCount(c));
        updateCount(col);

        const allIds = selectedCards.map(c => parseInt(c.dataset.storyId, 10));
        // Suppress reconciler for all affected cards BEFORE the network call
        if (window._markBoardMoves) {
          const _earlyIds = [];
          affectedCols.forEach(ac => Array.from(ac.querySelectorAll('.board-card')).forEach(c => _earlyIds.push(c.dataset.storyId)));
          window._markBoardMoves(_earlyIds);
        }

        fetch(appRoot + '/api/stories/bulk-move', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
          body: JSON.stringify({
            story_ids: allIds,
            status_id: parseInt(statusId, 10),
            sprint:    sprint ? parseInt(sprint, 10) : null,
          }),
        })
        .then(r => { if (!r.ok) throw new Error('API error ' + r.status); })
        .then(() => {
          affectedCols.forEach(saveColOrder);
          // Cascade container/integrate badges for every moved card
          if (window._postMoveBadgeRefresh) {
            allIds.forEach(function(sid){
              var c = document.querySelector('.board-card[data-story-id="' + sid + '"]');
              if (c) window._postMoveBadgeRefresh(c);
            });
          }
          clearSelection();
        })
        .catch(() => {
          if (prevParent) {
            if (prevSibling) prevParent.insertBefore(dragCard, prevSibling);
            else prevParent.appendChild(dragCard);
          }
          refreshColWidths();
          if (sourceCol) updateCount(sourceCol);
          updateCount(col);
        });
        return;
      }

      // Single-card cross-column move: update status, then persist order for both cols
      if (isCross) {
        // Suppress reconciler for BOTH affected columns right now, before the network call
        if (window._markBoardMoves) {
          const _earlyColIds = Array.from(col.querySelectorAll('.board-card')).map(c => c.dataset.storyId);
          const _earlySrcIds = sourceCol ? Array.from(sourceCol.querySelectorAll('.board-card')).map(c => c.dataset.storyId) : [];
          window._markBoardMoves([..._earlyColIds, ..._earlySrcIds]);
        }

        fetch(appRoot + `/api/story/${storyId}/move`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
          body: JSON.stringify({
            status_id:   parseInt(statusId, 10),
            sprint:      sprint ? parseInt(sprint, 10) : null,
            order_index: 0,
          }),
        })
        .then(r => { if (!r.ok) throw new Error('API error ' + r.status); })
        .then(() => {
          saveColOrder(col); saveColOrder(sourceCol);
          if (window._postMoveBadgeRefresh) window._postMoveBadgeRefresh(dragCard);
        })
        .catch(() => {
          if (prevParent) {
            if (prevSibling) prevParent.insertBefore(dragCard, prevSibling);
            else prevParent.appendChild(dragCard);
          }
          refreshColWidths();
          if (sourceCol) updateCount(sourceCol);
          updateCount(col);
        });
      } else {
        // Same-column reorder only — no status change needed
        saveColOrder(col);
      }
    });
  }

  // Persist the DOM order of all cards in a column to the server
  function saveColOrder(col) {
    if (!col) return;
    // Traverse per-subcol so we can record which subcol each card lives in.
    var allCards = [];
    var subcols = Array.from(col.querySelectorAll(':scope .board-cards > .board-subcol'));
    if (subcols.length) {
      subcols.forEach(function (sub, si) {
        Array.from(sub.querySelectorAll('.board-card')).forEach(function (c) {
          c.dataset.subcolIndex = String(si);
          allCards.push(c);
        });
      });
    } else {
      allCards = Array.from(col.querySelectorAll('.board-card'));
      allCards.forEach(function (c) { c.dataset.subcolIndex = '0'; });
    }
    allCards.forEach(function (c, i) { c.dataset.orderIndex = String(i); });
    const items = allCards.map(function (c, i) {
      return {
        id:           parseInt(c.dataset.storyId, 10),
        order_index:  i,
        subcol_index: parseInt(c.dataset.subcolIndex || '0', 10),
      };
    });
    if (!items.length) return;
    if (window._markBoardMoves) window._markBoardMoves(items.map(function (it) { return String(it.id); }));
    fetch(appRoot + '/api/stories/reorder', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
      body:    JSON.stringify({ items }),
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

  // ── Split each status column into real sub-column drop zones ─────────────
  function refreshColWidths() {
    document.querySelectorAll('.board-column').forEach(function (col) {
      var outer = col.querySelector('.board-cards');
      if (!outer) return;

      var n = col.querySelectorAll('.board-card').length;
      var inner = n > 12 ? 3 : n > 6 ? 2 : 1;
      col.style.setProperty('--inner-cols', inner);

      // Only restructure when the sub-column count needs to change
      var currentInner = parseInt(outer.dataset.innerCols || '0', 10);
      if (currentInner === inner) return;
      outer.dataset.innerCols = inner;

      // Snapshot all cards before touching the DOM
      var allCards = Array.from(col.querySelectorAll('.board-card'));

      // Remove old subcols (cards inside are removed with them but refs still valid)
      Array.from(outer.querySelectorAll(':scope > .board-subcol')).forEach(function (s) {
        s.remove();
      });

      // Build new subcols and attach drop logic
      var newSubs = [];
      for (var i = 0; i < inner; i++) {
        var sub = document.createElement('div');
        sub.className = 'board-subcol';
        outer.appendChild(sub);
        newSubs.push(sub);
        initSubcol(sub, col);
      }

      // Distribute by stored subcol_index — user controls which subcol each card
      // lives in. Clamp to the actual number of subcols (in case inner shrank).
      allCards.forEach(function (card) {
        var si = Math.min(parseInt(card.dataset.subcolIndex || '0', 10), inner - 1);
        newSubs[si].appendChild(card);
      });
    });
  }

  // Init on load
  document.querySelectorAll('.board-card').forEach(initCard);
  refreshColWidths();
  // Expose so bulk-action scripts can trigger redistribution
  window._refreshColWidths = refreshColWidths;
  // Expose so the realtime reconciler can wire drag on newly-inserted cards
  window._initBoardCardDrag = initCard;
  // Expose so the Organize button can persist new order/subcol layout
  window._saveColOrder = saveColOrder;

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
