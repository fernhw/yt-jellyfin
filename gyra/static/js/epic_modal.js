/* Global Epic edit modal — exposes window.openEpicModal(id) anywhere. */
(function () {
  if (window.__epicModalInited) return;
  window.__epicModalInited = true;

  var bg = document.getElementById('em-bg');
  if (!bg) return;

  var ROOT = (document.querySelector('meta[name="app-root"]') || {getAttribute:function(){return '';}}).getAttribute('content') || '';
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {getAttribute:function(){return '';}}).getAttribute('content') || '';

  var EPIC_COLORS = [
    '#6B7280', '#DC2626', '#EA580C', '#D97706', '#65A30D',
    '#059669', '#0891B2', '#2563EB', '#7C3AED', '#C026D3',
    '#DB2777', '#7C4A1E'
  ];
  var STATUS_LABELS = {
    planning:'Planning', active:'Active', on_hold:'On hold', completed:'Completed'
  };

  var loading     = document.getElementById('em-loading');
  var content     = document.getElementById('em-content');
  var titleInput  = document.getElementById('em-title');
  var statusBadge = document.getElementById('em-status-badge');
  var colorDot    = document.getElementById('em-color-dot');

  var currentEpic = null;
  var onChangeCb  = null;  // optional listener (e.g. manage page) to refresh after save/delete
  var selectedColor = null;  // currently chosen color (swatch / input / auto)

  // ── Color helpers (CIE76 distance in Lab) ─────────────────────────
  function hexToRgb(h){
    h = (h||'').replace('#','');
    if (h.length === 3) h = h.split('').map(function(c){return c+c;}).join('');
    var n = parseInt(h,16);
    return [(n>>16)&255,(n>>8)&255,n&255];
  }
  function rgbToHex(r,g,b){
    var hx = function(v){return ('0'+Math.max(0,Math.min(255,Math.round(v))).toString(16)).slice(-2);};
    return '#'+hx(r)+hx(g)+hx(b);
  }
  function rgbToLab(r,g,b){
    var R=r/255, G=g/255, B=b/255;
    var f0 = function(v){return v>0.04045 ? Math.pow((v+0.055)/1.055,2.4) : v/12.92;};
    R=f0(R); G=f0(G); B=f0(B);
    var X = (R*0.4124 + G*0.3576 + B*0.1805) / 0.95047;
    var Y = (R*0.2126 + G*0.7152 + B*0.0722) / 1.00000;
    var Z = (R*0.0193 + G*0.1192 + B*0.9505) / 1.08883;
    var f1 = function(t){return t > 0.008856 ? Math.cbrt(t) : (7.787*t + 16/116);};
    var fx=f1(X), fy=f1(Y), fz=f1(Z);
    return [116*fy - 16, 500*(fx-fy), 200*(fy-fz)];
  }
  function labDist(a,b){ var dL=a[0]-b[0], dA=a[1]-b[1], dB=a[2]-b[2]; return Math.sqrt(dL*dL+dA*dA+dB*dB); }
  function hslToRgb(h, s, l){
    h/=360; s/=100; l/=100;
    if (s === 0) { var v = l*255; return [v,v,v]; }
    var q = l < 0.5 ? l*(1+s) : l + s - l*s;
    var p = 2*l - q;
    var h2 = function(t){
      if (t<0) t+=1; if (t>1) t-=1;
      if (t<1/6) return p + (q-p)*6*t;
      if (t<1/2) return q;
      if (t<2/3) return p + (q-p)*(2/3 - t)*6;
      return p;
    };
    return [h2(h+1/3)*255, h2(h)*255, h2(h-1/3)*255];
  }
  function pickFurthestColor(existingHexes){
    var existing = existingHexes.filter(Boolean).map(function(h){ return rgbToLab.apply(null, hexToRgb(h)); });
    var best = null, bestScore = -1;
    var HUES = 36, SATS = [55,70,85], LIGHTS = [45,55,65];
    for (var i=0; i<HUES; i++){
      var h = (i*360/HUES);
      for (var si=0; si<SATS.length; si++){
        for (var li=0; li<LIGHTS.length; li++){
          var rgb = hslToRgb(h, SATS[si], LIGHTS[li]);
          var lab = rgbToLab(rgb[0], rgb[1], rgb[2]);
          var minD = Infinity;
          for (var k=0; k<existing.length; k++){
            var d = labDist(lab, existing[k]);
            if (d < minD) minD = d;
          }
          if (minD > bestScore){ bestScore = minD; best = rgbToHex(rgb[0], rgb[1], rgb[2]); }
        }
      }
    }
    return best || '#6B7280';
  }

  function setColor(hex, opts){
    opts = opts || {};
    selectedColor = hex;
    colorDot.style.background = hex;
    var input = document.getElementById('em-color-input');
    if (input && !opts.skipInput) input.value = hex;
    // Sync swatch selection state.
    var pickerWrap = document.getElementById('em-color-picker');
    if (pickerWrap){
      var match = null;
      pickerWrap.querySelectorAll('.em-color-swatch').forEach(function(sw){
        sw.classList.remove('is-selected');
        if (sw.getAttribute('data-color').toLowerCase() === hex.toLowerCase()) match = sw;
      });
      if (match) match.classList.add('is-selected');
    }
  }

  function esc(s){ return String(s||'').replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });}
  function fmtDate(ts){ if(!ts) return '—'; try { return new Date(ts*1000).toLocaleDateString(); } catch(e){ return '—'; } }

  function apiGet(path){
    return fetch(ROOT+path, {credentials:'same-origin'}).then(function(r){
      if (!r.ok) return {ok:false, status:r.status};
      return r.json();
    });
  }
  function apiPatch(path, body){
    return fetch(ROOT+path, {
      method:'PATCH', credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},
      body: JSON.stringify(body)
    }).then(function(r){ return r.json(); });
  }

  function renderColorPicker(selected){
    var wrap = document.getElementById('em-color-picker');
    wrap.innerHTML = '';
    EPIC_COLORS.forEach(function(c){
      var sw = document.createElement('span');
      sw.className = 'em-color-swatch';
      sw.style.background = c;
      sw.setAttribute('data-color', c);
      sw.addEventListener('click', function(){ setColor(c); });
      wrap.appendChild(sw);
    });
    setColor(selected || '#6B7280');
  }

  function updateStatusBadge(s){
    statusBadge.className = 'em-status-badge s-' + s;
    statusBadge.textContent = STATUS_LABELS[s] || 'Planning';
  }

  function renderStatusBreakdown(by_status, total){
    var bar    = document.getElementById('em-status-bar');
    var legend = document.getElementById('em-status-legend');
    bar.innerHTML = ''; legend.innerHTML = '';
    if (!total) {
      bar.innerHTML = '<div style="flex:1;background:var(--c-gray-200,#DDD0B8)"></div>';
      legend.innerHTML = '<span style="color:var(--c-gray-500,#7A6A52);font-style:italic">No stories yet</span>';
      return;
    }
    by_status.forEach(function(s){
      if (!s.count) return;
      var pct = (s.count / total) * 100;
      var seg = document.createElement('div');
      seg.className = 'em-status-seg';
      seg.style.width = pct + '%';
      seg.style.background = s.color || '#6B7280';
      seg.textContent = s.count;
      seg.title = s.name + ': ' + s.count + ' (' + s.points + ' pts)';
      bar.appendChild(seg);

      var li = document.createElement('span');
      li.className = 'em-status-legend-item';
      li.innerHTML = '<span class="em-status-legend-dot" style="background:'+esc(s.color||'#6B7280')+'"></span>'
                  + esc(s.name) + ' (' + s.count + ')';
      legend.appendChild(li);
    });
  }

  function renderStories(stories){
    var list = document.getElementById('em-story-list');
    document.getElementById('em-story-total').textContent = '(' + stories.length + ')';
    if (!stories.length) {
      list.innerHTML = '<div style="padding:14px;text-align:center;color:var(--c-gray-500,#7A6A52);font-size:13px">No stories assigned yet.</div>';
      return;
    }
    list.innerHTML = '';
    stories.forEach(function(s){
      var row = document.createElement('div');
      row.className = 'em-story-row';
      row.innerHTML =
        '<span class="em-story-status-dot" style="background:'+esc(s.status_color||'#6B7280')+'"></span>'
      + '<span class="em-story-title">'+esc(s.title||'')+'</span>'
      + '<span class="em-story-status-name">'+esc(s.status_name||'')+'</span>'
      + '<span class="em-story-pts">'+(s.story_points||0)+' pts</span>';
      row.addEventListener('click', function(){
        closeEpicModal();
        if (typeof window.openStoryModal === 'function') {
          window.openStoryModal(s.id);
        } else {
          window.location.href = ROOT + '/story/' + s.id;
        }
      });
      list.appendChild(row);
    });
  }

  function renderDaysBadge(stats){
    var wrap = document.getElementById('em-days-badge-wrap');
    wrap.innerHTML = '';
    var d = stats.days_remaining;
    if (d === null || d === undefined) return;
    var cls, txt;
    if (d < 0)       { cls = 'is-late'; txt = Math.abs(d) + ' day' + (Math.abs(d)===1?'':'s') + ' overdue'; }
    else if (d <= 3) { cls = 'is-soon'; txt = d + ' day' + (d===1?'':'s') + ' left'; }
    else             { cls = 'is-ok';   txt = d + ' days left'; }
    var b = document.createElement('span');
    b.className = 'em-days-badge ' + cls;
    b.textContent = txt;
    wrap.appendChild(b);
  }

  function populate(data){
    var e = data.epic; currentEpic = e;
    var s = data.stats;

    titleInput.value          = e.title || '';
    document.getElementById('em-desc').value     = e.description || '';
    document.getElementById('em-status').value   = e.status || 'planning';
    document.getElementById('em-start').value    = e.start_date || '';
    document.getElementById('em-due').value      = e.due_date   || '';
    document.getElementById('em-creator').textContent = e.creator_name || '—';
    document.getElementById('em-created').textContent = fmtDate(e.created_at);
    document.getElementById('em-updated').textContent = fmtDate(e.updated_at);

    colorDot.style.background = e.color || '#6B7280';
    updateStatusBadge(e.status || 'planning');
    renderColorPicker(e.color);

    document.getElementById('em-pct-count').textContent  = s.pct_count + '%';
    document.getElementById('em-fill-count').style.width = s.pct_count + '%';
    document.getElementById('em-pct-points').textContent  = s.pct_points + '%';
    document.getElementById('em-fill-points').style.width = s.pct_points + '%';

    document.getElementById('em-stat-stories').textContent = s.total_stories;
    document.getElementById('em-stat-points').textContent  = s.total_points;
    document.getElementById('em-stat-done').textContent    = s.done_stories;
    document.getElementById('em-stat-days').textContent    =
      (s.days_remaining === null || s.days_remaining === undefined) ? '—' : s.days_remaining;

    var marker = document.getElementById('em-today-marker');
    if (s.sched_pct !== null && s.sched_pct !== undefined) {
      marker.style.display = 'block';
      marker.style.left = Math.min(100, Math.max(0, s.sched_pct)) + '%';
    } else { marker.style.display = 'none'; }

    var meta = '';
    if (e.start_date || e.due_date) {
      meta = (e.start_date || '?') + ' → ' + (e.due_date || '?');
      if (s.days_total !== null && s.days_total !== undefined) {
        meta += '  •  ' + s.days_total + ' day' + (s.days_total===1?'':'s') + ' total';
      }
    }
    document.getElementById('em-progress-meta').textContent = meta;

    renderStatusBreakdown(s.by_status || [], s.total_stories);
    renderStories(data.stories || []);
    renderDaysBadge(s);
  }

  function openEpicModal(epicId, opts){
    opts = opts || {};
    onChangeCb = typeof opts.onChange === 'function' ? opts.onChange : null;
    bg.style.display = 'flex';
    document.body.style.overflow = 'hidden';
    loading.style.display = 'block';
    content.style.display = 'none';
    apiGet('/api/epic/' + epicId + '/full').then(function(d){
      if (!d || !d.ok) { alert('Could not load epic'); closeEpicModal(); return; }
      populate(d);
      loading.style.display = 'none';
      content.style.display = '';
    });
  }
  function closeEpicModal(){
    bg.style.display = 'none';
    document.body.style.overflow = '';
    currentEpic = null;
  }

  document.getElementById('em-close').addEventListener('click', closeEpicModal);
  bg.addEventListener('click', function(ev){ if (ev.target === bg) closeEpicModal(); });
  document.addEventListener('keydown', function(ev){
    if (ev.key === 'Escape' && bg.style.display === 'flex') closeEpicModal();
  });

  document.getElementById('em-status').addEventListener('change', function(){
    updateStatusBadge(this.value);
  });

  var colorInputEl = document.getElementById('em-color-input');
  if (colorInputEl){
    colorInputEl.addEventListener('input', function(){ setColor(this.value, {skipInput:true}); });
  }

  var autoBtn = document.getElementById('em-autocolor');
  if (autoBtn){
    autoBtn.addEventListener('click', function(){
      if (!currentEpic || !currentEpic.project_id) return;
      var btn = this;
      btn.disabled = true;
      var orig = btn.textContent;
      btn.textContent = '…';
      apiGet('/api/project/' + currentEpic.project_id + '/epics/stats?archived=0&limit=500').then(function(r){
        btn.disabled = false; btn.textContent = orig;
        if (!r || !r.ok) { alert('Could not load palette'); return; }
        var avoid = (r.epics || [])
          .filter(function(e){ return e.id !== currentEpic.id; })
          .map(function(e){ return e.color; });
        setColor(pickFurthestColor(avoid));
      });
    });
  }

  document.getElementById('em-save').addEventListener('click', function(){
    if (!currentEpic) return;
    var btn = this;
    var body = {
      title:       titleInput.value.trim(),
      description: document.getElementById('em-desc').value,
      status:      document.getElementById('em-status').value,
      start_date:  document.getElementById('em-start').value || '',
      due_date:    document.getElementById('em-due').value   || '',
      color:       selectedColor || currentEpic.color || '#6B7280'
    };
    if (!body.title) { alert('Title is required'); return; }
    btn.disabled = true; btn.textContent = 'Saving…';
    var savedId = currentEpic.id;
    apiPatch('/api/epic/' + savedId, body).then(function(r){
      btn.disabled = false; btn.textContent = 'Save';
      if (r && r.ok) {
        if (onChangeCb) { try { onChangeCb('save', savedId); } catch(_){} }
        openEpicModal(savedId, {onChange: onChangeCb});
      } else {
        alert('Save failed' + (r && r.error ? ': ' + r.error : ''));
      }
    });
  });

  document.getElementById('em-delete').addEventListener('click', function(){
    if (!currentEpic) return;
    var id    = currentEpic.id;
    var title = currentEpic.title || ('epic #' + id);
    if (!confirm('Delete "' + title + '"?\n\nStories will NOT be deleted, but they will no longer belong to this epic.')) return;
    var btn = this;
    btn.disabled = true; btn.textContent = 'Deleting…';
    var cb = onChangeCb;
    fetch(ROOT + '/api/epic/' + id, {
      method: 'DELETE', credentials: 'same-origin',
      headers: {'X-CSRF-Token': CSRF}
    }).then(function(r){
      btn.disabled = false; btn.textContent = 'Delete epic';
      if (!r.ok) {
        return r.json().catch(function(){ return {}; }).then(function(d){
          alert('Delete failed (HTTP ' + r.status + ')' + (d && d.error ? ': ' + d.error : ''));
        });
      }
      closeEpicModal();
      if (cb) { try { cb('delete', id); return; } catch(_){} }
      // Fallback: reload page so any lists update.
      window.location.reload();
    }).catch(function(err){
      btn.disabled = false; btn.textContent = 'Delete epic';
      alert('Delete failed: ' + err);
    });
  });

  // Global delegated handler for any clickable epic trigger.
  document.addEventListener('click', function(ev){
    var chip = ev.target.closest('.col-epic-chip, [data-epic-modal-id]');
    if (!chip) return;
    ev.preventDefault();
    ev.stopPropagation();
    var id = parseInt(chip.getAttribute('data-epic-id') || chip.getAttribute('data-epic-modal-id') || '0', 10);
    if (id) openEpicModal(id);
  }, true);

  // ── Right-click context menu (Edit + Auto-color) ──────────────────────────
  var ctxMenu = null;
  function closeCtxMenu(){
    if (ctxMenu && ctxMenu.parentNode) ctxMenu.parentNode.removeChild(ctxMenu);
    ctxMenu = null;
  }
  function showCtxMenu(x, y, epicId, row){
    closeCtxMenu();
    var menu = document.createElement('div');
    menu.className = 'epic-ctx-menu';
    menu.style.cssText =
      'position:fixed;z-index:36000;min-width:170px;background:#fff;'
    + 'border:1.5px solid var(--c-gray-200,#DDD0B8);border-radius:8px;'
    + 'box-shadow:0 6px 24px rgba(0,0,0,.18);padding:4px 0;font-size:13px;';
    function item(label, onClick){
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.style.cssText =
        'display:block;width:100%;text-align:left;border:none;background:transparent;'
      + 'padding:8px 14px;cursor:pointer;color:var(--c-gray-900,#1A1208);font:inherit;';
      b.addEventListener('mouseenter', function(){ b.style.background = 'var(--c-gray-100,#EDE4D0)'; });
      b.addEventListener('mouseleave', function(){ b.style.background = 'transparent'; });
      b.addEventListener('click', function(ev){
        ev.preventDefault(); ev.stopPropagation();
        closeCtxMenu();
        onClick();
      });
      menu.appendChild(b);
    }
    item('✏️  Edit epic…', function(){
      openEpicModal(epicId, { onChange: function(){
        // Soft refresh: if the row is on a manage page, click triggers reset there via its own onChange.
        // Otherwise reload non-destructively.
        if (typeof window.__epicListReset === 'function') window.__epicListReset();
      }});
    });
    item('🎨  Auto-color', function(){
      autoColorEpic(epicId);
    });
    document.body.appendChild(menu);
    // Clamp to viewport
    var vw = window.innerWidth, vh = window.innerHeight;
    menu.style.left = Math.min(x, vw - 200) + 'px';
    menu.style.top  = Math.min(y, vh -  90) + 'px';
    ctxMenu = menu;
  }
  document.addEventListener('click',       closeCtxMenu, true);
  document.addEventListener('scroll',      closeCtxMenu, true);
  window.addEventListener('resize',        closeCtxMenu);
  document.addEventListener('keydown', function(ev){ if (ev.key === 'Escape') closeCtxMenu(); });

  function autoColorEpic(epicId){
    // 1) fetch the epic to learn project_id + current color
    apiGet('/api/epic/' + epicId + '/full').then(function(d){
      if (!d || !d.ok) { alert('Could not load epic'); return; }
      var pid = d.epic.project_id;
      // 2) fetch active palette
      apiGet('/api/project/' + pid + '/epics/stats?archived=0&limit=500').then(function(r){
        if (!r || !r.ok) { alert('Could not load palette'); return; }
        var avoid = (r.epics || [])
          .filter(function(e){ return e.id !== epicId; })
          .map(function(e){ return e.color; });
        var next = pickFurthestColor(avoid);
        apiPatch('/api/epic/' + epicId, { color: next }).then(function(res){
          if (!res || !res.ok) { alert('Save failed' + (res && res.error ? ': ' + res.error : '')); return; }
          // Reflect immediately in any visible chips/rows.
          document.querySelectorAll('[data-epic-id="' + epicId + '"], [data-epic-modal-id="' + epicId + '"]').forEach(function(el){
            // Update background tint for chips
            if (el.classList.contains('col-epic-chip')) el.style.background = next;
            // Update color swatch in manage rows
            var swatch = el.closest && el.closest('.ep-mgr-row') && el.closest('.ep-mgr-row').querySelector('.ep-mgr-color');
            if (swatch) swatch.style.background = next;
          });
          // Manage page also has .ep-mgr-row[data-id]
          var mgrRow = document.querySelector('.ep-mgr-row[data-id="' + epicId + '"]');
          if (mgrRow) {
            var sw = mgrRow.querySelector('.ep-mgr-color');
            if (sw) sw.style.background = next;
            var ci = mgrRow.querySelector('.ep-mgr-color-input');
            if (ci) ci.value = next;
          }
          // NOTE: deliberately do NOT call __epicListReset() here.
          // Auto-color only changes the color, which we already reflect in
          // the DOM above. Resetting the list would reload from page 1 and
          // throw away the user's scroll position.
        });
      });
    });
  }

  document.addEventListener('contextmenu', function(ev){
    var trigger = ev.target.closest(
      '.col-epic-chip, [data-epic-modal-id], .ep-mgr-row[data-id]'
    );
    if (!trigger) return;
    var id = parseInt(
      trigger.getAttribute('data-epic-id')
      || trigger.getAttribute('data-epic-modal-id')
      || trigger.getAttribute('data-id')
      || '0', 10
    );
    if (!id) return;
    ev.preventDefault();
    showCtxMenu(ev.clientX, ev.clientY, id, trigger);
  });

  window.openEpicModal  = openEpicModal;
  window.closeEpicModal = closeEpicModal;
}());
