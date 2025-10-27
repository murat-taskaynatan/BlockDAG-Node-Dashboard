(() => {
  const state = {
    nodes: new Map(), // id -> { card, meta }
    charts: new Map(), // id -> Chart instance
    paused: new Set(),
    lastMetricsTs: 0,
  };

  const cardsContainer = document.getElementById('fleetCards');
  const emptyStateCard = document.getElementById('emptyFleetState');
  const cardTemplate = document.getElementById('nodeCardTemplate');
  const summaryBadge = document.getElementById('globalSummaryBadge');

  const fmt = new Intl.NumberFormat();
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  async function loadNodes() {
    try {
      const res = await fetch('/api/node-manager/nodes', { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      renderSummary(payload.summary || {});
      syncCards(payload.nodes || []);
      toggleEmptyState();
    } catch (err) {
      console.error('[fleet] failed to load nodes', err);
      renderSummary({ error: err.message });
    }
  }

  function renderSummary(summary) {
    const countEl = document.getElementById('statNodeCount');
    const onlineEl = document.getElementById('statOnline');
    const offlineEl = document.getElementById('statOffline');
    const maxLocalEl = document.getElementById('statMaxLocal');
    const maxRemoteEl = document.getElementById('statMaxRemote');

    if (!summary || Object.keys(summary).length === 0) {
      summaryBadge.textContent = 'No data';
      countEl.textContent = onlineEl.textContent = offlineEl.textContent = maxLocalEl.textContent = maxRemoteEl.textContent = '—';
      return;
    }

    const count = summary.count ?? 0;
    const online = summary.running ?? 0;
    const offline = summary.offline ?? Math.max(count - online, 0);

    countEl.textContent = fmt.format(count);
    onlineEl.textContent = fmt.format(online);
    offlineEl.textContent = fmt.format(offline);
    maxLocalEl.textContent = summary.max_local_height !== undefined ? fmt.format(summary.max_local_height) : '—';
    maxRemoteEl.textContent = summary.max_remote_height !== undefined ? fmt.format(summary.max_remote_height) : '—';

    const ts = summary.timestamp ? new Date(summary.timestamp * 1000) : null;
    const suffix = ts ? ` · ${fmtTime.format(ts)}` : '';
    summaryBadge.textContent = `${fmt.format(count)} node${count === 1 ? '' : 's'}${suffix}`;
  }

  function syncCards(nodes) {
    const seen = new Set();
    nodes.forEach((node) => {
      if (!node || !node.id) return;
      seen.add(node.id);
      const existing = state.nodes.get(node.id);
      if (!existing) {
        createCard(node);
      } else {
        updateCardHeader(node);
      }
    });

    // remove cards for nodes that vanished
    Array.from(state.nodes.keys()).forEach((nodeId) => {
      if (!seen.has(nodeId)) {
        const entry = state.nodes.get(nodeId);
        if (entry?.card?.parentElement) {
          entry.card.parentElement.removeChild(entry.card);
        }
        const chart = state.charts.get(nodeId);
        if (chart) {
          try { chart.destroy(); } catch (err) { console.warn('[fleet] chart destroy failed', err); }
        }
        state.nodes.delete(nodeId);
        state.charts.delete(nodeId);
        state.paused.delete(nodeId);
      }
    });
  }

  function toggleEmptyState() {
    const hasCards = state.nodes.size > 0;
    if (emptyStateCard) {
      emptyStateCard.style.display = hasCards ? 'none' : 'block';
    }
  }

  function createCard(node) {
    const fragment = cardTemplate.content.cloneNode(true);
    const details = fragment.querySelector('.fleet-card');
    details.dataset.nodeId = node.id;

    const nameEl = details.querySelector('.node-name');
    const metaEl = details.querySelector('.node-meta');
    nameEl.textContent = node.label || node.id;
    metaEl.textContent = node.container || '—';

    const toggleBtn = details.querySelector('[data-action="toggle"]');
    toggleBtn.addEventListener('click', () => toggleNode(node.id, toggleBtn));
    const restartBtn = details.querySelector('[data-action="restart"]');
    restartBtn.addEventListener('click', () => restartNode(node.id));

    cardsContainer.appendChild(details);
    state.nodes.set(node.id, { card: details, meta: node });
    state.paused.delete(node.id);
    updateCardHeader(node);

    const canvas = details.querySelector('canvas');
    const chart = createChart(canvas.getContext('2d'));
    state.charts.set(node.id, chart);
  }

  function updateCardHeader(node) {
    const entry = state.nodes.get(node.id);
    if (!entry) return;
    entry.meta = node;

    const card = entry.card;
    card.querySelector('.node-name').textContent = node.label || node.id;
    card.querySelector('.node-meta').textContent = node.container || '—';

    const statusEl = card.querySelector('.status-text');
    const indicator = card.querySelector('.indicator');
    const stats = node.status || {};
    const running = !!stats.running;
    statusEl.textContent = running ? 'Online' : 'Offline';
    indicator.classList.toggle('is-ok', running);
    indicator.classList.toggle('is-warn', !running);
    statusEl.parentElement.classList.toggle('is-ok', running);
    statusEl.parentElement.classList.toggle('is-warn', !running);

    setStat(card, '.stat-local', stats.local_height);
    setStat(card, '.stat-remote', stats.remote_height);
    setStat(card, '.stat-delta', stats.height_delta, { sign: true });
    setStat(card, '.stat-peers', stats.peers);
    updateToggleButton(card.querySelector('[data-action="toggle"]'), state.paused.has(node.id));
  }

  function setStat(card, selector, value, opts = {}) {
    const el = card.querySelector(selector);
    if (!el) return;
    if (value === undefined || value === null || Number.isNaN(value)) {
      el.textContent = '—';
      el.classList.remove('is-warn');
      return;
    }
    const num = Number(value);
    const text = opts.sign && num > 0 ? `+${fmt.format(num)}` : fmt.format(num);
    el.textContent = text;
    if (selector.includes('delta')) {
      el.classList.toggle('is-warn', num > 8);
    }
  }

  function createChart(ctx) {
    return new Chart(ctx, {
      type: 'line',
      data: {
        labels: [],
        datasets: [
          {
            label: 'Local Height',
            data: [],
            borderColor: '#25d366',
            backgroundColor: 'rgba(37,211,102,0.18)',
            fill: false,
            tension: 0.2,
            borderWidth: 2,
            pointRadius: 0,
          },
          {
            label: 'Remote Height',
            data: [],
            borderColor: '#ff5370',
            backgroundColor: 'rgba(255,83,112,0.15)',
            fill: false,
            tension: 0.2,
            borderWidth: 2,
            borderDash: [6, 4],
            pointRadius: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: {
            labels: { color: '#aeb5cf', usePointStyle: true },
          },
          tooltip: {
            callbacks: {
              title: (items) => items.map((item) => item.label).join(', '),
              label: (ctx) => `${ctx.dataset.label}: ${fmt.format(ctx.parsed.y ?? 0)}`,
            },
          },
        },
        scales: {
          x: {
            ticks: { color: '#7681a8', maxRotation: 0, autoSkip: true, maxTicksLimit: 6 },
            grid: { color: 'rgba(255,255,255,0.06)' },
          },
          y: {
            ticks: { color: '#7681a8', callback: (val) => fmt.format(val) },
            grid: { color: 'rgba(255,255,255,0.06)' },
          },
        },
      },
    });
  }

  function updateToggleButton(btn, paused) {
    if (!btn) return;
    btn.dataset.paused = paused ? '1' : '0';
    btn.innerHTML = paused ? '<span class="icon">▶</span>' : '<span class="icon">⏸</span>';
    btn.setAttribute('aria-label', paused ? 'Resume sampling' : 'Pause sampling');
  }

  function toggleNode(nodeId, btn) {
    if (state.paused.has(nodeId)) {
      state.paused.delete(nodeId);
    } else {
      state.paused.add(nodeId);
    }
    updateToggleButton(btn, state.paused.has(nodeId));
  }

  async function restartNode(nodeId) {
    const entry = state.nodes.get(nodeId);
    if (!entry) return;
    const meta = entry.meta || {};
    const container = meta.container || meta.id;
    if (!container) return;
    const btn = entry.card.querySelector('[data-action="restart"]');
    if (btn) btn.disabled = true;
    try {
      const res = await fetch('/api/control', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action: 'docker_restart', container, node: nodeId }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('[fleet] restart failed', err);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function discoverNodes() {
    const btn = document.getElementById('btnDiscoverNodes');
    if (btn) btn.disabled = true;
    try {
      const res = await fetch('/api/node-manager/discover', { method: 'POST', headers: { 'content-type': 'application/json' } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadNodes();
      await refreshMetrics();
    } catch (err) {
      console.error('[fleet] discovery failed', err);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function refreshMetrics() {
    if (!state.nodes.size) return;
    const nodeIds = Array.from(state.nodes.keys());
    const query = encodeURIComponent(nodeIds.join(','));
    try {
      const res = await fetch(`/api/node-manager/metrics?nodes=${query}`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload = await res.json();
      applyMetrics(payload.nodes || {});
    } catch (err) {
      console.error('[fleet] metrics refresh failed', err);
    }
  }

  function applyMetrics(metricsByNode) {
    const now = Date.now();
    Object.entries(metricsByNode).forEach(([nodeId, metrics]) => {
      const entry = state.nodes.get(nodeId);
      if (!entry) return;
      const card = entry.card;

      setStat(card, '.stat-local', metrics.local_height);
      setStat(card, '.stat-remote', metrics.remote_height);
      setStat(card, '.stat-delta', metrics.height_delta, { sign: true });
      setStat(card, '.stat-peers', metrics.peers);

      const statusEl = card.querySelector('.node-status');
      if (statusEl) {
        const running = !!metrics.running;
        statusEl.classList.toggle('is-ok', running);
        statusEl.classList.toggle('is-warn', !running);
        const textEl = statusEl.querySelector('.status-text');
        if (textEl) {
          textEl.textContent = running ? 'Online' : 'Offline';
        }
      }

      const tsEl = card.querySelector('.stat-updated');
      if (tsEl) {
        const ts = metrics.last_updated ? new Date(metrics.last_updated) : new Date(now);
        tsEl.textContent = fmtTime.format(ts);
      }

      if (state.paused.has(nodeId)) {
        return;
      }

      const chart = state.charts.get(nodeId);
      if (!chart) return;
      const labels = (metrics.labels || []).map((stamp) => {
        try {
          return fmtTime.format(new Date(stamp));
        } catch (_) {
          return stamp;
        }
      });
      chart.data.labels = labels;
      chart.data.datasets[0].data = metrics.local || [];
      chart.data.datasets[1].data = metrics.remote || [];
      chart.update('none');
    });
  }

  function attachEventHandlers() {
    const refreshBtn = document.getElementById('btnRefreshFleet');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', async () => {
        refreshBtn.disabled = true;
        await loadNodes();
        await refreshMetrics();
        refreshBtn.disabled = false;
      });
    }
    const discoverBtn = document.getElementById('btnDiscoverNodes');
    if (discoverBtn) {
      discoverBtn.addEventListener('click', () => discoverNodes());
    }
  }

  async function init() {
    attachEventHandlers();
    await loadNodes();
    await refreshMetrics();
    setInterval(refreshMetrics, 5000);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      refreshMetrics();
    }
  });

  window.addEventListener('load', init);
})();
