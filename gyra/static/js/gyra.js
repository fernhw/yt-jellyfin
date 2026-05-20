/* gyra.js — global helpers */
(function () {
  'use strict';

  // Auto-dismiss flash messages after 6 s
  document.querySelectorAll('.flash').forEach(el => {
    const id = setTimeout(() => {
      el.style.transition = 'opacity .4s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 420);
    }, 6000);

    const closeBtn = el.querySelector('.flash-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => {
        clearTimeout(id);
        el.style.transition = 'opacity .2s';
        el.style.opacity = '0';
        setTimeout(() => el.remove(), 220);
      });
    }
  });

  // Uppercase enforcement for project key inputs
  document.querySelectorAll('input[name="key"]').forEach(inp => {
    inp.addEventListener('input', () => {
      const pos = inp.selectionStart;
      inp.value = inp.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
      inp.setSelectionRange(pos, pos);
    });
  });
})();
