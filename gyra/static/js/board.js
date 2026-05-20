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

      // Persist via API
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
})();
