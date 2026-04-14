function app() {
  let statKeyCounter = 0;

  return {
    tab: 'deals',
    loading: false,
    errorMessage: '',

    config: {
      poesessid_set: false,
      poesessid_preview: '',
      league: '',
      cache_ttl_hours: 24,
      max_fetch_items: 200,
    },

    categories: [],
    leagues: [],

    dealsForm: {
      category: '',
      stats: [],
      min_price: null,
      max_price: null,
      max_items: null,
      limit: 20,
    },

    analyzeForm: {
      category: '',
      min_price: null,
      max_price: null,
      max_items: null,
      min_occurrence: 0.1,
    },

    settingsForm: {
      poesessid: '',
      league: '',
      max_fetch_items: 200,
      saved: false,
      showSessid: false,
    },

    dealsResult: null,
    analyzeResult: null,

    // ── lifecycle ──

    async init() {
      await this.loadConfig();
      this.settingsForm.league = this.config.league;
      this.settingsForm.max_fetch_items = this.config.max_fetch_items;
      this.addStat();
      if (this.config.poesessid_set) {
        this.loadCategories();
        this.loadLeagues();
      }
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
        min_price: this.analyzeForm.min_price,
        max_price: this.analyzeForm.max_price,
        max_items: this.analyzeForm.max_items,
        min_occurrence: this.analyzeForm.min_occurrence || 0.1,
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

      try {
        await this.api('/api/config', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        this.settingsForm.poesessid = '';
        this.settingsForm.saved = true;
        setTimeout(() => { this.settingsForm.saved = false; }, 2000);
        await this.loadConfig();
        this.loadCategories();
        this.loadLeagues();
      } catch (e) {}
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
