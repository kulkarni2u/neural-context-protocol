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

    graph: {
      nodes: [], edges: [],
      positions: {}, viewBox: null, initialViewBox: null,
      selectedId: null, lastKey: ''
    },

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

  var SVG_NS = 'http://www.w3.org/2000/svg';

  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    }
    return node;
  }

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

  function refreshGraph() {
    return apiFetch('/api/graph', withPipeline({ limit: 200 })).then(function (data) {
      state.graph.nodes = (data && data.nodes) || [];
      state.graph.edges = (data && data.edges) || [];
      state.lastUpdated = Date.now();
      renderGraph();
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
    else if (state.activeTab === 'graph') tasks.push(refreshGraph());
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
    ['timeline', 'memory', 'whispers', 'graph', 'stats'].forEach(function (t) {
      var view = $('#view-' + t);
      if (view) view.hidden = (t !== tab);
    });
    if (skipFetch) return;
    // fetch fresh data for the newly active tab immediately
    if (tab === 'timeline') refreshTimeline().catch(handleStandaloneError);
    else if (tab === 'memory') refreshMemory().catch(handleStandaloneError);
    else if (tab === 'whispers') refreshWhispers().catch(handleStandaloneError);
    else if (tab === 'graph') refreshGraph().catch(handleStandaloneError);
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
  /* graph view                                                        */
  /* ---------------------------------------------------------------- */

  var GRAPH_W = 1000;
  var GRAPH_H = 700;

  function nodeRadius(trust) {
    var t = trust === null || trust === undefined || isNaN(trust) ? 0.5 : Number(trust);
    t = Math.max(0.1, Math.min(1.0, t));
    return 6 + (t - 0.1) / 0.9 * (12 - 6);
  }

  function nodeById(g, id) {
    for (var i = 0; i < g.nodes.length; i++) {
      if (g.nodes[i].chunk_id === id) return g.nodes[i];
    }
    return null;
  }

  function computeGraphKey(nodes, edges) {
    var nodeIds = nodes.map(function (n) { return n.chunk_id; }).slice().sort();
    var edgeKeys = edges.map(function (e) { return e.src + '>' + e.dst + ':' + e.type; }).slice().sort();
    return nodeIds.join(',') + '|' + edgeKeys.join(',');
  }

  // Deterministic pseudo-random unit value in [0, 1) seeded from a string —
  // used only for initial node placement so layout is stable across polls.
  function seededUnit(id, salt) {
    return (hashStr(String(id) + salt) % 100000) / 100000;
  }

  // Small Fruchterman-Reingold style force simulation: mutual repulsion
  // between all node pairs, spring attraction along edges, and a weak
  // centering pull. Runs synchronously for a fixed iteration count and
  // produces static positions (no continuous animation).
  function layoutGraph(g) {
    var nodes = g.nodes;
    var edges = g.edges;
    var pos = {};
    nodes.forEach(function (n) {
      pos[n.chunk_id] = {
        x: seededUnit(n.chunk_id, ':x') * GRAPH_W,
        y: seededUnit(n.chunk_id, ':y') * GRAPH_H
      };
    });

    var n = nodes.length;
    var k = Math.sqrt((GRAPH_W * GRAPH_H) / Math.max(n, 1));
    var iterations = n > 120 ? 150 : 260;
    var cx = GRAPH_W / 2, cy = GRAPH_H / 2;

    for (var iter = 0; iter < iterations; iter++) {
      var disp = {};
      nodes.forEach(function (nd) { disp[nd.chunk_id] = { x: 0, y: 0 }; });

      for (var i = 0; i < n; i++) {
        for (var j = i + 1; j < n; j++) {
          var a = nodes[i].chunk_id, b = nodes[j].chunk_id;
          var dx = pos[a].x - pos[b].x, dy = pos[a].y - pos[b].y;
          var dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
          var force = (k * k) / dist;
          var fx = (dx / dist) * force, fy = (dy / dist) * force;
          disp[a].x += fx; disp[a].y += fy;
          disp[b].x -= fx; disp[b].y -= fy;
        }
      }

      edges.forEach(function (e) {
        var pa = pos[e.src], pb = pos[e.dst];
        if (!pa || !pb) return;
        var dx = pa.x - pb.x, dy = pa.y - pb.y;
        var dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        var force = (dist * dist) / k * 0.5;
        var fx = (dx / dist) * force, fy = (dy / dist) * force;
        disp[e.src].x -= fx; disp[e.src].y -= fy;
        disp[e.dst].x += fx; disp[e.dst].y += fy;
      });

      nodes.forEach(function (nd) {
        var d = disp[nd.chunk_id];
        var p = pos[nd.chunk_id];
        d.x += (cx - p.x) * 0.01;
        d.y += (cy - p.y) * 0.01;
        var dlen = Math.sqrt(d.x * d.x + d.y * d.y) || 0.01;
        var lim = Math.min(dlen, 30);
        p.x += (d.x / dlen) * lim;
        p.y += (d.y / dlen) * lim;
      });
    }

    // Normalize the settled layout into a fixed GRAPH_W x GRAPH_H box so node
    // radii (6-12 SVG units) stay legible regardless of how far the force
    // simulation spread the raw coordinates. Fitting the viewBox to raw layout
    // extents instead makes nodes microscopic when isolated nodes drift wide.
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach(function (nd) {
      var p = pos[nd.chunk_id];
      minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x);
      minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);
    });
    if (!isFinite(minX)) { minX = 0; maxX = GRAPH_W; minY = 0; maxY = GRAPH_H; }

    var pad = 70;
    var spanX = maxX - minX, spanY = maxY - minY;
    var innerW = GRAPH_W - 2 * pad, innerH = GRAPH_H - 2 * pad;
    var scale = Math.min(
      spanX > 1e-6 ? innerW / spanX : innerW,
      spanY > 1e-6 ? innerH / spanY : innerH
    );
    if (!isFinite(scale) || scale <= 0) scale = 1;
    // Center the scaled content within the fixed box.
    var offX = pad + (innerW - spanX * scale) / 2;
    var offY = pad + (innerH - spanY * scale) / 2;
    nodes.forEach(function (nd) {
      var p = pos[nd.chunk_id];
      p.x = offX + (p.x - minX) * scale;
      p.y = offY + (p.y - minY) * scale;
    });

    // Min-separation relaxation in the normalized box: the spring force pulls
    // linked nodes nearly on top of each other, hiding their edge. Push any
    // pair closer than minSep apart so every node (and edge) stays visible,
    // then clamp back inside the padded box.
    var minSep = 90;
    for (var s = 0; s < 60; s++) {
      for (var a2 = 0; a2 < n; a2++) {
        for (var b2 = a2 + 1; b2 < n; b2++) {
          var pa2 = pos[nodes[a2].chunk_id], pb2 = pos[nodes[b2].chunk_id];
          var ddx = pa2.x - pb2.x, ddy = pa2.y - pb2.y;
          var d2 = Math.sqrt(ddx * ddx + ddy * ddy) || 0.01;
          if (d2 < minSep) {
            var push = (minSep - d2) / 2;
            var ux = ddx / d2, uy = ddy / d2;
            pa2.x += ux * push; pa2.y += uy * push;
            pb2.x -= ux * push; pb2.y -= uy * push;
          }
        }
      }
    }
    nodes.forEach(function (nd) {
      var p = pos[nd.chunk_id];
      p.x = Math.max(pad, Math.min(GRAPH_W - pad, p.x));
      p.y = Math.max(pad, Math.min(GRAPH_H - pad, p.y));
    });

    g.positions = pos;
    g.viewBox = { x: 0, y: 0, w: GRAPH_W, h: GRAPH_H };
    g.initialViewBox = { x: 0, y: 0, w: GRAPH_W, h: GRAPH_H };
  }

  function edgeDashClass(type) {
    if (type === 'caused_by') return 'graph-edge-solid';
    if (type === 'supersedes') return 'graph-edge-dashed';
    return 'graph-edge-dotted';
  }

  function legendDashClass(type) {
    if (type === 'caused_by') return '';
    if (type === 'supersedes') return 'legend-line-dashed';
    return 'legend-line-dotted';
  }

  function drawGraphSvg(svg, g) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute('viewBox', g.viewBox.x + ' ' + g.viewBox.y + ' ' + g.viewBox.w + ' ' + g.viewBox.h);

    var defs = svgEl('defs', {});
    var marker = svgEl('marker', {
      id: 'graph-arrow', viewBox: '0 0 10 10', refX: '9', refY: '5',
      markerWidth: '7', markerHeight: '7', orient: 'auto-start-reverse'
    });
    var arrowPath = svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: 'var(--text-muted)' });
    marker.appendChild(arrowPath);
    defs.appendChild(marker);
    svg.appendChild(defs);

    var bg = svgEl('rect', {
      id: 'graph-bg',
      x: String(g.initialViewBox.x - 5000), y: String(g.initialViewBox.y - 5000),
      width: '10000', height: '10000', fill: 'transparent'
    });
    svg.appendChild(bg);

    var edgeLayer = svgEl('g', { class: 'graph-edges' });
    g.edges.forEach(function (e) {
      var pa = g.positions[e.src], pb = g.positions[e.dst];
      if (!pa || !pb) return;
      var dx = pb.x - pa.x, dy = pb.y - pa.y;
      var dist = Math.sqrt(dx * dx + dy * dy) || 1;
      var dstNode = nodeById(g, e.dst);
      var gap = nodeRadius(dstNode && dstNode.base_trust) + 6;
      var ex = pb.x - (dx / dist) * gap, ey = pb.y - (dy / dist) * gap;
      var line = svgEl('line', {
        x1: String(pa.x), y1: String(pa.y), x2: String(ex), y2: String(ey),
        class: 'graph-edge ' + edgeDashClass(e.type),
        'marker-end': 'url(#graph-arrow)'
      });
      var title = svgEl('title', {});
      title.textContent = (e.type || 'edge') + ': ' + e.src + ' → ' + e.dst;
      line.appendChild(title);
      edgeLayer.appendChild(line);
    });
    svg.appendChild(edgeLayer);

    var nodeLayer = svgEl('g', { class: 'graph-nodes' });
    g.nodes.forEach(function (nd) {
      var p = g.positions[nd.chunk_id];
      if (!p) return;
      var r = nodeRadius(nd.base_trust);
      var group = svgEl('g', { class: 'graph-node', transform: 'translate(' + p.x + ',' + p.y + ')' });

      if (g.selectedId === nd.chunk_id) {
        group.appendChild(svgEl('circle', { r: String(r + 5), class: 'graph-node-ring' }));
      }

      var circle = svgEl('circle', { r: String(r), fill: colorForAgent(nd.written_by), class: 'graph-node-circle' });
      var title = svgEl('title', {});
      title.textContent = 'layer: ' + (nd.layer || '—') +
        '\nsrc: ' + (nd.src || '—') +
        '\ntrust: ' + (nd.base_trust === null || nd.base_trust === undefined ? '—' : Number(nd.base_trust).toFixed(2));
      circle.appendChild(title);
      group.appendChild(circle);

      var label = svgEl('text', { class: 'graph-node-label', y: String(r + 13), 'text-anchor': 'middle' });
      label.textContent = truncate(nd.written_by || nd.layer || '', 14);
      group.appendChild(label);

      group.addEventListener('click', function (evt) {
        evt.stopPropagation();
        g.selectedId = nd.chunk_id;
        renderGraph();
      });

      nodeLayer.appendChild(group);
    });
    svg.appendChild(nodeLayer);
  }

  function renderGraphLegend(container, g) {
    container.innerHTML = '';

    var seenTypes = {};
    g.edges.forEach(function (e) {
      var t = e.type || 'other';
      if (seenTypes[t]) return;
      seenTypes[t] = true;
      var sample = el('span', { class: ('graph-legend-line ' + legendDashClass(t)).trim() });
      container.appendChild(el('span', { class: 'legend-item' }, [sample, el('span', { class: 'mono', text: t })]));
    });

    var seenAgents = {};
    g.nodes.forEach(function (nd) {
      var id = nd.written_by || '(unknown)';
      if (seenAgents[id]) return;
      seenAgents[id] = true;
      var dot = el('span', { class: 'legend-dot' });
      dot.style.background = colorForAgent(id);
      container.appendChild(el('span', { class: 'legend-item' }, [dot, el('span', { class: 'mono', text: id })]));
    });
  }

  function renderGraphDetail(container, g) {
    container.innerHTML = '';
    var nd = g.selectedId ? nodeById(g, g.selectedId) : null;
    if (!nd) {
      container.appendChild(el('div', { class: 'muted graph-detail-empty', text: 'Click a node to see details.' }));
      return;
    }

    var dl = el('dl', { class: 'meta-grid' }, []);
    [
      ['chunk_id', nd.chunk_id],
      ['layer', nd.layer],
      ['src', nd.src],
      ['written_by', nd.written_by],
      ['base_trust', nd.base_trust === null || nd.base_trust === undefined ? null : Number(nd.base_trust).toFixed(2)]
    ].forEach(function (pair) {
      dl.appendChild(el('dt', { text: pair[0] }));
      dl.appendChild(el('dd', { text: pair[1] === null || pair[1] === undefined || pair[1] === '' ? '—' : String(pair[1]) }));
    });
    container.appendChild(dl);

    var summaryPre = el('pre', { class: 'detail-content' });
    summaryPre.textContent = nd.summary || '';
    container.appendChild(summaryPre);

    var outEdges = g.edges.filter(function (e) { return e.src === nd.chunk_id; });
    var inEdges = g.edges.filter(function (e) { return e.dst === nd.chunk_id; });

    container.appendChild(el('div', { class: 'graph-edge-list-label', text: 'Outgoing (' + outEdges.length + ')' }));
    container.appendChild(el('ul', { class: 'graph-edge-list' }, outEdges.map(function (e) {
      return el('li', {}, [
        el('span', { class: 'tag', text: e.type || 'edge' }),
        el('span', { class: 'mono', text: '→ ' + e.dst })
      ]);
    })));

    container.appendChild(el('div', { class: 'graph-edge-list-label', text: 'Incoming (' + inEdges.length + ')' }));
    container.appendChild(el('ul', { class: 'graph-edge-list' }, inEdges.map(function (e) {
      return el('li', {}, [
        el('span', { class: 'tag', text: e.type || 'edge' }),
        el('span', { class: 'mono', text: e.src + ' →' })
      ]);
    })));
  }

  function applyGraphViewBox() {
    var vb = state.graph.viewBox;
    if (!vb) return;
    $('#graph-svg').setAttribute('viewBox', vb.x + ' ' + vb.y + ' ' + vb.w + ' ' + vb.h);
  }

  function renderGraph() {
    var g = state.graph;
    var svg = $('#graph-svg');
    var empty = $('#graph-empty');
    var legend = $('#graph-legend');
    var detail = $('#graph-detail');

    if (!g.nodes.length) {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      legend.innerHTML = '';
      empty.hidden = false;
      g.selectedId = null;
      renderGraphDetail(detail, g);
      return;
    }
    empty.hidden = true;

    var key = computeGraphKey(g.nodes, g.edges);
    if (key !== g.lastKey) {
      g.lastKey = key;
      layoutGraph(g);
    }
    if (g.selectedId && !nodeById(g, g.selectedId)) g.selectedId = null;

    drawGraphSvg(svg, g);
    renderGraphLegend(legend, g);
    renderGraphDetail(detail, g);
  }

  // Pan (drag) and zoom (wheel) purely via viewBox math — no libraries.
  function wireGraphPanZoom() {
    var svg = $('#graph-svg');
    var dragging = false, moved = false, startX, startY, startViewBox;

    svg.addEventListener('mousedown', function (e) {
      if (!state.graph.viewBox) return;
      dragging = true; moved = false;
      startX = e.clientX; startY = e.clientY;
      startViewBox = { x: state.graph.viewBox.x, y: state.graph.viewBox.y };
    });
    window.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var dx = e.clientX - startX, dy = e.clientY - startY;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) moved = true;
      var rect = svg.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      var scale = state.graph.viewBox.w / rect.width;
      state.graph.viewBox.x = startViewBox.x - dx * scale;
      state.graph.viewBox.y = startViewBox.y - dy * scale;
      applyGraphViewBox();
    });
    window.addEventListener('mouseup', function () { dragging = false; });

    svg.addEventListener('wheel', function (e) {
      if (!state.graph.viewBox) return;
      e.preventDefault();
      var rect = svg.getBoundingClientRect();
      var vb = state.graph.viewBox;
      var mx = vb.x + (e.clientX - rect.left) / rect.width * vb.w;
      var my = vb.y + (e.clientY - rect.top) / rect.height * vb.h;
      var factor = e.deltaY > 0 ? 1.1 : 0.9;
      var newW = Math.max(50, Math.min(vb.w * factor, 20000));
      var newH = Math.max(50, Math.min(vb.h * factor, 20000));
      vb.x = mx - (mx - vb.x) * (newW / vb.w);
      vb.y = my - (my - vb.y) * (newH / vb.h);
      vb.w = newW; vb.h = newH;
      applyGraphViewBox();
    }, { passive: false });

    svg.addEventListener('click', function (e) {
      if (moved) return;
      if (e.target === svg || e.target.id === 'graph-bg') {
        state.graph.selectedId = null;
        renderGraph();
      }
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

    $('#graph-reset-view').addEventListener('click', function () {
      var g = state.graph;
      if (!g.initialViewBox) return;
      g.viewBox = { x: g.initialViewBox.x, y: g.initialViewBox.y, w: g.initialViewBox.w, h: g.initialViewBox.h };
      applyGraphViewBox();
    });
    wireGraphPanZoom();

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
    renderGraph();
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
