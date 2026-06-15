function app() {
  let statKeyCounter = 0;

  return {
    tab: 'history',
    loading: false,
    errorMessage: '',

    config: {
      poesessid_set: false,
      poesessid_preview: '',
      league: '',
      cache_ttl_hours: 24,
      max_fetch_items: 200,
      anthropic_key_set: false,
      anthropic_model: 'claude-opus-4-6',
    },

    promptText: '',
    interpreting: false,
    interpretation: '',
    unresolvedStats: [],

    categories: [],
    leagues: [],

    dealsForm: {
      category: '',
      stats: [],
      attributes: [],
      min_price: null,
      max_price: null,
      max_items: null,
      limit: 20,
    },

    analyzeForm: {
      category: '',
      attributes: [],
      min_divine: 1,
      max_items: null,
      min_occurrence: 0.1,
      use_history: true,
    },

    settingsForm: {
      poesessid: '',
      league: '',
      max_fetch_items: 200,
      auto_sync_minutes: 20,
      anthropic_api_key: '',
      anthropic_model: '',
      saved: false,
      showSessid: false,
    },

    dealsResult: null,
    analyzeResult: null,

    // history state
    summary: null,
    sales: [],
    historyLoaded: false,

    tracker: null,
    trackerLoaded: false,
    trackerLoading: false,
    trackerLimit: 200,
    trackerFilter: { q: '', status: 'all', sockets: 'all', defc: 'all', sort: 'price_desc' },
    historyLoading: false,
    syncMessage: '',
    historyUnit: 'divine',
    historyFilter: '',
    sortKey: 'time',
    sortDir: 'desc',
    page: 0,
    pageSize: 50,
    _charts: {},
    syncStatus: null,
    _lastSeenSuccess: 0,

    // ── lifecycle ──

    async init() {
      await this.loadConfig();
      this.settingsForm.league = this.config.league;
      this.settingsForm.max_fetch_items = this.config.max_fetch_items;
      this.settingsForm.auto_sync_minutes = this.config.auto_sync_minutes;
      this.settingsForm.anthropic_model = this.config.anthropic_model;
      this.addStat();
      if (this.config.poesessid_set) {
        this.loadCategories();
        this.loadLeagues();
        this.loadHistory();
      }
      this.loadStatus();
      // Poll sync status; when the background auto-sync lands new data, refresh
      // the open dashboard so the user never has to click.
      setInterval(() => this.pollStatus(), 60000);
    },

    async loadStatus() {
      try {
        this.syncStatus = await fetch('/api/history/status').then(r => r.json());
        if (this.syncStatus.last_success) this._lastSeenSuccess = this.syncStatus.last_success;
      } catch (e) {}
    },

    async pollStatus() {
      const prev = this._lastSeenSuccess;
      await this.loadStatus();
      if (this.syncStatus && this.syncStatus.last_success && this.syncStatus.last_success !== prev) {
        if (this.tab === 'history' && this.historyLoaded) await this.loadHistory();
      }
    },

    syncDotClass() {
      if (!this.syncStatus) return '';
      if (this.syncStatus.error === 'auth') return 'dot-bad';
      if (this.syncStatus.error) return 'dot-warn';
      if (this.syncStatus.last_success) return 'dot-good';
      return 'dot-warn';
    },

    syncStatusText() {
      const s = this.syncStatus;
      if (!s) return '';
      if (s.error === 'auth') return 'Auto-sync paused — POESESSID expired (update it in Settings).';
      const every = s.auto_sync_minutes > 0 ? `Auto-syncs every ${s.auto_sync_minutes} min` : 'Auto-sync off';
      if (!s.last_success) return `${every} · not synced yet`;
      const ago = this.timeAgo(s.last_success);
      return `${every} · last synced ${ago}`;
    },

    timeAgo(epochSeconds) {
      const secs = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
      if (secs < 60) return 'just now';
      const mins = Math.floor(secs / 60);
      if (mins < 60) return `${mins} min ago`;
      const hrs = Math.floor(mins / 60);
      if (hrs < 24) return `${hrs}h ago`;
      return `${Math.floor(hrs / 24)}d ago`;
    },

    // ── API helpers ──

    async api(path, opts = {}) {
      try {
        const resp = await fetch(path, {
          headers: { 'Content-Type': 'application/json' },
          ...opts,
        });
        if (!resp.ok) {
          let detail = resp.statusText;
          try {
            const body = await resp.json();
            detail = body.detail || detail;
          } catch (e) {}
          throw new Error(`${resp.status}: ${detail}`);
        }
        return await resp.json();
      } catch (e) {
        this.errorMessage = e.message;
        throw e;
      }
    },

    async loadConfig() {
      this.config = await this.api('/api/config');
    },

    async loadCategories() {
      if (this.categories.length) return;
      try {
        this.categories = await this.api('/api/categories');
      } catch (e) {
        this.categories = [];
      }
    },

    async loadLeagues() {
      if (this.leagues.length) return;
      try {
        this.leagues = await this.api('/api/leagues');
      } catch (e) {
        this.leagues = [];
      }
    },

    get categoryGroups() {
      const groups = {};
      for (const c of this.categories) {
        const g = c.group || 'Other';
        if (!groups[g]) groups[g] = [];
        groups[g].push(c);
      }
      return Object.keys(groups).sort().map(name => ({
        name,
        items: groups[name],
      }));
    },

    // ── stat autocomplete ──

    makeStat() {
      statKeyCounter += 1;
      return {
        _key: statKeyCounter,
        query: '',
        suggestions: [],
        showSuggestions: false,
        stat_id: '',
        text: '',
        weight: 1.0,
        min_value: null,
      };
    },

    addStat() {
      this.dealsForm.stats.push(this.makeStat());
    },

    removeStat(idx) {
      this.dealsForm.stats.splice(idx, 1);
    },

    clearStat(idx) {
      const s = this.dealsForm.stats[idx];
      s.stat_id = '';
      s.text = '';
      s.query = '';
      s.suggestions = [];
    },

    async searchStatsFor(idx) {
      const stat = this.dealsForm.stats[idx];
      const q = stat.query.trim();
      if (q.length < 2) {
        stat.suggestions = [];
        return;
      }
      try {
        const results = await this.api(`/api/stats?q=${encodeURIComponent(q)}&limit=15`);
        stat.suggestions = results;
        stat.showSuggestions = true;
      } catch (e) {
        stat.suggestions = [];
      }
    },

    selectStat(idx, suggestion) {
      const s = this.dealsForm.stats[idx];
      s.stat_id = suggestion.id;
      s.text = suggestion.text;
      s.query = suggestion.text;
      s.suggestions = [];
      s.showSuggestions = false;
    },

    // ── base-attribute filter ──

    attrOptions: [
      { key: 'str', label: 'Str', sub: 'Armour' },
      { key: 'dex', label: 'Dex', sub: 'Evasion' },
      { key: 'int', label: 'Int', sub: 'Energy Shield' },
    ],

    toggleAttr(form, key) {
      const arr = form.attributes;
      const i = arr.indexOf(key);
      if (i === -1) arr.push(key); else arr.splice(i, 1);
    },

    // ── form validation ──

    get canRunDeals() {
      if (!this.dealsForm.category) return false;
      const resolved = this.dealsForm.stats.filter(s => s.stat_id);
      return resolved.length > 0;
    },

    // ── run searches ──

    async runDeals() {
      this.errorMessage = '';
      this.loading = true;
      this.dealsResult = null;

      const stats = this.dealsForm.stats
        .filter(s => s.stat_id)
        .map(s => ({
          stat_id: s.stat_id,
          text: s.text,
          weight: s.weight || 1.0,
          min_value: (s.min_value === null || s.min_value === '') ? null : s.min_value,
        }));

      const payload = {
        category: this.dealsForm.category,
        stats,
        attributes: this.dealsForm.attributes.length ? this.dealsForm.attributes : null,
        min_price: this.dealsForm.min_price,
        max_price: this.dealsForm.max_price,
        max_items: this.dealsForm.max_items,
        limit: this.dealsForm.limit || 20,
      };

      try {
        const result = await this.api('/api/deals', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        result.deals.forEach(d => { d._showAll = false; });
        this.dealsResult = result;
      } finally {
        this.loading = false;
      }
    },

    async runAnalyze() {
      this.errorMessage = '';
      this.loading = true;
      this.analyzeResult = null;

      const payload = {
        category: this.analyzeForm.category,
        attributes: this.analyzeForm.attributes.length ? this.analyzeForm.attributes : null,
        min_divine: (this.analyzeForm.min_divine === null || this.analyzeForm.min_divine === '') ? 1 : this.analyzeForm.min_divine,
        max_items: this.analyzeForm.max_items,
        min_occurrence: this.analyzeForm.min_occurrence || 0.1,
        use_history: this.analyzeForm.use_history,
      };

      try {
        this.analyzeResult = await this.api('/api/analyze', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
      } finally {
        this.loading = false;
      }
    },

    // ── settings ──

    async saveSettings() {
      this.errorMessage = '';
      const payload = {};
      if (this.settingsForm.poesessid) payload.poesessid = this.settingsForm.poesessid;
      if (this.settingsForm.league) payload.league = this.settingsForm.league;
      if (this.settingsForm.max_fetch_items) payload.max_fetch_items = this.settingsForm.max_fetch_items;
      if (this.settingsForm.auto_sync_minutes !== null && this.settingsForm.auto_sync_minutes !== '') payload.auto_sync_minutes = this.settingsForm.auto_sync_minutes;
      if (this.settingsForm.anthropic_api_key) payload.anthropic_api_key = this.settingsForm.anthropic_api_key;
      if (this.settingsForm.anthropic_model) payload.anthropic_model = this.settingsForm.anthropic_model;

      try {
        await this.api('/api/config', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        this.settingsForm.poesessid = '';
        this.settingsForm.anthropic_api_key = '';
        this.settingsForm.saved = true;
        setTimeout(() => { this.settingsForm.saved = false; }, 2000);
        await this.loadConfig();
        this.loadCategories();
        this.loadLeagues();
      } catch (e) {}
    },

    async interpretPrompt() {
      if (!this.promptText || !this.config.anthropic_key_set) return;
      this.interpreting = true;
      this.interpretation = '';
      this.unresolvedStats = [];
      this.errorMessage = '';

      try {
        const result = await this.api('/api/interpret', {
          method: 'POST',
          body: JSON.stringify({ prompt: this.promptText }),
        });

        this.dealsForm.category = result.category;
        this.dealsForm.stats = result.stats.map(s => {
          const stat = this.makeStat();
          stat.stat_id = s.stat_id;
          stat.text = s.text;
          stat.query = s.text;
          stat.weight = s.weight;
          stat.min_value = s.min_value;
          return stat;
        });
        if (this.dealsForm.stats.length === 0) this.addStat();

        this.dealsForm.min_price = result.min_price;
        this.dealsForm.max_price = result.max_price;
        this.dealsForm.limit = result.limit || 20;

        this.interpretation = result.explanation;
        this.unresolvedStats = result.unresolved_stats || [];
      } finally {
        this.interpreting = false;
      }
    },

    async clearCache() {
      try {
        await this.api('/api/cache/clear', { method: 'POST' });
        this.categories = [];
        this.leagues = [];
        this.loadCategories();
        this.loadLeagues();
        this.settingsForm.saved = true;
        setTimeout(() => { this.settingsForm.saved = false; }, 2000);
      } catch (e) {}
    },

    // ── sale history ──

    async showHistory() {
      this.tab = 'history';
      if (!this.historyLoaded) {
        await this.loadHistory();
      } else {
        this.$nextTick(() => this.renderCharts());
      }
    },

    async loadHistory() {
      this.errorMessage = '';
      try {
        const [list, summary] = await Promise.all([
          this.api('/api/history'),
          this.api('/api/history/summary'),
        ]);
        this.sales = (list.sales || []).map(s => ({ ...s, _open: false }));
        this.summary = summary;
        this.historyLoaded = true;
        this.page = 0;
        this.$nextTick(() => this.renderCharts());
      } catch (e) {
        // error surfaced by api()
      }
    },

    async showTracker() {
      this.tab = 'tracker';
      if (!this.trackerLoaded) await this.loadTracker();
    },

    async loadTracker() {
      this.trackerLoading = true;
      try {
        this.tracker = await this.api('/api/tracker');
        this.trackerLoaded = true;
      } catch (e) {
        // error surfaced by api()
      } finally {
        this.trackerLoading = false;
      }
    },

    refreshTracker() {
      this.trackerLoaded = false;
      this.loadTracker();
    },

    agoMin(ts) {
      if (!ts) return 'never';
      const m = Math.round(Date.now() / 1000 - ts) / 60;
      if (m < 1) return 'just now';
      if (m < 90) return Math.round(m) + 'm ago';
      return Math.round(m / 60) + 'h ago';
    },

    filteredItems() {
      const f = this.trackerFilter;
      const q = f.q.toLowerCase();
      let items = (this.tracker?.items || []).filter(it => {
        if (f.status !== 'all' && it.status !== f.status) return false;
        if (f.sockets === '2' && it.sockets < 2) return false;
        if (f.sockets === '1' && it.sockets >= 2) return false;
        if (f.defc !== 'all' && it.defc !== f.defc) return false;
        if (q && !it.base.toLowerCase().includes(q)) return false;
        return true;
      });
      const cmp = {
        price_desc: (a, b) => b.price_d - a.price_d,
        price_asc: (a, b) => a.price_d - b.price_d,
        age: (a, b) => b.age_days - a.age_days,
        cuts: (a, b) => b.reprice_down - a.reprice_down,
      }[f.sort];
      return cmp ? [...items].sort(cmp) : items;
    },

    async syncHistory() {
      this.errorMessage = '';
      this.syncMessage = '';
      this.historyLoading = true;
      try {
        const res = await this.api('/api/history/sync', { method: 'POST' });
        await this.loadHistory();
        this.loadStatus();
        if (res.new > 0) {
          this.syncMessage = `Added ${res.new} new sale${res.new === 1 ? '' : 's'} · ${res.total} stored for ${res.league}.`;
        } else {
          this.syncMessage = `Up to date — no new sales. ${res.total} stored for ${res.league}.`;
        }
        setTimeout(() => { this.syncMessage = ''; }, 6000);
      } catch (e) {
        // error surfaced by api()
      } finally {
        this.historyLoading = false;
      }
    },

    async clearHistory() {
      if (!confirm('Delete all locally stored sales for this league? This cannot be undone.')) return;
      try {
        await this.api('/api/history/clear', { method: 'POST' });
        this.sales = [];
        await this.loadHistory();
      } catch (e) {}
    },

    get divineRate() {
      // exalted-per-divine, from the league's live ratio
      return (this.summary && (this.summary.divine_price ||
        (this.summary.rates && this.summary.rates.divine))) || 124;
    },

    toUnit(exalted) {
      return this.historyUnit === 'divine' ? exalted / this.divineRate : exalted;
    },

    unitValue(exalted) {
      const v = this.toUnit(exalted);
      const suffix = this.historyUnit === 'divine' ? ' div' : ' ex';
      if (v >= 1000) return Math.round(v).toLocaleString() + suffix;
      if (v >= 10) return v.toFixed(1) + suffix;
      return v.toFixed(2) + suffix;
    },

    totalDisplay() {
      if (!this.summary) return '';
      return this.unitValue(this.summary.total_exalted);
    },

    totalAltDisplay() {
      if (!this.summary) return '';
      // Show the other unit as the sub-line.
      const ex = this.summary.total_exalted;
      if (this.historyUnit === 'divine') {
        return '≈ ' + Math.round(ex).toLocaleString() + ' exalted';
      }
      return '≈ ' + (ex / this.divineRate).toFixed(1) + ' divine';
    },

    dateRangeLabel() {
      if (!this.summary || !this.summary.first_sale) return '';
      const a = this.formatTime(this.summary.first_sale, true);
      const b = this.formatTime(this.summary.last_sale, true);
      return a === b ? a : `${a} – ${b}`;
    },

    formatTime(iso, dateOnly = false) {
      if (!iso) return '—';
      const d = new Date(iso);
      if (isNaN(d)) return iso;
      if (dateOnly) return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
      return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    },

    // table sorting / filtering / paging

    sortBy(key) {
      if (this.sortKey === key) {
        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        this.sortKey = key;
        this.sortDir = key === 'time' ? 'desc' : 'desc';
      }
      this.page = 0;
    },

    sortIndicator(key) {
      if (this.sortKey !== key) return '';
      return this.sortDir === 'asc' ? '▲' : '▼';
    },

    get filteredSales() {
      let rows = this.sales;
      const f = this.historyFilter.trim().toLowerCase();
      if (f) {
        rows = rows.filter(s =>
          (s.name || '').toLowerCase().includes(f) ||
          (s.base_type || '').toLowerCase().includes(f));
      }
      const key = this.sortKey;
      const dir = this.sortDir === 'asc' ? 1 : -1;
      const rarityOrder = { unique: 4, rare: 3, magic: 2, normal: 1, currency: 0, gem: 0, '': 0 };
      rows = [...rows].sort((a, b) => {
        let av, bv;
        if (key === 'price' || key === 'value') {
          av = a.exalted_value; bv = b.exalted_value;
        } else if (key === 'rarity') {
          av = rarityOrder[(a.rarity || '').toLowerCase()] ?? 0;
          bv = rarityOrder[(b.rarity || '').toLowerCase()] ?? 0;
        } else { // time
          av = a.time || ''; bv = b.time || '';
        }
        if (av < bv) return -1 * dir;
        if (av > bv) return 1 * dir;
        return 0;
      });
      return rows;
    },

    get pageCount() {
      return Math.max(1, Math.ceil(this.filteredSales.length / this.pageSize));
    },

    get pagedSales() {
      const start = this.page * this.pageSize;
      return this.filteredSales.slice(start, start + this.pageSize);
    },

    // charts

    renderCharts() {
      if (typeof Chart === 'undefined' || !this.summary || this.summary.count === 0) return;

      Chart.defaults.color = '#888b94';
      Chart.defaults.borderColor = '#2a2c33';
      Chart.defaults.font.family = "-apple-system, 'Segoe UI', Roboto, sans-serif";

      const accent = '#d4a95e';
      const unitLabel = this.historyUnit === 'divine' ? 'Divine' : 'Chaos';
      const self = this;

      // destroy any existing charts before re-rendering
      for (const k in this._charts) {
        if (this._charts[k]) { this._charts[k].destroy(); this._charts[k] = null; }
      }

      // cumulative line (time on x)
      const cumCanvas = document.getElementById('chart-cumulative');
      if (cumCanvas) {
        const pts = this.summary.cumulative.map(p => ({ x: new Date(p.t).getTime(), y: this.toUnit(p.exalted) }));
        this._charts.cumulative = new Chart(cumCanvas, {
          type: 'line',
          data: {
            datasets: [{
              label: `Cumulative ${unitLabel}`,
              data: pts,
              borderColor: accent,
              backgroundColor: 'rgba(212,169,94,0.12)',
              fill: true,
              tension: 0.25,
              pointRadius: 0,
              borderWidth: 2,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            scales: {
              x: {
                type: 'linear',
                ticks: { callback: v => new Date(v).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
                grid: { display: false },
              },
              y: { ticks: { callback: v => self.compactNum(v) }, grid: { color: '#1d1f24' } },
            },
            plugins: {
              legend: { display: false },
              tooltip: {
                callbacks: {
                  title: items => new Date(items[0].parsed.x).toLocaleString(),
                  label: ctx => `${unitLabel}: ${self.compactNum(ctx.parsed.y)}`,
                },
              },
            },
          },
        });
      }

      // daily bar
      const dailyCanvas = document.getElementById('chart-daily');
      if (dailyCanvas) {
        this._charts.daily = new Chart(dailyCanvas, {
          type: 'bar',
          data: {
            labels: this.summary.daily.map(d => d.date.slice(5)),
            datasets: [{
              label: unitLabel,
              data: this.summary.daily.map(d => this.toUnit(d.exalted)),
              backgroundColor: accent,
              borderRadius: 3,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            scales: {
              x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } },
              y: { ticks: { callback: v => self.compactNum(v) }, grid: { color: '#1d1f24' } },
            },
            plugins: {
              legend: { display: false },
              tooltip: { callbacks: { label: ctx => `${unitLabel}: ${self.compactNum(ctx.parsed.y)}` } },
            },
          },
        });
      }

      // currency doughnut
      const curCanvas = document.getElementById('chart-currency');
      if (curCanvas) {
        const palette = ['#d4a95e', '#6ea3d4', '#7bb66a', '#c56b6b', '#b98ad0', '#5fbcb0', '#d99b57', '#8893a8'];
        this._charts.currency = new Chart(curCanvas, {
          type: 'doughnut',
          data: {
            labels: this.summary.by_currency.map(c => c.currency),
            datasets: [{
              data: this.summary.by_currency.map(c => Math.round(this.toUnit(c.exalted) * 100) / 100),
              backgroundColor: palette,
              borderColor: '#17181c',
              borderWidth: 2,
            }],
          },
          options: {
            responsive: true, maintainAspectRatio: false,
            cutout: '58%',
            plugins: {
              legend: { position: 'right', labels: { boxWidth: 12, padding: 8 } },
              tooltip: {
                callbacks: {
                  label: ctx => {
                    const c = self.summary.by_currency[ctx.dataIndex];
                    return ` ${c.currency}: ${self.unitValue(c.exalted)} (${c.count}×)`;
                  },
                },
              },
            },
          },
        });
      }
    },

    compactNum(n) {
      if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
      if (n >= 10) return Math.round(n).toString();
      return n.toFixed(1);
    },

    // ── utils ──

    toggleAllStats(deal) {
      deal._showAll = !deal._showAll;
    },

    cleanStatText(text) {
      return text
        .replace(/^\+?#%?\s+to\s+/i, '')
        .replace(/^\+?#%?\s+/i, '')
        .replace(/^#%?\s+/i, '');
    },

    formatPrice(n) {
      if (n < 1) return n.toFixed(2);
      if (n === Math.floor(n)) return n.toFixed(0);
      return n.toFixed(1);
    },

    formatExaltedEq(deal) {
      // Show the price's exalted-equivalent, switching to divine once it's >= 1 div.
      if (deal.divine_price_eq >= 1) return '~' + deal.divine_price_eq.toFixed(1) + ' div';
      return '~' + Math.round(deal.exalted_price) + ' ex';
    },

    coeffBarStyle(c, all) {
      const maxAbs = Math.max(...all.map(x => Math.abs(x.coefficient))) || 1;
      const pct = (Math.abs(c.coefficient) / maxAbs) * 100;
      const color = c.coefficient >= 0 ? 'var(--good)' : 'var(--bad)';
      return `width: ${pct}%; background: ${color};`;
    },

    async copyWhisper(text) {
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        this.errorMessage = '';
        // Brief feedback by repurposing saved flag
      } catch (e) {
        // Fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
    },
  };
}
