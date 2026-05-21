/**
 * screensaver.js — Gyra board screensaver
 *
 * Each slide: one unique story + its first assignee, on a solid colour
 * background that changes every time. The card drifts to a new random
 * position every slide so no pixel is ever static — that is the whole point.
 * Every CLOCK_EVERY story slides a full-screen clock appears.
 * Tap / click / Escape exits.
 */
(function () {
  'use strict';

  var SLIDE_MS    = 9000;
  var CLOCK_MS    = 7500;
  var BOARD_MS    = 14000;  // board overview slide duration
  var FADE_MS     = 600;    // must match CSS transition
  var CLOCK_EVERY = 4;

  /* ── Post-it note background palette ────────────────────────────────── */
  var BG_COLORS = [
    '#FF6B6B', '#FFD93D', '#6BCB77', '#4D96FF', '#FF6BAE',
    '#FFB347', '#C084FC', '#06D6A0', '#EF476F', '#0EA5E9',
    '#FBBF24', '#34D399', '#FB923C', '#F472B6', '#818CF8',
  ];

  /* ── Sticker definitions — match exact board colors + icons ───────────── */
  var STICKER_DEFS = {
    exclamation: { bg: '#FF3B30', color: '#fff',      icon: '!' },
    arrow:       { bg: '#FF9F0A', color: '#fff',      icon: '→' },
    question:    { bg: '#007AFF', color: '#fff',      icon: '?' },
    star:        { bg: '#FFD60A', color: '#1a0a00',   icon: '★' },
    fire:        { bg: '#FF9500', color: '#fff',      icon: '🔥' },
    eyes:        { bg: '#636366', color: '#fff',      icon: '👀' },
    check:       { bg: '#34C759', color: '#fff',      icon: '✓' },
    blocked:     { bg: '#FF2D55', color: '#fff',      icon: '✕' },
    up:          { bg: '#BF5AF2', color: '#fff',      icon: '↑' },
    note:        { bg: '#5AC8FA', color: '#1a2030',   icon: '#' },
  };

  /* ── Fun fallback sticker types for stories with no board stickers ──────── */
  var FUN_STICKER_TYPES = ['exclamation','star','fire','check','up','note','question','arrow','blocked','eyes'];

  /* ── Random scatter position — zones around the card edges ─────────────── */
  function _stickerPos() {
    /* full-screen scatter — spread across the entire viewport */
    return {
      top:  5 + Math.random() * 88,   /* 5% – 93% of viewport height */
      left: 3 + Math.random() * 91,   /* 3% – 94% of viewport width  */
    };
  }

  /* ── Fisher-Yates shuffle ─────────────────────────────────────────────── */
  function shuffle(arr) {
    var i = arr.length, j, tmp;
    while (i > 1) {
      j = Math.floor(Math.random() * i--);
      tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
    }
    return arr;
  }

  /* ── Screensaver class ─────────────────────────────────────────────────── */

  function Screensaver(projectId) {
    this.projectId   = projectId;
    this._storyPool  = [];   // deduplicated story slides
    this._queue      = [];   // current play queue
    this.idx         = -1;
    this._paletteIdx = 0;
    this._timer      = null;
    this._clockTick  = null;
    this.overlay     = null;
    this._statusList = [];   // ordered statuses for board overview
    this._storiesByStatus = {}; // status_id → story[]
  }

  Screensaver.prototype.start = function () {
    var self = this;
    self._fetchData().then(function () {
      if (!self._storyPool.length) {
        alert('No assigned stories found in this sprint.\nAssign team members to sprint stories first!');
        return;
      }
      self._buildQueue();
      self._mount();
      self.idx = -1;
      self._next();
      self.overlay.focus();
    });
  };

  /* ── Data fetching — each story appears ONCE (first assignee) ───────────── */

  Screensaver.prototype._fetchData = function () {
    var self = this;
    return Promise.all([
      fetch('/api/project/' + self.projectId + '/board-full-state').then(function (r) { return r.json(); }),
      fetch('/api/statuses/'  + self.projectId).then(function (r) { return r.json(); }),
    ]).then(function (res) {
      var boardData  = res[0];
      var statusData = res[1];

      var statusMap = {};
      (statusData || []).forEach(function (s) { statusMap[s.id] = s; });

      /* store ordered statuses + stories grouped by status for the board overview */
      self._statusList = statusData || [];
      self._storiesByStatus = {};
      (boardData.stories || []).forEach(function (story) {
        var sid = story.status_id;
        if (!self._storiesByStatus[sid]) self._storiesByStatus[sid] = [];
        self._storiesByStatus[sid].push(story);
      });

      /* one slide per story, paired with its first assignee */
      /* build sticker map: story_id → [type strings] */
      var stickersByStory = {};
      (boardData.stickers || []).forEach(function (st) {
        if (!st.card_story_id) return;
        if (!stickersByStory[st.card_story_id]) stickersByStory[st.card_story_id] = [];
        stickersByStory[st.card_story_id].push(st.type);
      });

      /* one slide per story, paired with its first assignee */
      var pool = [];
      (boardData.stories || []).forEach(function (story) {
        var assignees = story.assignees || [];
        if (!assignees.length) return;
        var copy = {};
        for (var k in story) copy[k] = story[k];
        copy._status = statusMap[story.status_id] || null;
        copy._users  = assignees;  /* all participants */
        pool.push({ type: 'story', user: assignees[0], story: copy, stickers: stickersByStory[story.id] || [] });
      });

      self._storyPool = pool;
    }).catch(function (err) {
      console.error('[Screensaver] fetch error:', err);
      self._storyPool = [];
    });
  };

  /* ── Build/re-build the play queue with fresh shuffle ───────────────────── */

  Screensaver.prototype._buildQueue = function () {
    /* in-flight = not in first or last column — mid-way stories are most interesting */
    var n       = this._statusList.length;
    var firstId = n > 0 ? this._statusList[0].id         : -1;
    var lastId  = n > 1 ? this._statusList[n - 1].id     : -1;
    var inFlight = [], notInFlight = [];
    this._storyPool.forEach(function (slide) {
      var sid = slide.story && slide.story.status_id;
      if (sid !== firstId && sid !== lastId) {
        inFlight.push(slide);
      } else {
        notInFlight.push(slide);
      }
    });
    var stories = shuffle(inFlight).concat(shuffle(notInFlight));
    var queue   = [];
    stories.forEach(function (slide, i) {
      queue.push(slide);
      if ((i + 1) % CLOCK_EVERY === 0) {
        queue.push({ type: 'clock' });
        queue.push({ type: 'board' });  // board overview after each clock
      }
    });
    this._queue = queue;
    this.idx    = -1;
  };

  /* ── DOM overlay ──────────────────────────────────────────────────────── */

  Screensaver.prototype._mount = function () {
    var self = this;
    var el   = document.createElement('div');
    el.id        = 'ss-overlay';
    el.className = 'ss-overlay ss-active';
    el.tabIndex  = 0;

    el.innerHTML =
      '<div class="ss-content-wrap ss-fade-in" id="ss-content"></div>' +
      '<div class="ss-sticker-layer" id="ss-sticker-layer"></div>';

    el.addEventListener('click',      function () { self.stop(); });
    el.addEventListener('touchstart', function () { self.stop(); }, { passive: true });
    el.addEventListener('keydown',    function (e) { if (e.key === 'Escape') self.stop(); });

    document.body.appendChild(el);
    this.overlay = el;
  };

  /* ── Advance to next slide ────────────────────────────────────────────── */

  Screensaver.prototype._next = function () {
    var self = this;
    if (!self.overlay) return;

    self.idx++;
    /* end of queue → re-shuffle and start a new cycle */
    if (self.idx >= self._queue.length) {
      self._buildQueue();
      self.idx = 0;
    }

    var slide   = self._queue[self.idx];
    var content = document.getElementById('ss-content');

    /* fade out */
    content.classList.remove('ss-fade-in');
    content.classList.add('ss-fade-out');
    clearInterval(self._clockTick);

    setTimeout(function () {
      if (!self.overlay) return;

      /* new background colour every slide */
      self.overlay.style.background = BG_COLORS[self._paletteIdx % BG_COLORS.length];
      self._paletteIdx++;

      /* move the card to a fresh random position (board slide stays centred) */
      self._randomisePosition(content, slide.type === 'clock', slide.type === 'board');

      /* render */
      if (slide.type === 'clock') {
        self._renderClock(content);
      } else if (slide.type === 'board') {
        self._renderBoard(content);
      } else {
        self._renderStory(content, slide.user, slide.story, slide.stickers || []);
      }

      /* fade in */
      content.classList.remove('ss-fade-out');
      content.classList.add('ss-fade-in');

      var dur = slide.type === 'clock' ? CLOCK_MS
              : slide.type === 'board' ? BOARD_MS
              : SLIDE_MS;
      self._timer = setTimeout(function () { self._next(); }, dur);
    }, FADE_MS);
  };

  /* ── Drift the card to a new position on screen ─────────────────────────── */

  Screensaver.prototype._randomisePosition = function (content, isClock, isBoard) {
    if (isBoard) {
      /* board overview takes up the whole screen — no drift, centred */
      content.classList.add('ss-wide');
      content.style.transform = 'translate(-50%, -50%)';
      return;
    }
    content.classList.remove('ss-wide');
    /* ±rangeX vw horizontally, ±rangeY vh vertically from centre */
    var rangeX = isClock ? 8  : 14;
    var rangeY = isClock ? 6  : 12;
    var dx = (Math.random() - 0.5) * 2 * rangeX;
    var dy = (Math.random() - 0.5) * 2 * rangeY;
    content.style.transform =
      'translate(calc(-50% + ' + dx.toFixed(1) + 'vw), calc(-50% + ' + dy.toFixed(1) + 'vh))';
  };

  /* ── Renderers ─────────────────────────────────────────────────────────── */

  Screensaver.prototype._renderStory = function (content, user, story, stickers) {
    /* all participants — everyone assigned to this story */
    var users = (story._users && story._users.length) ? story._users : [user];
    var avatarHtml = users.map(function (u) {
      var av = u.avatar
        ? '<img src="/avatars/' + _esc(u.avatar) + '" class="ss-avatar-img" alt="">'
        : '<span class="ss-avatar-initials">' + _initials(u.display_name) + '</span>';
      return '<div class="ss-participant">' +
        '<div class="ss-avatar-wrap">' + av + '</div>' +
        '<span class="ss-p-name">' + _esc(u.display_name) + '</span>' +
        '</div>';
    }).join('');

    var typeBadge = story.story_type_name
      ? '<span class="ss-badge-type" style="background:' + _esc(story.story_type_color || '#6b7280') + '">' +
          _esc(story.story_type_name) + '</span>'
      : '';

    var pri    = story.priority || '';
    var priBadge = pri
      ? '<span class="ss-badge-priority ss-badge-priority--' + _esc(pri.toLowerCase()) + '">' + _esc(pri) + '</span>'
      : '';

    var ptsBadge = story.story_points
      ? '<span class="ss-badge-points">' + story.story_points + ' pt</span>'
      : '';

    var meta = [typeBadge, priBadge, ptsBadge].filter(Boolean).join('');
    var metaRow = meta ? '<div class="ss-meta">' + meta + '</div>' : '';

    var st = story._status;
    var statusHtml = st
      ? '<div class="ss-status-row"><span class="ss-status-chip" style="border-color:' +
          _esc(st.color) + ';color:' + _esc(st.color) + '">' + _esc(st.name) + '</span></div>'
      : '';

    /* thumbnail image */
    var thumbHtml = story.thumbnail
      ? '<div class="ss-thumb-wrap"><img src="/story-images/' + _esc(story.thumbnail) + '" class="ss-thumb" alt=""></div>'
      : '';

    /* addons (subtasks) — show up to 4 */
    var addons = story.addons || [];
    var addonsHtml = '';
    if (addons.length) {
      var items = addons.slice(0, 4).map(function (t) {
        return '<li class="ss-addon-item">' + _esc(t) + '</li>';
      }).join('');
      var more = addons.length > 4
        ? '<li class="ss-addon-more">+' + (addons.length - 4) + ' more</li>'
        : '';
      addonsHtml = '<ul class="ss-addons">' + items + more + '</ul>';
    }

    /* sticker chips — board-style colored squares scattered around the card */
    /* always aim for 7-9 stickers: use board stickers first, pad with random fun ones */
    var TARGET_STICKERS = 7 + Math.floor(Math.random() * 3); // 7, 8, or 9
    var stickerTypes = (stickers || []).slice(0, TARGET_STICKERS);
    if (stickerTypes.length < TARGET_STICKERS) {
      var _pad = FUN_STICKER_TYPES.slice(); shuffle(_pad);
      for (var _pi = 0; _pi < _pad.length && stickerTypes.length < TARGET_STICKERS; _pi++) {
        if (stickerTypes.indexOf(_pad[_pi]) === -1) stickerTypes.push(_pad[_pi]);
      }
    }
    var stickersHtml = stickerTypes.map(function (type) {
      var def = STICKER_DEFS[type] || { bg: '#888', color: '#fff', icon: '?' };
      var pos = _stickerPos();
      var rot = (Math.random() - 0.5) * 40;
      return '<div class="ss-sticker" style="' +
        'top:'  + pos.top.toFixed(1)  + '%;' +
        'left:' + pos.left.toFixed(1) + '%;' +
        'background:' + def.bg + ';' +
        'color:' + def.color + ';' +
        'transform:rotate(' + rot.toFixed(1) + 'deg)' +
        '">' + def.icon + '</div>';
    }).join('');

    /* subtle random card tilt each slide — post-it stuck on wall vibe */
    var tilt = (Math.random() - 0.5) * 3;

    /* populate the full-screen sticker layer */
    var stickerLayer = document.getElementById('ss-sticker-layer');
    if (stickerLayer) stickerLayer.innerHTML = stickersHtml;

    content.innerHTML =
      '<div class="ss-card-tilt" style="transform:rotate(' + tilt.toFixed(2) + 'deg)">' +
        '<div class="ss-card ss-card-enter">' +
          '<div class="ss-participants">' + avatarHtml + '</div>' +
          '<div class="ss-divider"></div>' +
          metaRow +
          '<div class="ss-title">' + (story.html_title || _esc(story.title)) + '</div>' +
          (story.description ? '<div class="ss-description">' + _esc(story.description) + '</div>' : '') +
          statusHtml +
          thumbHtml +
          addonsHtml +
        '</div>' +
      '</div>';
  };

  /* ── Board overview renderer ─────────────────────────────────────────────── */

  Screensaver.prototype._renderBoard = function (content) {
    var self = this;
    /* show columns that have at least one story */
    var cols = self._statusList.filter(function (s) {
      return (self._storiesByStatus[s.id] || []).length > 0;
    });

    var colsHtml = cols.map(function (st) {
      var stories = (self._storiesByStatus[st.id] || []).slice(0, 6);
      var more    = (self._storiesByStatus[st.id] || []).length - stories.length;
      var items   = stories.map(function (s) {
        return '<li class="ss-bov-story">' + _esc(s.title) + '</li>';
      }).join('');
      if (more > 0) items += '<li class="ss-bov-more">+' + more + ' more…</li>';
      return '<div class="ss-bov-col">' +
        '<div class="ss-bov-col-head" style="border-color:' + _esc(st.color) + ';color:' + _esc(st.color) + '">' +
          _esc(st.name) +
          '<span class="ss-bov-cnt">' + (self._storiesByStatus[st.id] || []).length + '</span>' +
        '</div>' +
        '<ul class="ss-bov-list">' + items + '</ul>' +
      '</div>';
    }).join('');

    content.innerHTML =
      '<div class="ss-board-overview ss-card-enter">' +
        '<div class="ss-bov-title">Sprint Board</div>' +
        '<div class="ss-bov-cols">' + colsHtml + '</div>' +
      '</div>';
  };

  Screensaver.prototype._renderClock = function (content) {
    var self = this;

    function timeStr() {
      return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    }
    function dateStr() {
      return new Date().toLocaleDateString([], {
        weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
      });
    }

    var t   = timeStr();
    var hh  = t.slice(0, 2);
    var mm  = t.slice(3, 5);

    content.innerHTML =
      '<div class="ss-clock-wrap ss-card-enter">' +
        '<div class="ss-clock-time">' +
          '<span id="ss-clock-hh">' + hh + '</span>' +
          '<span class="ss-clock-colon">:</span>' +
          '<span id="ss-clock-mm">' + mm + '</span>' +
        '</div>' +
        '<div class="ss-clock-date" id="ss-clock-date">' + dateStr() + '</div>' +
      '</div>';

    /* tick every second */
    self._clockTick = setInterval(function () {
      var now  = new Date();
      var hEl  = document.getElementById('ss-clock-hh');
      var mEl  = document.getElementById('ss-clock-mm');
      var dEl  = document.getElementById('ss-clock-date');
      if (!hEl) { clearInterval(self._clockTick); return; }
      var t2 = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
      hEl.textContent = t2.slice(0, 2);
      mEl.textContent = t2.slice(3, 5);
      if (dEl) dEl.textContent = dateStr();
    }, 1000);
  };

  /* ── Progress bar ──────────────────────────────────────────────────────── */

  Screensaver.prototype._startProgress = function (duration) {
    var fill = document.getElementById('ss-progress-fill');
    if (!fill) return;
    fill.style.transition = 'none';
    fill.style.width = '0%';
    /* force reflow so the transition restart is visible */
    void fill.offsetWidth;
    fill.style.transition = 'width ' + duration + 'ms linear';
    fill.style.width = '100%';
  };

  /* ── Stop / tear-down ──────────────────────────────────────────────────── */

  Screensaver.prototype.stop = function () {
    clearTimeout(this._timer);
    clearInterval(this._clockTick);
    if (this.overlay && this.overlay.parentNode) {
      this.overlay.parentNode.removeChild(this.overlay);
    }
    this.overlay = null;
  };

  /* ── Utilities ─────────────────────────────────────────────────────────── */

  function _esc(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _initials(name) {
    return (name || '?')
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map(function (w) { return w.charAt(0) || ''; })
      .join('')
      .toUpperCase();
  }

  /* ── Expose ─────────────────────────────────────────────────────────────── */
  window.Gyra = window.Gyra || {};
  window.Gyra.Screensaver = Screensaver;

}());
