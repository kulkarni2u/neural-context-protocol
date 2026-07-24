'use strict';

/* NCP Memory — vanilla JS frontend. No build step, no external requests. */

(function () {

  var TOKEN_KEY = 'ncp_token';
  var REFRESH_MS = 3000;
  var TICK_MS = 1000;
  var CHUNK_LIMIT = 100;

  var PALETTE = [
    '#4f8fea', '#e0a03c', '#59c48a', '#c96bd6',
    '#e0645c', '#4fbfbf', '#8f8fea', '#c0c04f',
    '#e06ba0', '#5ce0a0', '#a0785c', '#7c9cf2'
  ];

  var WHISPER_TYPES = ['share', 'request', 'dissent', 'nudge', 'alert'];

  /* ---------------------------------------------------------------- */
  /* state                                                             */
  /* ---------------------------------------------------------------- */

  var state = {
    pipelineId: '',
    pipelines: [],
    activeTab: 'timeline',
    paused: false,
    lastUpdated: null,
    lastError: null,

    turns: { items: [] },
    timelineWhispers: { items: [] },
    expandedTurns: {},

    chunks: {
      items: [], total: 0, offset: 0, limit: CHUNK_LIMIT,
      filters: { layer: '', zone: '', src: '', written_by: '' },
      search: ''
    },
    expandedChunks: {},

    whispers: { items: [], includeExpired: false },
    expandedWhispers: {},

    status: null,
    cost: null
  };

  var authModalOpen = false;
  var pollTimer = null;
  var tickTimer = null;

  /* ---------------------------------------------------------------- */
  /* dom helpers                                                       */
  /* ---------------------------------------------------------------- */

  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  function escapeHtml(value) {
    var s = value === null || value === undefined ? '' : String(value);
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'text') node.textContent = attrs[k];
        else if (k.indexOf('on') === 0 && typeof attrs[k] === 'function') {
          node.addEventListener(k.slice(2), attrs[k]);
        } else {
          node.setAttribute(k, attrs[k]);
        }
      });
    }
    (children || []).forEach(function (c) {
      if (c) node.appendChild(c);
    });
    return node;
  }

  function fmtTime(epochSeconds) {
    if (epochSeconds === null || epochSeconds === undefined) return '—';
    var d = new Date(epochSeconds * 1000);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString(undefined, {
      month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  }

  function fmtNum(n, digits) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    if (typeof digits === 'number') return Number(n).toFixed(digits);
    return String(n);
  }

  function fmtUsd(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return '$' + Number(n).toFixed(4);
  }

  function fmtDuration(seconds) {
    if (seconds <= 0) return '0s';
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    if (m <= 0) return s + 's';
    return m + 'm ' + s + 's';
  }

  function truncate(str, n) {
    var s = str === null || str === undefined ? '' : String(str);
    if (s.length <= n) return s;
    return s.slice(0, n) + '…';
  }

  function hashStr(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) {
      h = (h * 31 + s.charCodeAt(i)) | 0;
    }
    return Math.abs(h);
  }

  function colorForAgent(agentId) {
    var id = agentId || '(unknown)';
    return PALETTE[hashStr(id) % PALETTE.length];
  }

  function whisperTypeClass(t) {
    return WHISPER_TYPES.indexOf(t) !== -1 ? t : 'other';
  }

  function trustBadgeClass(trust) {
    if (trust === null || trust === undefined) return 'badge-red';
    if (trust >= 0.8) return 'badge-green';
    if (trust >= 0.6) return 'badge-amber';
    return 'badge-red';
  }

  /* ---------------------------------------------------------------- */
  /* networking                                                        */
  /* ---------------------------------------------------------------- */

  function buildUrl(path, params) {
    var usp = new URLSearchParams();
    if (params) {
      Object.keys(params).forEach(function (k) {
        var v = params[k];
        if (v !== undefined && v !== null && v !== '') usp.set(k, v);
      });
    }
    var qs = usp.toString();
    return qs ? (path + '?' + qs) : path;
  }

  function withPipeline(params) {
    var out = {};
    Object.keys(params || {}).forEach(function (k) { out[k] = params[k]; });
    if (state.pipelineId) out.pipeline_id = state.pipelineId;
    return out;
  }

  function apiFetch(path, params) {
    var token = '';
    try { token = localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { token = ''; }
    var headers = {};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    var url = buildUrl(path, params);

    return fetch(url, { headers: headers }).then(function (resp) {
      if (resp.status === 401) {
        var err = new Error('unauthorized');
        err.kind = 'auth';
        throw err;
      }
      return resp.json().catch(function () {
        var err = new Error('Bad response from ' + path);
        err.kind = 'parse';
        throw err;
      }).then(function (body) {
        if (!resp.ok) {
          var msg = (body && (body.detail || body.error)) || ('HTTP ' + resp.status + ' from ' + path);
          var err2 = new Error(msg);
          err2.kind = 'http';
          throw err2;
        }
        return body;
      });
    }, function () {
      var err = new Error('Network error contacting ' + path);
      err.kind = 'network';
      throw err;
    });
  }

  /* ---------------------------------------------------------------- */
  /* auth modal                                                        */
  /* ---------------------------------------------------------------- */

  function openAuthModal() {
    if (authModalOpen) return;
    authModalOpen = true;
    var modal = $('#auth-modal');
    modal.hidden = false;
    var input = $('#token-input');
    try { input.value = localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { input.value = ''; }
    input.focus();
  }

  function closeAuthModal() {
    authModalOpen = false;
    $('#auth-modal').hidden = true;
  }

  /* ---------------------------------------------------------------- */
  /* error banner                                                      */
  /* ---------------------------------------------------------------- */

  function showError(message) {
    state.lastError = message;
    var banner = $('#error-banner');
    $('#error-banner-text').textContent = message;
    banner.hidden = false;
  }

  function clearError() {
    state.lastError = null;
    $('#error-banner').hidden = true;
  }

  /* ---------------------------------------------------------------- */
  /* fetchers                                                          */
  /* ---------------------------------------------------------------- */

  function refreshStatus() {
    return apiFetch('/api/status', withPipeline({})).then(function (data) {
      state.status = data;
      state.pipelines = (data && data.recent_pipelines) || [];
      state.lastUpdated = Date.now();
      renderPipelineSelect();
      if (state.activeTab === 'stats') renderStats();
    });
  }

  function refreshTimeline() {
    var turnsP = apiFetch('/api/turns', withPipeline({ limit: 100 })).then(function (data) {
      state.turns.items = (data && data.turns) || [];
    });
    var whisperP = apiFetch('/api/whispers', withPipeline({ limit: 200, include_expired: 'true' })).then(function (data) {
      state.timelineWhispers.items = (data && data.whispers) || [];
    });
    return Promise.all([turnsP, whisperP]).then(function () {
      state.lastUpdated = Date.now();
      renderTimeline();
    });
  }

  function refreshMemory() {
    var c = state.chunks;
    var params = withPipeline({
      layer: c.filters.layer,
      zone: c.filters.zone,
      src: c.filters.src,
      written_by: c.filters.written_by,
      limit: c.limit,
      offset: c.offset
    });
    return apiFetch('/api/chunks', params).then(function (data) {
      c.items = (data && data.chunks) || [];
      c.total = (data && data.total) || 0;
      state.lastUpdated = Date.now();
      renderMemory();
    });
  }

  function refreshWhispers() {
    var w = state.whispers;
    var params = withPipeline({
      limit: 100,
      include_expired: w.includeExpired ? 'true' : 'false'
    });
    return apiFetch('/api/whispers', params).then(function (data) {
      w.items = (data && data.whispers) || [];
      state.lastUpdated = Date.now();
      renderWhispers();
    });
  }

  function refreshCost() {
    return apiFetch('/api/cost', withPipeline({ limit: 10 })).then(function (data) {
      state.cost = data;
      state.lastUpdated = Date.now();
      renderStats();
    });
  }

  function refreshAll() {
    var tasks = [refreshStatus()];
    if (state.activeTab === 'timeline') tasks.push(refreshTimeline());
    else if (state.activeTab === 'memory') tasks.push(refreshMemory());
    else if (state.activeTab === 'whispers') tasks.push(refreshWhispers());
    else if (state.activeTab === 'stats') tasks.push(refreshCost());

    return Promise.allSettled(tasks).then(function (results) {
      var authFailed = false;
      var errors = [];
      results.forEach(function (r) {
        if (r.status === 'rejected') {
          var reason = r.reason || {};
          if (reason.kind === 'auth') authFailed = true;
          else errors.push(reason.message || String(reason));
        }
      });

      renderHeader();

      if (authFailed) {
        openAuthModal();
        return;
      }
      if (errors.length) {
        showError(errors.join(' — '));
      } else {
        clearError();
      }
    });
  }

  /* ---------------------------------------------------------------- */
  /* header / pipeline select / tabs                                   */
  /* ---------------------------------------------------------------- */

  function renderPipelineSelect() {
    var select = $('#pipeline-select');
    var current = select.value;
    var html = '<option value="">All pipelines</option>';
    state.pipelines.forEach(function (p) {
      html += '<option value="' + escapeHtml(p.pipeline_id) + '">' +
        escapeHtml(p.pipeline_id) + ' (' + escapeHtml(String(p.chunk_count)) + ')</option>';
    });
    select.innerHTML = html;
    // preserve current selection (state.pipelineId is authoritative)
    select.value = state.pipelineId;
    if (select.value !== state.pipelineId) {
      // selected pipeline no longer in recent list; keep option so choice isn't silently lost
      if (state.pipelineId) {
        var opt = document.createElement('option');
        opt.value = state.pipelineId;
        opt.textContent = state.pipelineId;
        select.appendChild(opt);
        select.value = state.pipelineId;
      }
    }
  }

  function renderHeader() {
    var span = $('#last-updated');
    if (state.lastUpdated) {
      span.textContent = 'updated ' + new Date(state.lastUpdated).toLocaleTimeString();
    } else {
      span.textContent = 'never updated';
    }
    var toggle = $('#refresh-toggle');
    if (state.paused) {
      toggle.textContent = 'Resume';
      toggle.setAttribute('aria-pressed', 'true');
    } else if (document.hidden) {
      toggle.textContent = 'Pause (tab hidden)';
      toggle.setAttribute('aria-pressed', 'false');
    } else {
      toggle.textContent = 'Pause';
      toggle.setAttribute('aria-pressed', 'false');
    }
  }

  function setActiveTab(tab, skipFetch) {
    state.activeTab = tab;
    $all('.tab-btn').forEach(function (btn) {
      var isActive = btn.dataset.tab === tab;
      btn.classList.toggle('active', isActive);
    });
    ['timeline', 'memory', 'whispers', 'stats'].forEach(function (t) {
      var view = $('#view-' + t);
      if (view) view.hidden = (t !== tab);
    });
    if (skipFetch) return;
    // fetch fresh data for the newly active tab immediately
    if (tab === 'timeline') refreshTimeline().catch(handleStandaloneError);
    else if (tab === 'memory') refreshMemory().catch(handleStandaloneError);
    else if (tab === 'whispers') refreshWhispers().catch(handleStandaloneError);
    else if (tab === 'stats') refreshCost().catch(handleStandaloneError);
  }

  function handleStandaloneError(err) {
    if (err && err.kind === 'auth') { openAuthModal(); return; }
    showError((err && err.message) || String(err));
  }

  /* ---------------------------------------------------------------- */
  /* timeline view                                                     */
  /* ---------------------------------------------------------------- */

  function renderTimeline() {
    var turns = state.turns.items || [];
    var whispers = state.timelineWhispers.items || [];

    var legend = $('#timeline-legend');
    var grid = $('#timeline-grid');
    var empty = $('#timeline-empty');

    if (!turns.length && !whispers.length) {
      legend.innerHTML = '';
      grid.innerHTML = '';
      grid.style.gridTemplateColumns = '';
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    var agentIds = [];
    var seen = {};
    turns.forEach(function (t) {
      if (!seen[t.agent_id]) { seen[t.agent_id] = true; agentIds.push(t.agent_id); }
    });

    legend.innerHTML = '';
    agentIds.forEach(function (id) {
      var item = el('span', { class: 'legend-item' }, [
        el('span', { class: 'legend-dot' }),
        el('span', { class: 'mono', text: id })
      ]);
      item.firstChild.style.background = colorForAgent(id);
      legend.appendChild(item);
    });

    var cols = Math.max(agentIds.length, 1);
    grid.style.gridTemplateColumns = 'repeat(' + cols + ', minmax(240px, 1fr))';
    grid.innerHTML = '';

    var items = [];
    turns.forEach(function (t) { items.push({ kind: 'turn', ts: t.created_at || 0, data: t }); });
    whispers.forEach(function (w) { items.push({ kind: 'whisper', ts: w.created_at || 0, data: w }); });
    items.sort(function (a, b) { return a.ts - b.ts; });

    var row = 1;
    items.forEach(function (item) {
      var node;
      if (item.kind === 'turn') {
        node = buildTurnCard(item.data);
        var laneIdx = agentIds.indexOf(item.data.agent_id);
        node.style.gridColumn = String((laneIdx >= 0 ? laneIdx : 0) + 1);
      } else {
        node = buildWhisperPill(item.data);
        node.style.gridColumn = '1 / -1';
      }
      node.style.gridRow = String(row);
      grid.appendChild(node);
      row++;
    });
  }

  function buildTurnCard(t) {
    var expanded = !!state.expandedTurns[t.turn_id];
    var text = expanded ? (t.result_full !== undefined && t.result_full !== null ? t.result_full : t.result) : t.result;
    var meta = [];
    if (t.task) meta.push(t.task);
    if (t.slot) meta.push(t.slot);

    var card = el('div', { class: 'turn-card' }, [
      el('div', { class: 'turn-card-head' }, [
        el('span', { class: 'agent-name mono', text: t.agent_id || '(unknown)' }),
        el('span', { class: 'turn-time', text: fmtTime(t.created_at) })
      ]),
      meta.length ? el('div', { class: 'turn-task', text: meta.join(' · ') }) : null,
      el('div', { class: 'turn-result', text: text || '' })
    ]);
    card.style.setProperty('--lane-color', colorForAgent(t.agent_id));
    card.title = expanded ? 'Click to collapse' : 'Click to expand full result';
    card.addEventListener('click', function () {
      if (state.expandedTurns[t.turn_id]) delete state.expandedTurns[t.turn_id];
      else state.expandedTurns[t.turn_id] = true;
      renderTimeline();
    });
    return card;
  }

  function buildWhisperPill(w) {
    var cls = whisperTypeClass(w.whisper_type);
    var pill = el('div', { class: 'whisper-pill whisper-type-' + cls }, [
      el('span', { class: 'mono', text: w.from_agent || '?' }),
      el('span', { text: ' → ' }),
      el('span', { class: 'mono', text: w.target || '?' }),
      el('span', { class: 'whisper-type-label', text: w.whisper_type || 'other' })
    ]);
    pill.title = fmtTime(w.created_at) + (w.payload ? (' — ' + w.payload) : '');
    return pill;
  }

  /* ---------------------------------------------------------------- */
  /* memory view                                                       */
  /* ---------------------------------------------------------------- */

  function uniqueValues(items, field) {
    var seen = {};
    var out = [];
    items.forEach(function (item) {
      var v = item[field];
      if (v === null || v === undefined || v === '') return;
      if (!seen[v]) { seen[v] = true; out.push(v); }
    });
    out.sort();
    return out;
  }

  function populateSelect(select, values, placeholder, current) {
    var html = '<option value="">' + escapeHtml(placeholder) + '</option>';
    values.forEach(function (v) {
      html += '<option value="' + escapeHtml(v) + '">' + escapeHtml(v) + '</option>';
    });
    select.innerHTML = html;
    select.value = current || '';
  }

  function renderMemory() {
    var c = state.chunks;
    var tbody = $('#memory-tbody');
    var empty = $('#memory-empty');

    populateSelect($('#memory-filter-layer'), uniqueValues(c.items, 'layer'), 'Layer: all', c.filters.layer);
    populateSelect($('#memory-filter-zone'), uniqueValues(c.items, 'zone'), 'Zone: all', c.filters.zone);
    populateSelect($('#memory-filter-src'), uniqueValues(c.items, 'src'), 'Src: all', c.filters.src);
    populateSelect($('#memory-filter-written-by'), uniqueValues(c.items, 'written_by'), 'Written by: all', c.filters.written_by);

    var search = c.search.trim().toLowerCase();
    var rows = c.items;
    if (search) {
      rows = rows.filter(function (chunk) {
        return String(chunk.content || '').toLowerCase().indexOf(search) !== -1;
      });
    }

    tbody.innerHTML = '';
    if (!rows.length) {
      empty.hidden = false;
      empty.textContent = c.items.length
        ? 'No chunks match the current filters/search.'
        : 'No memory chunks yet — agents write chunks via ncp_write_memory.';
    } else {
      empty.hidden = true;
      rows.forEach(function (chunk) { appendChunkRows(tbody, chunk); });
    }

    var start = c.total === 0 ? 0 : c.offset + 1;
    var end = Math.min(c.offset + c.items.length, c.total);
    $('#memory-pager-label').textContent = start + '–' + end + ' of ' + c.total;
    $('#memory-prev').disabled = c.offset <= 0;
    $('#memory-next').disabled = c.offset + c.limit >= c.total;
  }

  function appendChunkRows(tbody, chunk) {
    var trustBadge = el('span', {
      class: 'badge ' + trustBadgeClass(chunk.base_trust),
      text: chunk.base_trust === null || chunk.base_trust === undefined ? '—' : Number(chunk.base_trust).toFixed(2)
    });

    var stateTag = null;
    if (chunk.tombstoned) stateTag = el('span', { class: 'tag', text: 'tombstoned' });
    else if (chunk.superseded_by) stateTag = el('span', { class: 'tag', text: 'superseded' });

    var previewSpan = el('span', { class: 'content-preview' });
    previewSpan.textContent = truncate(chunk.content, 120);
    var contentCell = el('td', { class: 'content-cell' }, [previewSpan, stateTag]);

    var tr = el('tr', { class: 'row-clickable' + ((chunk.tombstoned || chunk.superseded_by) ? ' row-dim' : '') }, [
      el('td', { class: 'mono', text: fmtTime(chunk.created_at) }),
      el('td', { text: chunk.layer || '—' }),
      el('td', { text: chunk.zone || '—' }),
      el('td', { text: chunk.src || '—' }),
      el('td', { class: 'mono', text: chunk.written_by || '—' }),
      el('td', {}, [trustBadge]),
      contentCell
    ]);

    var expanded = !!state.expandedChunks[chunk.chunk_id];
    tr.addEventListener('click', function () {
      if (state.expandedChunks[chunk.chunk_id]) delete state.expandedChunks[chunk.chunk_id];
      else state.expandedChunks[chunk.chunk_id] = true;
      renderMemory();
    });
    tbody.appendChild(tr);

    if (expanded) {
      var metaFields = [
        ['chunk_id', chunk.chunk_id], ['caused_by', chunk.caused_by],
        ['supersedes', chunk.supersedes], ['superseded_by', chunk.superseded_by],
        ['version', chunk.version], ['tombstoned', chunk.tombstoned ? 'true' : 'false'],
        ['pipeline_id', chunk.pipeline_id], ['scope', chunk.scope],
        ['chunk_type', chunk.chunk_type]
      ];
      var dl = el('dl', { class: 'meta-grid' }, []);
      metaFields.forEach(function (pair) {
        dl.appendChild(el('dt', { text: pair[0] }));
        dl.appendChild(el('dd', { text: pair[1] === null || pair[1] === undefined || pair[1] === '' ? '—' : String(pair[1]) }));
      });
      var pre = el('pre', { class: 'detail-content' });
      pre.textContent = chunk.content || '';
      var detailTd = el('td', { colspan: '7' }, [pre, dl]);
      var detailTr = el('tr', { class: 'detail-row' }, [detailTd]);
      tbody.appendChild(detailTr);
    }
  }

  /* ---------------------------------------------------------------- */
  /* whispers view                                                     */
  /* ---------------------------------------------------------------- */

  function renderWhispers() {
    var w = state.whispers;
    var tbody = $('#whispers-tbody');
    var empty = $('#whispers-empty');

    tbody.innerHTML = '';
    if (!w.items.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    w.items.forEach(function (wh) { appendWhisperRows(tbody, wh); });
  }

  function whisperRemainingSeconds(wh) {
    var now = Date.now() / 1000;
    return (wh.expires_at || 0) - now;
  }

  function appendWhisperRows(tbody, wh) {
    var cls = whisperTypeClass(wh.whisper_type);
    var typePill = el('span', { class: 'whisper-pill whisper-type-' + cls }, [
      el('span', { class: 'whisper-type-label', text: wh.whisper_type || 'other' })
    ]);

    var remaining = whisperRemainingSeconds(wh);
    var isExpired = wh.expired || remaining <= 0;
    var statusCell = el('td', { class: 'whisper-status' });
    if (isExpired) {
      statusCell.appendChild(el('span', { class: 'tag', text: 'expired' }));
    } else {
      var ttlSpan = el('span', { class: 'mono whisper-ttl', text: fmtDuration(remaining) });
      ttlSpan.dataset.expiresAt = String(wh.expires_at || 0);
      statusCell.appendChild(document.createTextNode('pending · '));
      statusCell.appendChild(ttlSpan);
    }

    var expanded = !!state.expandedWhispers[wh.whisper_id];
    var payloadCell = el('td', { class: expanded ? '' : 'content-preview' });
    payloadCell.textContent = expanded ? (wh.payload || '') : truncate(wh.payload, 120);
    if (expanded) payloadCell.style.whiteSpace = 'pre-wrap';

    var tr = el('tr', { class: 'row-clickable' + (isExpired ? ' row-dim' : '') }, [
      el('td', { class: 'mono', text: fmtTime(wh.created_at) }),
      el('td', {}, [
        el('span', { class: 'mono', text: wh.from_agent || '?' }),
        document.createTextNode(' → '),
        el('span', { class: 'mono', text: wh.target || '?' })
      ]),
      el('td', {}, [typePill]),
      el('td', { class: 'mono', text: wh.confidence === null || wh.confidence === undefined ? '—' : Number(wh.confidence).toFixed(2) }),
      payloadCell,
      statusCell
    ]);
    tr.addEventListener('click', function (ev) {
      if (state.expandedWhispers[wh.whisper_id]) delete state.expandedWhispers[wh.whisper_id];
      else state.expandedWhispers[wh.whisper_id] = true;
      renderWhispers();
    });
    tbody.appendChild(tr);
  }

  function tickWhisperCountdowns() {
    if (state.activeTab !== 'whispers') return;
    $all('.whisper-ttl').forEach(function (span) {
      var expiresAt = Number(span.dataset.expiresAt || 0);
      var remaining = expiresAt - Date.now() / 1000;
      if (remaining <= 0) {
        renderWhispers();
        return;
      }
      span.textContent = fmtDuration(remaining);
    });
  }

  /* ---------------------------------------------------------------- */
  /* stats view                                                        */
  /* ---------------------------------------------------------------- */

  function statCard(label, value) {
    return el('div', { class: 'card' }, [
      el('div', { class: 'card-label', text: label }),
      el('div', { class: 'card-value', text: value })
    ]);
  }

  function renderStats() {
    var cardsEl = $('#stats-cards');
    var emptyEl = $('#stats-empty');
    var status = state.status;

    if (!status || !status.overview) {
      cardsEl.innerHTML = '';
      emptyEl.hidden = false;
    } else {
      emptyEl.hidden = true;
      var ov = status.overview;
      cardsEl.innerHTML = '';
      cardsEl.appendChild(statCard('Chunks', fmtNum(ov.chunk_count)));
      cardsEl.appendChild(statCard('Whispers', fmtNum(ov.whisper_count)));
      cardsEl.appendChild(statCard('Turns', fmtNum(ov.turn_record_count)));
      cardsEl.appendChild(statCard('Pipelines', fmtNum(ov.pipeline_count)));
      cardsEl.appendChild(statCard('Tombstones', fmtNum(ov.tombstone_count)));
      cardsEl.appendChild(statCard('Total cost', fmtUsd(ov.cost_usd_total)));
    }

    var layers = (status && status.layer_counts) || {};
    var layerKeys = Object.keys(layers);
    var layerContainer = $('#stats-layers');
    var layerEmpty = $('#stats-layers-empty');
    layerContainer.innerHTML = '';
    if (!layerKeys.length) {
      layerEmpty.hidden = false;
    } else {
      layerEmpty.hidden = true;
      var max = Math.max.apply(null, layerKeys.map(function (k) { return layers[k]; }));
      layerKeys.sort(function (a, b) { return layers[b] - layers[a]; });
      layerKeys.forEach(function (k) {
        var pct = max > 0 ? Math.round((layers[k] / max) * 100) : 0;
        var fill = el('div', { class: 'bar-fill' });
        fill.style.width = pct + '%';
        var row = el('div', { class: 'bar-row' }, [
          el('span', { class: 'mono', text: k }),
          el('div', { class: 'bar-track' }, [fill]),
          el('span', { class: 'mono', text: String(layers[k]) })
        ]);
        layerContainer.appendChild(row);
      });
    }

    renderCost();
  }

  function renderCost() {
    var cost = state.cost;
    var costCards = $('#cost-cards');
    var costEmpty = $('#cost-empty');
    costCards.innerHTML = '';

    if (!cost || !cost.summary || !cost.summary.entry_count) {
      costEmpty.hidden = false;
    } else {
      costEmpty.hidden = true;
      var s = cost.summary;
      costCards.appendChild(statCard('Input tokens', fmtNum(s.input_tokens_total)));
      costCards.appendChild(statCard('Output tokens', fmtNum(s.output_tokens_total)));
      costCards.appendChild(statCard('Cache reads', fmtNum(s.cache_read_tokens_total)));
      costCards.appendChild(statCard('Avg latency', s.avg_latency_ms !== undefined && s.avg_latency_ms !== null ? Math.round(s.avg_latency_ms) + 'ms' : '—'));
      costCards.appendChild(statCard('Total cost', fmtUsd(s.cost_usd_total)));
      costCards.appendChild(statCard('Entries', fmtNum(s.entry_count)));
    }

    var byAgent = (cost && cost.by_agent) || [];
    var tbody = $('#cost-by-agent-tbody');
    var byAgentEmpty = $('#cost-by-agent-empty');
    tbody.innerHTML = '';
    if (!byAgent.length) {
      byAgentEmpty.hidden = false;
    } else {
      byAgentEmpty.hidden = true;
      byAgent.forEach(function (row) {
        tbody.appendChild(el('tr', {}, [
          el('td', { class: 'mono', text: row.agent_id || '—' }),
          el('td', { text: fmtNum(row.turns) }),
          el('td', { class: 'mono', text: fmtUsd(row.cost_usd_total) })
        ]));
      });
    }

    var memo = state.status && state.status.memoization;
    var memoCards = $('#memo-cards');
    var memoEmpty = $('#memo-empty');
    memoCards.innerHTML = '';
    if (!memo) {
      memoEmpty.hidden = false;
    } else {
      memoEmpty.hidden = true;
      memoCards.appendChild(statCard('Hits', fmtNum(memo.hits)));
      memoCards.appendChild(statCard('Misses', fmtNum(memo.misses)));
      memoCards.appendChild(statCard('Entries', fmtNum(memo.entry_count)));
      memoCards.appendChild(statCard('Tokens saved', fmtNum(memo.estimated_tokens_saved)));
    }
  }

  /* ---------------------------------------------------------------- */
  /* wiring                                                            */
  /* ---------------------------------------------------------------- */

  function wireEvents() {
    $all('.tab-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { setActiveTab(btn.dataset.tab); });
    });

    $('#pipeline-select').addEventListener('change', function (e) {
      state.pipelineId = e.target.value;
      state.chunks.offset = 0;
      refreshAll();
    });

    $('#refresh-toggle').addEventListener('click', function () {
      state.paused = !state.paused;
      renderHeader();
    });

    $('#token-btn').addEventListener('click', openAuthModal);
    $('#token-modal-close').addEventListener('click', closeAuthModal);
    $('#error-banner-close').addEventListener('click', clearError);

    $('#token-form').addEventListener('submit', function (e) {
      e.preventDefault();
      var val = $('#token-input').value.trim();
      try { localStorage.setItem(TOKEN_KEY, val); } catch (err) { /* ignore storage errors */ }
      closeAuthModal();
      refreshAll();
    });

    // memory filters
    ['layer', 'zone', 'src', 'written-by'].forEach(function (name) {
      var field = name === 'written-by' ? 'written_by' : name;
      $('#memory-filter-' + name).addEventListener('change', function (e) {
        state.chunks.filters[field] = e.target.value;
        state.chunks.offset = 0;
        refreshMemory().catch(handleStandaloneError);
      });
    });
    $('#memory-search').addEventListener('input', function (e) {
      state.chunks.search = e.target.value;
      renderMemory();
    });
    $('#memory-prev').addEventListener('click', function () {
      state.chunks.offset = Math.max(0, state.chunks.offset - state.chunks.limit);
      refreshMemory().catch(handleStandaloneError);
    });
    $('#memory-next').addEventListener('click', function () {
      if (state.chunks.offset + state.chunks.limit < state.chunks.total) {
        state.chunks.offset += state.chunks.limit;
        refreshMemory().catch(handleStandaloneError);
      }
    });

    $('#whisper-include-expired').addEventListener('change', function (e) {
      state.whispers.includeExpired = e.target.checked;
      refreshWhispers().catch(handleStandaloneError);
    });

    document.addEventListener('visibilitychange', function () {
      renderHeader();
      if (!document.hidden && !state.paused) refreshAll();
    });
  }

  function pollLoop() {
    pollTimer = setInterval(function () {
      if (state.paused || document.hidden) { renderHeader(); return; }
      refreshAll();
    }, REFRESH_MS);
    tickTimer = setInterval(function () {
      tickWhisperCountdowns();
      renderHeader();
    }, TICK_MS);
  }

  function renderAllViews() {
    // Render every view once from whatever is currently in `state` (initially
    // all empty) so the page shows proper empty-state text — not a blank
    // screen — even if every network request fails (e.g. opened from disk).
    renderHeader();
    renderTimeline();
    renderMemory();
    renderWhispers();
    renderStats();
  }

  function init() {
    wireEvents();
    setActiveTab('timeline', true);
    renderAllViews();
    refreshAll();
    pollLoop();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
