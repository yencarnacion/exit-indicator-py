(() => {
  const els = {
    status: document.getElementById('statusBadge'),
    sym: document.getElementById('symbolInput'),
    thr: document.getElementById('thresholdInput'),
    start: document.getElementById('startBtn'),
    stop: document.getElementById('stopBtn'),
    sideAsk: document.getElementById('sideAsk'),
    sideBid: document.getElementById('sideBid'),
    test: document.getElementById('testSoundBtn'),
    bookBidBody: document.querySelector('#bookTableBid tbody'),
    bookAskBody: document.querySelector('#bookTableAsk tbody'),

    // NEW: 1m volume strip (full width)
    vol1mCanvas: document.getElementById('vol1mCanvas'),

    /* footprint strip */
    footprintBody: document.getElementById('footprintBody'),

    log: document.getElementById('alertLog'),
    compact: document.getElementById('compactToggle'),
    bestBid: document.getElementById('bestBid'),
    bestAsk: document.getElementById('bestAsk'),
    spread: document.getElementById('spread'),
    last: document.getElementById('lastPrice'),
    vol: document.getElementById('dayVolume'),
    // T&S bits
    tape: document.getElementById('tape'),
    tapeBid: document.getElementById('tapeBid'),
    tapeAsk: document.getElementById('tapeAsk'),
    tapeSpread: document.getElementById('tapeSpread'),
    silent: document.getElementById('silentToggle'),
    machineGun: document.getElementById('machineGunToggle'),
    voiceBigOnly: document.getElementById('voiceBigOnlyToggle'),
    dollarHidden: document.getElementById('dollarHidden'),
    microVwapHidden: document.getElementById('microVwapHidden'),
    microBandSelect: document.getElementById('microBandSelect'),
    microVwapVal: document.getElementById('microVwapVal'),
    actionHintPill: document.getElementById('actionHintPill'),
    rvolBadge: document.getElementById('rvolBadge'),
    volBarBadge: document.getElementById('volBarBadge'), // NEW
  };

  // --- Orderflow state (client-side only) ---
  const BUBBLE_LIFETIME_MS = 1500;       // big-trade bubble lifetime
  const FOOTPRINT_WINDOW_MS = 5000;      // recent-flow window for footprint

  // Per-price recent delta accumulators (shares) for BID/ASK ladders (keyed by priceKey)
  const footprint = {
    BID: new Map(), // Map<priceKey, { buyVol, sellVol, lastUpdate }>
    ASK: new Map(), // Map<priceKey, { buyVol, sellVol, lastUpdate }>
  };

  // Big-trade bubbles keyed by side + priceKey
  const tradeBubbles = {
    BID: Object.create(null),
    ASK: Object.create(null),
  };

  // Last DOM snapshot (top 10 per side) for trade → DOM mapping
  let currentBook = { bids: [], asks: [] };

  // --- OBI mini chart handle ---
  let obiChart = null;
  // --- 1m volume chart handle ---
  let vol1mChart = null;
  let ws;
  let audio;
  let audioReady = false;
  let soundURL = '';
  let soundAvailable = false;
  let globalSilent = false;
 
  // WebAudio autoplay policy (Chrome/Opera): resume only after a user gesture.
  let audioUnlocked = false;

  // T&S audio UX toggles (persisted)
  let tsMachineGunClicks = true;
  let tsVoiceBigOnly = true;
  let tns = { dollar: 0, bigDollar: 0 }; // T&S thresholds
  let loadingTimer = null;
  let waitingForData = false;
  let activeSymbol = '';
  function setStatus(connected, symbol) {
    els.status.textContent = connected ? (symbol ? `Live on ${symbol}` : 'Connected') : 'Disconnected';
    els.status.className = 'badge ' + (connected ? 'badge-live' : 'badge-disconnected');
  }
  function priceKey(p) {
    const n = (typeof p === 'string') ? parseFloat(p) : p;
    return Number.isFinite(n) ? n.toFixed(4) : String(p);
  }
  function currentSide() {
    return els.sideBid && els.sideBid.checked ? 'BID' : 'ASK';
  }
  function setBookTitle(_side) { /* no-op (title removed in new UI) */ }
  function formatShares(n) {
    if (!Number.isFinite(n)) return String(n);
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
    if (n >= 100_000) return Math.round(n / 1_000) + 'k';
    if (n >= 10_000) return (n / 1_000).toFixed(1) + 'k';
    return n.toLocaleString();
  }
  // Volume display: put a decimal before the last digit and add ' K'
  // e.g., 111,037 -> "11,103.7 K"
  function formatVolumeK(x) {
    const v = Number(x);
    if (!Number.isFinite(v)) return '—';
    const scaled = v / 10;
    return scaled.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + ' K';
  }

  // Comma grouping (always uses commas, not locale)
  function formatCommaInt(n) {
    const v = Math.max(0, Math.floor(Number(n) || 0));
    return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  // Fixed-width 9 digits, comma grouped: 000,000,000 (pads up to 9 digits)
  function formatVolume9(n) {
    const v = Math.max(0, Math.floor(Number(n) || 0));
    const s = String(v);
    const padded = (s.length <= 9) ? s.padStart(9, '0') : s; // if > 9 digits, show full
    return padded.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function setCurrentVolBarBadge(vol) {
    if (!els.volBarBadge) return;
    const v = Math.max(0, Math.floor(Number(vol) || 0));
    els.volBarBadge.textContent = formatVolume9(v);
    // Title can be a cleaner, non-padded version
    els.volBarBadge.title = `Current (forming) 1m volume bar: ${formatCommaInt(v)}`;
  }
  async function initConfig() {
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      els.thr.value = cfg.currentThresholdShares || cfg.defaultThresholdShares || 20000;
      if (cfg.currentSide === 'BID') {
        if (els.sideBid) els.sideBid.checked = true; if (els.sideAsk) els.sideAsk.checked = false;
      } else {
        if (els.sideAsk) els.sideAsk.checked = true; if (els.sideBid) els.sideBid.checked = false;
      }
      soundURL = cfg.soundURL || '';
      soundAvailable = !!cfg.soundAvailable;
      globalSilent = !!cfg.silent;
      if (els.silent) els.silent.checked = globalSilent;
      tns.dollar = parseInt(cfg.dollarThreshold || 0, 10) || 0;
      tns.bigDollar = parseInt(cfg.bigDollarThreshold || 0, 10) || 0;
      if (soundAvailable && soundURL) {
        audio = new Audio(soundURL);
        audio.preload = 'auto';
        audio.addEventListener('canplaythrough', () => { audioReady = true; }, { once: true });
        // warm cache
        fetch(soundURL, { cache: 'force-cache' }).catch(() => {});
      }
    } catch (e) {
      console.warn('config failed', e);
    }

    // T&S audio UX toggles (persisted like compact)
    try {
      const mgSaved = localStorage.getItem('ei.ts.machineGun');
      tsMachineGunClicks = (mgSaved === null) ? true : (mgSaved === '1');
      if (els.machineGun) els.machineGun.checked = tsMachineGunClicks;
    } catch {}
    try {
      const vbSaved = localStorage.getItem('ei.ts.voiceBigOnly');
      tsVoiceBigOnly = (vbSaved === null) ? true : (vbSaved === '1');
      if (els.voiceBigOnly) els.voiceBigOnly.checked = tsVoiceBigOnly;
    } catch {}

    // Compact preference: default ON if unset
    const saved = localStorage.getItem('ei.compact');
    const savedCompact = (saved === null) ? true : (saved === '1');
    if (els.compact) {
      els.compact.checked = savedCompact;
    }
    document.body.classList.toggle('compact', savedCompact);
  }

  // Unlock WebAudio + <audio> once on first user gesture so market-driven sounds work later.
  function installAudioUnlocker() {
    const unlock = async () => {
      if (audioUnlocked) return;
      audioUnlocked = true;

      // Resume WebAudio mixer (TickSonic)
      try {
        if (TS_AUDIO.engine) await TS_AUDIO.engine.resume();
      } catch {}

      // Optional: unlock the HTMLAudioElement used by playSound()
      // by doing a muted play/pause once.
      try {
        if (audio && typeof audio.play === 'function') {
          const prevMuted = audio.muted;
          audio.muted = true;
          const p = audio.play();
          if (p && typeof p.then === 'function') await p.catch(() => {});
          audio.pause();
          audio.currentTime = 0;
          audio.muted = prevMuted;
        }
      } catch {}
    };

    // capture=true helps ensure we get the gesture even if other handlers stopPropagation
    window.addEventListener('pointerdown', unlock, { once: true, capture: true });
    window.addEventListener('keydown', unlock, { once: true, capture: true });
  }
  function beepFallback() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = 880;
      gain.gain.value = 0.1;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      setTimeout(() => { osc.stop(); ctx.close(); }, 150);
    } catch (e) {
      // as a last resort
      console.log('\u0007');
    }
  }
  async function playSound() {
    if (audio && audioReady) {
      try { await audio.play(); return; } catch (_) {}
    }
    beepFallback();
  }
  // ---- T&S audio (TickSonic-like) ----
  const TS_AUDIO = {
    urls: {
      above_ask: "/sounds/above_ask.wav",
      below_bid: "/sounds/below_bid.wav",
      between   : "/sounds/between_bid_ask.wav",
      buy       : "/sounds/buy.wav",
      sell      : "/sounds/sell.wav",
      u         : "/sounds/letter_u.wav",
      d         : "/sounds/letter_d.wav",
    },
    engine: null, ready: false
  };
  class Mixer {
    constructor(map){
      this.map = map;
      this.ctx=null;
      this.buffers=new Map();
      this.master=null;
      this.comp=null;
      this.active=[];
      this._lastWhen = 0;         // for scheduling
      this.minGapSec = 0.003;     // 3ms spacing to avoid same-sample-time pileups
    }
    async init(){
      const AC = window.AudioContext || window.webkitAudioContext; if(!AC) return false;
      this.ctx = new AC();

      // Master gain + compressor (prevents clipping when it gets insane)
      this.master = this.ctx.createGain();
      this.master.gain.value = 0.9;

      this.comp = this.ctx.createDynamicsCompressor();
      // mild limiter-ish settings
      this.comp.threshold.value = -18;
      this.comp.knee.value = 18;
      this.comp.ratio.value = 6;
      this.comp.attack.value = 0.003;
      this.comp.release.value = 0.08;

      this.master.connect(this.comp);
      this.comp.connect(this.ctx.destination);

      for (const [k,u] of Object.entries(this.map)) {
        try {
          const resp = await fetch(u, {cache:"force-cache"});
          const buf=await resp.arrayBuffer();
          // Safari-safe decode wrapper
          const audioBuf = await new Promise((res, rej) => {
            this.ctx.decodeAudioData(buf, res, rej);
          });
          this.buffers.set(k, audioBuf);
        } catch (e) {
          console.warn("sound load failed", k, u, e);
        }
      }
      // Return true if at least one wav is available; ticks work regardless.
      return this.buffers.size>0;
    }
    async resume(){ if(this.ctx && this.ctx.state==="suspended") try{ await this.ctx.resume(); }catch{} }

    _sched(whenSec){
      const now = this.ctx.currentTime;
      const t = Math.max(now, whenSec || 0, this._lastWhen);
      this._lastWhen = t + this.minGapSec;
      return t;
    }

    play(k, { rate=1, gain=1, when=0 } = {}){
      if(globalSilent) return;
      const b=this.buffers.get(k);
      if(!b || !this.ctx) return;

      const t = this._sched(when);

      const src = this.ctx.createBufferSource();
      src.buffer = b;
      src.playbackRate.value = rate;

      const g = this.ctx.createGain();
      g.gain.value = gain;

      src.connect(g);
      g.connect(this.master);

      src.start(t);

      this.active.push(src);
      src.onended = () => {
        try { src.disconnect(); } catch {}
        try { g.disconnect(); } catch {}
        const i=this.active.indexOf(src);
        if(i>=0) this.active.splice(i,1);
      };
    }

    // NEW: ultra-short tick per trade
    tick({ f0=1000, f1=900, gain=0.05, dur=0.018, when=0 } = {}){
      if(globalSilent) return;
      if(!this.ctx) return;

      const t = this._sched(when);

      const osc = this.ctx.createOscillator();
      const g = this.ctx.createGain();

      // sharp click-like envelope
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t + 0.002);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dur);

      osc.type = "square";
      osc.frequency.setValueAtTime(f0, t);
      osc.frequency.exponentialRampToValueAtTime(Math.max(40, f1), t + dur);

      osc.connect(g);
      g.connect(this.master);

      osc.start(t);
      osc.stop(t + dur + 0.002);

      osc.onended = () => {
        try { osc.disconnect(); } catch {}
        try { g.disconnect(); } catch {}
      };
    }
    stop(){ for(const s of this.active){ try{s.stop(0);}catch{} } this.active.length=0; }
  }
  (async () => {
    const m = new Mixer(TS_AUDIO.urls);
    TS_AUDIO.engine = m;                 // ticks can work even if no wavs load
    TS_AUDIO.ready = await m.init();     // wav availability
  })();
  function tsPlay(key, rate=1, gain=1){
    if(!TS_AUDIO.ready || !TS_AUDIO.engine) return;
    TS_AUDIO.engine.play(key, { rate, gain });
  }
  function showLoadingState() {
    const make = (tbody) => {
      tbody.innerHTML = '';
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 3;
      td.style.textAlign = 'center';
      td.style.padding = '2rem';
      td.style.color = '#888';
      td.textContent = 'Waiting for market data...';
      tr.appendChild(td);
      tbody.appendChild(tr);
    };
    make(els.bookBidBody);
    make(els.bookAskBody);
  }
  function clearLoadingTimer() {
    if (loadingTimer) {
      clearTimeout(loadingTimer);
      loadingTimer = null;
    }
    waitingForData = false;
  }
  function startLoadingTimer() {
    clearLoadingTimer();
    waitingForData = true;
    loadingTimer = setTimeout(() => {
      if (waitingForData) {
        showLoadingState();
      }
    }, 5000);
  }
  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      // Do NOT call AudioContext.resume() here.
      // Chrome/Opera require a user gesture; installAudioUnlocker() handles it.
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'status') {
          setStatus(!!msg.data.connected, msg.data.symbol || '');
          activeSymbol = msg.data.symbol || '';
          if (msg.data.side) {
            if (msg.data.side === 'BID') { if (els.sideBid) els.sideBid.checked = true; } else { if (els.sideAsk) els.sideAsk.checked = true; }
            setBookTitle(msg.data.side);
          }
        } else if (msg.type === 'stats') {
          // 1s heartbeat: refresh Last/Volume even when DOM/quotes are quiet
          const d = msg.data || {};
          if (els.last && d.last != null) {
            els.last.textContent = fmt2(d.last);
          }
          if (els.vol && d.volume != null) {
            els.vol.textContent = formatVolumeK(d.volume);
          }
          // Feed 1m volume chart (uses cumulative day volume deltas + last)
          onMarketPulse({ last: d.last, volume: d.volume, timeISO: d.timeISO });
        } else if (msg.type === 'book') {
          if (msg.data.side) setBookTitle(msg.data.side);
          // New payload: both sides + stats
          if (msg.data.asks && msg.data.bids) {
            renderBooks(msg.data);
          } else {
            // Back-compat (single side)
            renderSingleSide(msg.data.levels || msg.data.asks || []);
          }
        } else if (msg.type === 'alert') {
          appendAlert(msg.data);
          pulseRowForAlert(msg.data);
          if (!globalSilent) playSound(); // reuse existing alert beep, honor global mute
        } else if (msg.type === 'quote') {
          onTSQuote(msg);
        } else if (msg.type === 'trade') {
          onTSTrade(msg);
        } else if (msg.type === 'rvol_alert') {
          onRVOLAlert(msg.data || {});
        } else if (msg.type === 'error') {
          // MODIFIED: Ignore harmless Error 310
          const m = (msg && msg.data && typeof msg.data.message === 'string') ? msg.data.message : '';
          if (!m.includes('Error 310')) appendError(m || 'Error');
        }
      } catch (e) {
        console.warn('bad ws message', e);
      }
    };
    ws.onclose = () => {
      setStatus(false, '');
      activeSymbol = '';
      setTimeout(connectWS, 1000);
    };
  }
  // Back-compat renderer (single table)
  function renderSingleSide(rows) {
    clearLoadingTimer();
    const tbody = els.bookAskBody; // render into ASK table by default
    const thr = Math.max(1, parseInt(els.thr.value || '0', 10) || 1);
    tbody.innerHTML = '';
    rows.forEach(row => {
      const tr = document.createElement('tr');
      tr.dataset.price = priceKey(row.price);
      const rankTd = document.createElement('td');
      rankTd.className = 'col-rank';
      rankTd.textContent = row.rank;
      const priceTd = document.createElement('td');
      priceTd.className = 'col-price';
      const priceNum = (typeof row.price === 'string') ? parseFloat(row.price) : row.price;
      priceTd.textContent = Number.isFinite(priceNum) ? priceNum.toFixed(2) : String(row.price);
      const sizeTd = document.createElement('td');
      sizeTd.className = 'col-size';
      const meter = document.createElement('div');
      meter.className = 'meter';
      const fill = document.createElement('div');
      fill.className = 'fill';
      const label = document.createElement('span');
      label.className = 'label';
      const size = row.sumShares || 0;
      const ratio = size / thr;
      const width = Math.min(1, ratio) * 100;
      // Color bucket by ratio
      if (ratio >= 2) fill.classList.add('danger');
      else if (ratio >= 1.5) fill.classList.add('hot');
      else if (ratio >= 1.0) fill.classList.add('warn');
      fill.style.width = width.toFixed(2) + '%';
      meter.appendChild(fill);
      const ratioTxt = (ratio >= 1) ? ratio.toFixed(2) + '×' : (ratio.toFixed(2) + '×');
      label.textContent = `${formatShares(size)} ${ratioTxt}`;
      meter.appendChild(label);
      sizeTd.appendChild(meter);
      if (row.rank === 0) tr.classList.add('best');
      if (ratio >= 1.0) tr.classList.add('over');
      tr.append(rankTd, priceTd, sizeTd);
      tbody.appendChild(tr);
    });
  }

  // Build a 10‑element normalized snapshot [0..1] from DOM rows for heatmap
  function buildDomSnapshot(rows) {
    const levels = 10;
    const vec = new Float32Array(levels);
    if (!rows || !rows.length) return vec;

    let max = 0;
    for (let i = 0; i < rows.length && i < levels; i++) {
      const r = rows[i];
      if (!r) continue;
      const idx = (typeof r.rank === 'number' && r.rank >= 0 && r.rank < levels) ? r.rank : i;
      const size = Number(r.sumShares) || 0;
      vec[idx] = size;
      if (size > max) max = size;
    }
    if (max > 0) {
      const inv = 1 / max;
      for (let i = 0; i < levels; i++) {
        vec[i] = Math.max(0, Math.min(1, vec[i] * inv));
      }
    }
    return vec;
  }

  function pruneBubbles(nowMs) {
    for (const side of ['BID', 'ASK']) {
      const bucket = tradeBubbles[side];
      if (!bucket) continue;
      for (const key of Object.keys(bucket)) {
        const b = bucket[key];
        if (!b || (nowMs - b.ts) > BUBBLE_LIFETIME_MS) {
          delete bucket[key];
        }
      }
    }
  }

  function renderFootprintStrip(threshold) {
    const tbody = els.footprintBody;
    if (!tbody) return;
    const now = Date.now();

    // Decay / windowing: drop stale price buckets from each side
    for (const side of ['BID', 'ASK']) {
      const bucket = footprint[side];
      if (!bucket) continue;
      for (const [key, cell] of bucket.entries()) {
        if (!cell || (cell.lastUpdate && (now - cell.lastUpdate) > FOOTPRINT_WINDOW_MS)) {
          bucket.delete(key);
        }
      }
    }

    tbody.innerHTML = '';
    const levels = 10;
    const norm = Math.max(1, threshold || 1);

    // Iterate visible DOM ranks and map them to price-keyed footprint data
    for (let i = 0; i < levels; i++) {
      const askRow = currentBook.asks[i] || null;
      const bidRow = currentBook.bids[i] || null;

      const askPriceKey = askRow ? priceKey(askRow.price) : null;
      const bidPriceKey = bidRow ? priceKey(bidRow.price) : null;

      let net = 0;

      // Net aggressive flow at this visual level:
      // buys (at/above ask) – sells (at/below bid), keyed by price
      if (askPriceKey && footprint.ASK.has(askPriceKey)) {
        const c = footprint.ASK.get(askPriceKey);
        if (c) net += (c.buyVol || 0) - (c.sellVol || 0);
      }
      if (bidPriceKey && footprint.BID.has(bidPriceKey)) {
        const c = footprint.BID.get(bidPriceKey);
        if (c) net += (c.buyVol || 0) - (c.sellVol || 0);
      }

      const mag = Math.abs(net);
      const m = Math.min(1, mag / norm);

      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.className = 'footprint-cell-wrap';
      const cellDiv = document.createElement('div');
      cellDiv.className = 'footprint-cell';

      if (net > 0) {
        cellDiv.classList.add('footprint-buy');
      } else if (net < 0) {
        cellDiv.classList.add('footprint-sell');
      } else {
        cellDiv.classList.add('footprint-neutral');
      }

      const baseOpacity = 0.15;
      const op = baseOpacity + (1 - baseOpacity) * m;
      cellDiv.style.opacity = op.toFixed(3);

      td.appendChild(cellDiv);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  // Map trade price → closest DOM row (within ~2 ticks) for a given side
  function findClosestLevel(tradePrice, levels) {
    if (!levels || !levels.length) return null;
    const px = (typeof tradePrice === 'string') ? parseFloat(tradePrice) : tradePrice;
    if (!Number.isFinite(px)) return null;

    let bestIdx = -1;
    let bestDiff = Infinity;
    let prevPrice = null;
    let minStep = Infinity;

    for (let i = 0; i < levels.length; i++) {
      const r = levels[i];
      if (!r) continue;
      const p = (typeof r.price === 'string') ? parseFloat(r.price) : r.price;
      if (!Number.isFinite(p)) continue;
      const d = Math.abs(p - px);
      if (d < bestDiff) {
        bestDiff = d;
        bestIdx = i;
      }
      if (prevPrice != null) {
        const step = Math.abs(p - prevPrice);
        if (step > 0 && step < minStep) {
          minStep = step;
        }
      }
      prevPrice = p;
    }
    if (bestIdx < 0) return null;

    // Simple tick-size heuristic + 2-tick tolerance
    let tick = (minStep < Infinity) ? minStep : (px >= 5 ? 0.05 : 0.01);
    if (tick <= 0) tick = px >= 5 ? 0.05 : 0.01;
    const MAX_TICKS = 2;
    if (bestDiff > MAX_TICKS * tick + 1e-6 && bestDiff > 1e-4) {
      return null; // too far from ladder → ignore
    }
    return { levelIdx: bestIdx, price: levels[bestIdx].price };
  }

  // Ingest a trade into bubbles + footprint accumulators
  function updateOrderflowFromTrade(ev) {
    if (!ev || ev.price == null) return;
    const px = (typeof ev.price === 'string') ? parseFloat(ev.price) : ev.price;
    if (!Number.isFinite(px)) return;

    const side = ev.side || '';
    const isBuyAgg  = side === 'at_ask'  || side === 'above_ask'  || side === 'between_ask';
    const isSellAgg = side === 'at_bid'  || side === 'below_bid'  || side === 'between_bid';
    // 'between_mid' is ambiguous → ignore for footprint/bubbles

    let bookSide = null;
    if (isBuyAgg) bookSide = 'ASK';
    else if (isSellAgg) bookSide = 'BID';
    if (!bookSide) return;

    const levels = bookSide === 'ASK' ? currentBook.asks : currentBook.bids;
    if (!levels || !levels.length) return;

    const match = findClosestLevel(px, levels);
    if (!match) return;

    const now = Date.now();
    const row = levels[match.levelIdx];
    const key = priceKey(match.price);

    // --- bubbles (still keyed by DOM ladder price for that level) ---
    const bucket = tradeBubbles[bookSide] || (tradeBubbles[bookSide] = Object.create(null));
    bucket[key] = {
      kind: isBuyAgg ? 'buy' : 'sell',
      ts: now,
      big: !!ev.big,
    };

    // --- footprint (price-keyed, independent of current rank) ---
    const fpBucket = footprint[bookSide];
    // Key by the matched DOM ladder price so the footprint strip (which is rendered by ladder levels)
    // reflects aggressive prints even when trade px is above/below the displayed ladder row.
    const fpKey = priceKey(match.price);
    const sz = Number(ev.size) || 0;
    if (sz <= 0) return;

    let cell = fpBucket.get(fpKey);
    if (!cell) {
      cell = { buyVol: 0, sellVol: 0, lastUpdate: 0 };
      fpBucket.set(fpKey, cell);
    }

    if (isBuyAgg) {
      cell.buyVol += sz;
    } else {
      cell.sellVol += sz;
    }
    cell.lastUpdate = now;
  }

  // HELPER: Update a row instead of destroying it
  function updateRow(tr, rowData, thr, side) {
    // 1. Update Rank/Price Text
    const priceNum = (rowData && Number.isFinite(parseFloat(rowData.price))) ? parseFloat(rowData.price) : 0;
    tr.dataset.price = rowData ? priceKey(rowData.price) : "";

    if (tr.children.length < 3) return;

    tr.children[0].textContent = rowData ? rowData.rank : "";           // Rank
    tr.children[1].textContent = rowData ? priceNum.toFixed(2) : "";    // Price

    // 2. Update Size/Meter
    const sizeCell = tr.children[2];
    const meter = sizeCell.firstElementChild; // .meter div
    if (!meter) return;
    const fill = meter.firstElementChild; // .fill div
    const label = fill ? fill.nextElementSibling : null; // .label span
    if (!fill || !label) return;

    if (rowData) {
      const size = rowData.sumShares || 0;
      const ratio = size / thr;
      const width = Math.min(1, ratio) * 100;

      // Update classes efficiently
      fill.className = 'fill'; // reset
      if (ratio >= 2) fill.classList.add('danger');
      else if (ratio >= 1.5) fill.classList.add('hot');
      else if (ratio >= 1.0) fill.classList.add('warn');

      fill.style.width = width.toFixed(2) + '%';
      const ratioTxt = ratio.toFixed(2) + '×';
      label.textContent = `${formatShares(size)} ${ratioTxt}`;

      // Highlight if best bid/ask
      if (rowData.rank === 0) tr.classList.add('best');
      else tr.classList.remove('best');

      if (ratio >= 1.0) tr.classList.add('over');
      else tr.classList.remove('over');
    } else {
      fill.style.width = '0%';
      label.textContent = '';
      tr.classList.remove('best');
      tr.classList.remove('over');
    }

    // 3. MANAGE BUBBLES (The Fix)
    // We do NOT clear the meter. We only append NEW bubbles.
    // Existing bubbles fade out via CSS and are removed by animationend.
    if (rowData) {
      const sideKey = side; // 'BID' or 'ASK'
      const key = priceKey(rowData.price);
      const bucket = tradeBubbles[sideKey];

      // Check if we have a bubble for this specific Price Key
      if (bucket && bucket[key]) {
        const b = bucket[key];
        // Prevent re-adding the same bubble timestamp
        // Store last rendered timestamp on the DOM element to dedupe
        const lastTs = tr.dataset.lastBubbleTs ? parseInt(tr.dataset.lastBubbleTs, 10) || 0 : 0;

        if (b.ts > lastTs) {
          const bubble = document.createElement('span');
          bubble.className = 'bubble ' + (b.kind === 'buy' ? 'bubble-buy' : 'bubble-sell');
          if (b.big) bubble.classList.add('bubble-big');

          // Remove bubble from DOM after animation completes to keep DOM light
          bubble.addEventListener('animationend', () => bubble.remove());

          meter.appendChild(bubble);
          tr.dataset.lastBubbleTs = String(b.ts);
        }
      }
    }
  }

  function renderBooks(data) {
    clearLoadingTimer();
    const thr = Math.max(1, parseInt(els.thr.value || '0', 10) || 1);

    // Snapshot current book for all order‑flow visuals
    currentBook.bids = Array.isArray(data.bids) ? data.bids.slice(0, 10) : [];
    currentBook.asks = Array.isArray(data.asks) ? data.asks.slice(0, 10) : [];

    // Age out expired bubbles
    pruneBubbles(Date.now());

    const makeSide = (tbody, rows, side) => {
      // Sync row count (ensure 10 rows exist)
      while (tbody.children.length < 10) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td class="col-rank"></td><td class="col-price"></td><td class="col-size"><div class="meter"><div class="fill"></div><span class="label"></span></div></td>`;
        tbody.appendChild(tr);
      }

      // Update existing rows
      for (let i = 0; i < 10; i++) {
        updateRow(tbody.children[i], rows[i], thr, side);
      }
    };

    makeSide(els.bookBidBody, currentBook.bids, 'BID');
    makeSide(els.bookAskBody, currentBook.asks, 'ASK');

    // Central micro-footprint strip (per rank recent delta)
    renderFootprintStrip(thr);

    if (data.stats) updateStats(data.stats);
  }
  function updateStats(s) {
    const fmtP = (x) => (Number.isFinite(+x) ? (+x).toFixed(2) : '—');
    const fmtOBI = (x) => (Number.isFinite(+x) ? (+x).toFixed(2) : '—');
    const bb = (s.bestBid != null) ? +s.bestBid : null;
    const ba = (s.bestAsk != null) ? +s.bestAsk : null;
    const sp = (bb != null && ba != null) ? (ba - bb) : null;
    if (els.bestBid) els.bestBid.textContent = fmtP(bb);
    if (els.bestAsk) els.bestAsk.textContent = fmtP(ba);
    if (els.spread)  els.spread.textContent  = fmtP(sp);
    if (els.last)    els.last.textContent    = fmtP(s.last);
    if (els.vol)     els.vol.textContent     = formatVolumeK(s.volume);

    // Feed 1m volume chart from book stats too (useful when only DOM snapshots are active)
    onMarketPulse({ last: s.last, volume: s.volume, timeISO: s.timeISO });
    // OBI (−1..+1): quick mean-reversion read
    const obiEl = document.getElementById('obiVal');
    if (obiEl) {
      const val = (s.obi != null) ? +s.obi : NaN;
      obiEl.classList.remove('pos', 'neg', 'flat');
      obiEl.textContent = fmtOBI(val);
      if (!Number.isFinite(val)) {
        obiEl.classList.add('flat');
      } else if (val > 0.05) {
        obiEl.classList.add('pos');
      } else if (val < -0.05) {
        obiEl.classList.add('neg');
      } else {
        obiEl.classList.add('flat');
      }
      // Optional tiny hint with α and L (in a title)
      const a = (s.obiAlpha != null) ? Number(s.obiAlpha).toFixed(2) : 'auto';
      const L = (s.obiLevels != null) ? s.obiLevels : '—';
      obiEl.title = `OBI (α=${a}, L=${L})`;
    }
    // Feed the mini chart (only when we have a number)
    if (obiChart && s.obi != null && Number.isFinite(+s.obi)) {
      obiChart.push(+s.obi);
      obiChart.draw();
    }

    // Micro VWAP + bands (use server-supplied k to stay aligned)
    if (els.microVwapVal) {
      const mv = (s.microVWAP != null) ? +s.microVWAP : NaN;
      const sig = (s.microSigma != null) ? +s.microSigma : NaN;
      const k = (s.microBandK != null) ? +s.microBandK : (parseFloat(els.microBandSelect?.value || "2") || 2);
      if (Number.isFinite(mv) && Number.isFinite(sig) && sig > 0) {
        const lo = mv - k * sig;
        const hi = mv + k * sig;
        els.microVwapVal.textContent = `${mv.toFixed(2)} [${lo.toFixed(2)}, ${hi.toFixed(2)}]`;
      } else if (Number.isFinite(mv)) {
        els.microVwapVal.textContent = mv.toFixed(2);
      } else {
        els.microVwapVal.textContent = '—';
      }
    }

    // Action hint pill: mutually exclusive, color-coded
    if (els.actionHintPill) {
      const pill = els.actionHintPill;
      pill.className = 'signal-pill';
      const hint = s.actionHint;
      if (hint === 'long_ok') {
        pill.textContent = 'long ok';
        pill.classList.add('long-ok');
      } else if (hint === 'fade_short_ok') {
        pill.textContent = 'fade short ok';
        pill.classList.add('fade-short-ok');
      } else if (hint === 'trend_up') {
        pill.textContent = 'trend up';
        pill.classList.add('trend-up');
      } else if (hint === 'trend_down') {
        pill.textContent = 'trend down';
        pill.classList.add('trend-down');
      } else {
        pill.textContent = '—';
      }
    }
  }

  // --- Log utilities (newest at top + stable scroll) ---
  function prependLogItem(node) {
    const c = els.log;
    if (!c) return;
    // If the user is at the very top, keep showing the newest at top.
    // If they've scrolled away, keep their viewport stable even as new items come in.
    const AT_TOP_THRESHOLD = 1; // px tolerance
    const atTop = c.scrollTop <= AT_TOP_THRESHOLD;
    const prevScrollTop = c.scrollTop;
    const prevScrollHeight = c.scrollHeight;
    // Insert the new item at the top
    c.prepend(node);
    const newScrollHeight = c.scrollHeight;
    if (atTop) {
      // Stick to top: newest visible
      c.scrollTop = 0;
    } else {
      // Preserve viewport: compensate for the added height above
      c.scrollTop = prevScrollTop + (newScrollHeight - prevScrollHeight);
    }
  }
  function pulseRowForAlert(a) {
    const key = priceKey(a.price);
    const tbody = (a.side === 'BID') ? els.bookBidBody : els.bookAskBody;
    const row = tbody.querySelector(`tr[data-price="${key}"]`);
    if (row) {
      row.classList.add('pulse', 'over');
      const priceCell = row.querySelector('.col-price');
      if (priceCell) priceCell.classList.add('over');
      setTimeout(() => {
        row.classList.remove('pulse');
        if (priceCell) priceCell.classList.remove('over');
      }, 900);
    }
  }
  function appendAlert(a) {
    const el = document.createElement('div');
    el.className = 'log-item';
    const ts = new Date(a.timeISO || Date.now());
    const sideLabel = (a.side || 'ASK') === 'BID' ? 'bid' : 'ask';
    el.textContent = `[${ts.toLocaleTimeString()}] ${a.symbol} ${sideLabel} ${parseFloat(a.price).toFixed(2)}: ${(+a.sumShares).toLocaleString()} shares`;
    prependLogItem(el);
  }
  function appendError(msg) {
    const el = document.createElement('div');
    el.className = 'log-item error';
    el.textContent = `Error: ${msg}`;
    prependLogItem(el);
  }
  function fmt2(x){ return (Number.isFinite(+x)?(+x).toFixed(2):'—'); }
  function fmtX(x){ return (Number.isFinite(+x)?(+x).toFixed(2)+'×':'—'); }
  function fmtPctl(x){
    const v = Number(x);
    return Number.isFinite(v) ? ('p' + Math.max(0, Math.min(100, Math.round(v)))) : 'p—';
  }

  function appendRVOLAlert(d) {
    const el = document.createElement('div');
    const pace = !!d.pace;
    el.className = 'log-item rvol' + (pace ? ' pace' : '');

    const sym = d.symbol || activeSymbol || '';
    const r = fmtX(d.rvol);
    const pct = fmtPctl(d.percentile);
    const vol = formatShares(Number(d.volume) || 0);
    const base = formatShares(Math.round(Number(d.baseline) || 0));
    const t = d.time || '';
    const tag = pace ? 'pace' : 'close';
    const extra = pace && d.elapsedSec ? ` @${d.elapsedSec}s` : '';

    // Optional projections (if server sends them)
    let proj = '';
    if (pace && d.projectedVolume != null) {
      const pv = formatShares(Number(d.projectedVolume) || 0);
      const pp = (d.projectedPercentile != null) ? fmtPctl(d.projectedPercentile) : '';
      proj = ` proj=${pv}${pp ? ' ' + pp : ''}`;
    }

    el.textContent = `[${t}] ${sym} RVOL ${tag} ${r} ${pct} vol=${vol} med=${base}${extra}${proj}`;
    prependLogItem(el);
  }

  function updateRVOLBadge(d) {
    const b = els.rvolBadge;
    if (!b) return;
    const pace = !!d.pace;
    const r = Number(d.rvol);
    const pct = d.percentile;
    const txt = `RVOL ${fmtX(r)} ${fmtPctl(pct)}`;
    b.textContent = txt;

    const vol = formatShares(Number(d.volume) || 0);
    const base = formatShares(Math.round(Number(d.baseline) || 0));
    const t = d.time || '';
    const mode = pace ? 'PACE' : 'CLOSE';
    b.title = `${mode} • ${txt} • vol=${vol} • med=${base} • samples=${d.samples || 0} • ${t}`;

    b.classList.remove('hot','danger','pace','pulse');
    if (pace) b.classList.add('pace');
    if (Number.isFinite(r)) {
      if (r >= 3.0) b.classList.add('danger');
      else if (r >= 2.0) b.classList.add('hot');
    }
    b.classList.add('pulse');
    setTimeout(() => b.classList.remove('pulse'), 700);
  }

  function onRVOLAlert(d) {
    updateRVOLBadge(d);
    appendRVOLAlert(d);

    // Very short, distinct audio tick (optional). Uses existing T&S engine.
    if (globalSilent) return;
    if (TS_AUDIO && TS_AUDIO.engine && typeof TS_AUDIO.engine.tick === 'function') {
      const pace = !!d.pace;
      TS_AUDIO.engine.tick({
        f0: pace ? 2400 : 1600,
        f1: pace ? 1800 : 1200,
        gain: 0.06,
        dur: 0.020,
      });
    }
  }
  function onTSQuote(q){
    const { bid, ask } = q;
    if (els.tapeBid && bid!=null) els.tapeBid.textContent = fmt2(bid);
    if (els.tapeAsk && ask!=null) els.tapeAsk.textContent = fmt2(ask);
    if (els.tapeSpread && bid!=null && ask!=null) els.tapeSpread.textContent = fmt2(ask - bid);
    // Refresh volume whenever server includes it on quotes
    if (els.vol && q && q.volume != null) {
      els.vol.textContent = formatVolumeK(q.volume);
    }
    onMarketPulse({ last: q.last, volume: q.volume, timeISO: q.timeISO });
  }
  function tsRow(ev){
    const row = document.createElement('div');
    row.className = 'row' + (ev.big ? ' big' : '');
    const colorClass = {
      "above_ask":"col-yellow","at_ask":"col-green","between_mid":"col-white",
      "between_ask":"col-white","between_bid":"col-white","at_bid":"col-red","below_bid":"col-magenta"
    }[ev.side] || "col-white";
    const priceStr = fmt2(ev.price);
    // time as MM:SS (no hour)
    const dt = ev.timeISO ? new Date(ev.timeISO) : new Date();
    const timeStr = dt.toLocaleTimeString([], { minute: '2-digit', second: '2-digit' });
    // shares: reuse Level 2 formatter
    const sharesStr = formatShares(Number(ev.size) || 0);
    row.innerHTML = `
      <div class="left">
        <span class="badge ${colorClass}">${ev.side.replaceAll('_',' ')}</span>
        <span class="price ${colorClass}">${priceStr}</span>
        <span class="amt">$${ev.amountStr || ''} <span class="shares">(${sharesStr})</span></span>
      </div>
      <div class="time">${timeStr}</div>
      <div class="sym">${ev.sym || activeSymbol || ''}</div>`;
    return row;
  }

  // --- T&S: prepend row (newest at top) with stable scroll anchoring ---
  function prependTapeRow(row) {
    const c = els.tape;
    if (!c) return;
    const AT_TOP_THRESHOLD = 1; // px tolerance to consider "at top"
    const atTop = c.scrollTop <= AT_TOP_THRESHOLD;
    const prevScrollTop = c.scrollTop;
    const prevScrollHeight = c.scrollHeight;
    // Insert newest at the top
    c.prepend(row);
    // Trim to 1000 rows (remove from bottom)
    while (c.childElementCount > 1000) c.removeChild(c.lastElementChild);
    const newScrollHeight = c.scrollHeight;
    if (atTop) {
      // Keep newest visible at the top
      c.scrollTop = 0;
    } else {
      // Preserve viewport position while we injected content above
      c.scrollTop = prevScrollTop + (newScrollHeight - prevScrollHeight);
    }
  }

  function onTSTrade(ev){
    // T&S tape
    if (els.tape) {
      prependTapeRow(tsRow(ev));
    }
    // Refresh volume on every trade tick when server includes it
    if (els.vol && ev && ev.volume != null) {
      els.vol.textContent = formatVolumeK(ev.volume);
    }
    // Use actual trade price when available for candle direction
    onMarketPulse({ price: ev.price, last: ev.last, volume: ev.volume, timeISO: ev.timeISO });

    // Feed DOM bubbles + micro-footprint accumulators
    updateOrderflowFromTrade(ev);

    // sound (mute respected)
    if (globalSilent) return;
    if (!TS_AUDIO.engine) return;

    function clamp01(x){ return Math.max(0, Math.min(1, x)); }

    // Log-scale “intensity” from dollars (works across symbols)
    function tradeIntensity(ev){
      const amt = Number(ev.amount) || 0;
      if (!Number.isFinite(amt) || amt <= 0) return 0;

      // Use your selected thresholds if present; else a sane default range
      const lo = Math.max(1, Number(tns.dollar || 1)); // filter threshold
      const hi = Math.max(lo * 20, Number(tns.bigDollar || (lo * 50))); // “big” reference

      // intensity 0..1 between lo..hi on a log curve
      const x = Math.log(amt / lo) / Math.log(hi / lo);
      return clamp01(x);
    }

    function sideToTickFreq(side){
      switch(side){
        case "above_ask":   return [2200, 1700];
        case "at_ask":      return [1900, 1500];
        case "between_ask": return [1500, 1200];
        case "between_mid": return [1200, 1050];
        case "between_bid": return [950,  820];
        case "at_bid":      return [780,  650];
        case "below_bid":   return [620,  520];
        default:            return [1200, 1000];
      }
    }

    // Always do the machine-gun tick (optional toggle)
    const I = tradeIntensity(ev);
    const [f0, f1] = sideToTickFreq(ev.side);

    // louder & slightly sharper when bigger
    if (tsMachineGunClicks) {
      TS_AUDIO.engine.tick({
        f0: f0 * (1 + 0.35 * I),
        f1: f1 * (1 + 0.20 * I),
        gain: 0.02 + 0.08 * I,
        dur: 0.012 + 0.020 * I,
      });

      // Optional: "double tick" on big prints
      if (ev.big || I > 0.85) {
        TS_AUDIO.engine.tick({
          f0: f0 * (1.08 + 0.25 * I),
          f1: f1 * (1.05 + 0.18 * I),
          gain: 0.015 + 0.06 * I,
          dur: 0.010 + 0.016 * I,
          when: (TS_AUDIO.engine.ctx ? (TS_AUDIO.engine.ctx.currentTime + 0.016) : 0),
        });
      }
    }

    // Optional: WAV “word sounds” as accent marks
    // - default: only big prints
    // - if toggle off: allow WAVs for all trades (can get loud)
    if (TS_AUDIO.ready && (!tsVoiceBigOnly || (ev.big || I > 0.7))) {
      const rate = 0.95 + 0.50 * I;
      const gain = 0.15 + 0.25 * I;
      const key = ({
        "above_ask":"above_ask",
        "at_ask":"buy",
        "between_mid":"between",
        "between_ask":"u",
        "between_bid":"d",
        "at_bid":"sell",
        "below_bid":"below_bid"
      })[ev.side];

      if (key) TS_AUDIO.engine.play(key, { rate, gain });
    }
  }
  async function start() {
    const symbol = (els.sym.value || '').trim().toUpperCase(); // Uppercase symbol
    if (!symbol) {
      els.sym.focus();
      return;
    }

    if (vol1mChart && typeof vol1mChart.reset === 'function') {
      vol1mChart.reset();
      setCurrentVolBarBadge(0);
    }

    // Nudge audio context past autoplay restrictions on an explicit click
    try { TS_AUDIO.engine && (await TS_AUDIO.engine.resume()); } catch {}
    const threshold = parseInt(els.thr.value || '0', 10);
    // Dollar combobox stores a JSON blob or a numeric threshold; accept both
    let dollar = 0, bigDollar = 0;
    try {
      const raw = els.dollarHidden ? els.dollarHidden.value : "";
      if (raw && raw.trim().startsWith("{")) {
        const j = JSON.parse(raw);
        dollar = parseInt(j.threshold || 0, 10) || 0;
        bigDollar = parseInt(j.big_threshold || 0, 10) || 0;
      } else if (raw) {
        dollar = parseInt(raw, 10) || 0;
      }
    } catch {}
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ symbol, threshold, side: currentSide(),
                             dollar, bigDollar, silent: !!(els.silent && els.silent.checked) })
    });
    if (!res.ok) {
      const txt = await res.text();
      appendError(`Start failed: ${txt}`);
    } else {
      // Start loading timer - show loading state if no data after 5s
      startLoadingTimer();
      try {
        const out = await res.json();
        activeSymbol = out.symbol || symbol;
      } catch {}
    }

    // After successful start, push current micro-VWAP settings to server
    try {
      const minutes = parseFloat(els.microVwapHidden?.value || "5") || 5;
      const band_k = parseFloat(els.microBandSelect?.value || "2") || 2;
      await fetch('/api/microvwap', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ minutes, band_k })
      });
    } catch (e) {
      console.warn('microVWAP config failed', e);
    }
  }
  async function stop() {
    clearLoadingTimer();

    if (vol1mChart && typeof vol1mChart.reset === 'function') {
      vol1mChart.reset();
      setCurrentVolBarBadge(0);
    }

    const res = await fetch('/api/stop', { method: 'POST' });
    if (!res.ok) {
      const txt = await res.text();
      // MODIFIED: Ignore harmless Error 310 on stop
      if (!txt.includes('Error 310')) {
        appendError(`Stop failed: ${txt}`);
      }
    }
    activeSymbol = '';
  }
  async function updateThreshold() {
    const threshold = Math.max(1, parseInt(els.thr.value || '0', 10) || 1);
    try {
      const res = await fetch('/api/threshold', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ threshold })
      });
      if (!res.ok) {
        const txt = await res.text();
        appendError(`Threshold update failed: ${txt}`);
      }
    } catch (e) {
      appendError(`Threshold update error: ${String(e)}`);
    }
  }
  async function setSide(side) {
    try {
      const res = await fetch('/api/side', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ side })
      });
      if (!res.ok) {
        const txt = await res.text();
        appendError(`Side update failed: ${txt}`);
      }
    } catch (e) {
      appendError(`Side update error: ${String(e)}`);
    }
  }
  // Events
  els.start.addEventListener('click', start);
  els.stop.addEventListener('click', stop);
  els.test.addEventListener('click', () => playSound());
  if (els.silent) {
    els.silent.addEventListener('change', async () => {
      globalSilent = !!els.silent.checked;
      try { await fetch('/api/silent', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({silent: globalSilent})}); } catch {}
    });
  }
  if (els.machineGun) {
    els.machineGun.addEventListener('change', () => {
      tsMachineGunClicks = !!els.machineGun.checked;
      try { localStorage.setItem('ei.ts.machineGun', tsMachineGunClicks ? '1' : '0'); } catch {}
    });
  }
  if (els.voiceBigOnly) {
    els.voiceBigOnly.addEventListener('change', () => {
      tsVoiceBigOnly = !!els.voiceBigOnly.checked;
      try { localStorage.setItem('ei.ts.voiceBigOnly', tsVoiceBigOnly ? '1' : '0'); } catch {}
    });
  }
  els.sym.addEventListener('keydown', (e) => { if (e.key === 'Enter') start(); });
  els.thr.addEventListener('keydown', (e) => { if (e.key === 'Enter') updateThreshold(); });
  els.thr.addEventListener('change', updateThreshold);
  if (els.sideAsk) els.sideAsk.addEventListener('change', () => { if (els.sideAsk.checked) { setBookTitle('ASK'); setSide('ASK'); } });
  if (els.sideBid) els.sideBid.addEventListener('change', () => { if (els.sideBid.checked) { setBookTitle('BID'); setSide('BID'); } });
  if (els.compact) {
    els.compact.addEventListener('change', () => {
      document.body.classList.toggle('compact', els.compact.checked);
      localStorage.setItem('ei.compact', els.compact.checked ? '1' : '0');

      // Re-layout volume canvas when height changes
      if (vol1mChart && typeof vol1mChart.resize === 'function') {
        vol1mChart.resize();
        vol1mChart.draw();
      }

      // Re-layout canvases when height changes
      if (obiChart && typeof obiChart.resize === 'function') {
        obiChart.resize();
        obiChart.draw();
      }
    });
  }
  // Warn if navigating away while subscribed
  window.addEventListener('beforeunload', (e) => {
    if (activeSymbol) {
      e.preventDefault();
      e.returnValue = '';
      return '';
    }
  });
  // Boot
  initConfig().then(() => {
    initVol1mChart();
    initObiMiniChart();
    installAudioUnlocker();
    connectWS();
  });

  // --------- OBI mini chart implementation ----------
  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  // --------- 1m volume chart implementation ----------
  function _parseTimeISO(timeISO) {
    if (!timeISO || typeof timeISO !== 'string') return null;
    const t = Date.parse(timeISO);
    return Number.isFinite(t) ? t : null;
  }

  // IMPORTANT:
  // The day-volume number coming from the server is effectively in "hundreds of shares".
  // Your day Volume display (formatVolumeK) turns that into a K-display you like.
  // But the 1m volume bars should be in REAL SHARES, so scale here.
  const VOL_CUM_TO_SHARES = 100;

  function onMarketPulse({ price, last, volume, timeISO } = {}) {
    if (!vol1mChart) return;

    const ts = _parseTimeISO(timeISO) || Date.now();
    const px = (price != null) ? price : last;

    let cumVolShares = volume;
    if (cumVolShares != null) {
      const n = Number(cumVolShares);
      cumVolShares = Number.isFinite(n) ? (n * VOL_CUM_TO_SHARES) : cumVolShares;
    }

    vol1mChart.ingest(px, cumVolShares, ts);

    // Live current bar badge now reflects REAL SHARES
    setCurrentVolBarBadge(vol1mChart.currentVol());
  }

  class Vol1mChart {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.step = opts.step || 25000;           // 25k increments
      this.viewBars = opts.viewBars || 24;      // visible window
      this.maxBars = opts.maxBars || 240;       // keep a few hours

      this.bars = []; // finalized bars: {t0, open, close, vol}
      this.cur = null; // current minute bar (not yet finalized)

      this.lastCumVol = NaN;   // last known cumulative day volume (monotonic-clamped)
      this.lastPrice = NaN;    // last known price for gap-filling/open defaults

      this.bg = '#050910';
      this.grid = cssVar('--obi-grid', '#223149');
      this.axis = cssVar('--muted', '#98a6b3');
      this.up = cssVar('--good', '#2ecc71');
      this.down = cssVar('--danger', '#ff6b6b');
      this.flat = cssVar('--muted', '#98a6b3');
      this.text = cssVar('--text', '#e6edf3');

      this.dpr = 1;
      this._raf = 0;

      // Hover tooltip state
      this._geom = null;
      this._wrapEl = null;
      this._tooltipEl = null;
      this.resize();
      window.addEventListener('resize', () => { this.resize(); this.draw(); });
    }

    reset() {
      this.bars.length = 0;
      this.cur = null;
      this.lastCumVol = NaN;
      this.lastPrice = NaN;
      this._geom = null;
      this.hideTooltip();
      this.requestDraw();
    }

    resize() {
      const rect = this.canvas.getBoundingClientRect();
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      this.dpr = dpr;
      this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    }

    requestDraw() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => {
        this._raf = 0;
        this.draw();
      });
    }

    _pushFinal(bar) {
      if (!bar) return;
      const open = Number.isFinite(bar.open) ? bar.open : (Number.isFinite(bar.close) ? bar.close : NaN);
      const close = Number.isFinite(bar.close) ? bar.close : open;
      const vol = Number.isFinite(bar.vol) ? Math.max(0, bar.vol) : 0;
      this.bars.push({ t0: bar.t0, open, close, vol });
      if (this.bars.length > this.maxBars) {
        this.bars.splice(0, this.bars.length - this.maxBars);
      }
    }

    _rollTo(minute0) {
      const prev = this.cur;
      if (prev) {
        this._pushFinal(prev);
      }

      // Fill missing minutes with 0-volume bars so the 5-min markers stay stable.
      if (prev && minute0 > prev.t0 + 60000) {
        const prevClose = Number.isFinite(prev.close) ? prev.close : (Number.isFinite(prev.open) ? prev.open : this.lastPrice);
        let t = prev.t0 + 60000;
        while (t < minute0) {
          this._pushFinal({ t0: t, open: prevClose, close: prevClose, vol: 0 });
          t += 60000;
        }
      }

      const baseVol = Number.isFinite(this.lastCumVol) ? this.lastCumVol : NaN;
      const openPx = Number.isFinite(this.lastPrice) ? this.lastPrice : NaN;
      this.cur = {
        t0: minute0,
        baseVol,
        open: openPx,
        close: openPx,
        vol: 0,
      };
    }

    ingest(price, cumVol, tsMs) {
      const ts = Number.isFinite(tsMs) ? tsMs : Date.now();
      const minute0 = Math.floor(ts / 60000) * 60000;

      const px = Number(price);
      if (Number.isFinite(px)) this.lastPrice = px;

      let v = Number(cumVol);
      if (!Number.isFinite(v) || v < 0) v = NaN;
      if (Number.isFinite(v)) {
        // clamp to monotonic (IB official volume updates can occasionally “snap”)
        if (Number.isFinite(this.lastCumVol)) v = Math.max(v, this.lastCumVol);
        this.lastCumVol = v;
      }

      if (!this.cur || this.cur.t0 !== minute0) {
        this._rollTo(minute0);
      }

      const bar = this.cur;

      // open/close from last price stream (trade price preferred when present)
      if (Number.isFinite(px)) {
        if (!Number.isFinite(bar.open)) bar.open = px;
        bar.close = px;
      } else if (Number.isFinite(this.lastPrice)) {
        if (!Number.isFinite(bar.open)) bar.open = this.lastPrice;
        bar.close = this.lastPrice;
      }

      // volume = cumulative delta from minute baseline
      if (Number.isFinite(v)) {
        if (!Number.isFinite(bar.baseVol)) bar.baseVol = v;
        bar.vol = Math.max(0, v - bar.baseVol);
      }

      this.requestDraw();
    }

    currentVol() {
      return this.cur ? (Number(this.cur.vol) || 0) : 0;
    }

    attachHoverUI(wrapEl, tooltipEl) {
      this._wrapEl = wrapEl;
      this._tooltipEl = tooltipEl;
    }

    hideTooltip() {
      if (!this._tooltipEl) return;
      this._tooltipEl.classList.add('hidden');
    }

    handleHover(e) {
      if (!this._geom || !this._tooltipEl || !this._wrapEl) return;

      const canvasRect = this.canvas.getBoundingClientRect();
      const x = e.clientX - canvasRect.left;
      const y = e.clientY - canvasRect.top;

      const { x0, bw, gap, viewN, series } = this._geom;
      const totalW = bw * viewN + gap * (viewN - 1);

      // Must be over the bar region (and not in the gap)
      if (x < x0 || x > x0 + totalW || y < 0 || y > canvasRect.height) {
        this.hideTooltip();
        return;
      }

      const step = bw + gap;
      let idx = Math.floor((x - x0) / step);
      idx = Math.max(0, Math.min(viewN - 1, idx));

      const within = (x - x0) - idx * step;
      if (within > bw) { // cursor is in the gap
        this.hideTooltip();
        return;
      }

      const b = series[idx];
      if (!b) { this.hideTooltip(); return; }

      const vol = Math.max(0, Math.floor(Number(b.vol) || 0));
      const t0 = Number(b.t0);
      const timeLabel = Number.isFinite(t0) ? this._fmtTimeLabel(t0) : '';

      this._tooltipEl.innerHTML = `
        <div class="tt-time">${timeLabel}</div>
        <div class="tt-vol">${formatVolume9(vol)}</div>
      `;
      this._tooltipEl.classList.remove('hidden');

      // Position near cursor, clamped within the wrap
      const wrapRect = this._wrapEl.getBoundingClientRect();
      const mx = e.clientX - wrapRect.left;
      const my = e.clientY - wrapRect.top;

      const tw = this._tooltipEl.offsetWidth;
      const th = this._tooltipEl.offsetHeight;

      let left = mx + 10;
      let top  = my - th - 10;

      if (left + tw > wrapRect.width) left = mx - tw - 10;
      if (left < 0) left = 0;
      if (top < 0) top = my + 10;
      if (top + th > wrapRect.height) top = Math.max(0, wrapRect.height - th);

      this._tooltipEl.style.left = `${left}px`;
      this._tooltipEl.style.top  = `${top}px`;
    }

    _seriesForDraw() {
      const out = this.bars.slice();
      if (this.cur) out.push({ t0: this.cur.t0, open: this.cur.open, close: this.cur.close, vol: this.cur.vol });
      return out;
    }

    _fmtK(n) {
      // 25000 -> "25.0k"
      const x = Number(n) || 0;
      return (x / 1000).toFixed(1) + 'k';
    }

    _fmtTimeLabel(t0) {
      const dt = new Date(t0);
      // "9:35" style (strip AM/PM if locale adds it)
      const s = dt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      return String(s).split(' ')[0];
    }

    draw() {
      const ctx = this.ctx;
      const rect = this.canvas.getBoundingClientRect();
      const W = rect.width || 1;
      const H = rect.height || 1;
      const dpr = this.dpr || 1;

      ctx.save();
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, W, H);

      // background
      ctx.fillStyle = this.bg;
      ctx.fillRect(0, 0, W, H);

      const compact = document.body.classList.contains('compact');
      const fontSize = compact ? 10 : 11;
      ctx.font = `${fontSize}px ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace`;

      const padL = 6;
      const padR = 52;  // space for right-axis labels
      const padT = 6;
      const padB = compact ? 16 : 18; // time labels

      const innerW = Math.max(1, W - padL - padR);
      const innerH = Math.max(1, H - padT - padB);
      const xL = padL;
      const yT = padT;
      const yB = padT + innerH;

      const seriesAll = this._seriesForDraw();
      const nAll = seriesAll.length;
      if (!nAll) {
        this._geom = null;
        this.hideTooltip();
        // draw an empty baseline
        ctx.strokeStyle = this.grid;
        ctx.globalAlpha = 0.9;
        ctx.beginPath(); ctx.moveTo(xL, yB); ctx.lineTo(xL + innerW, yB); ctx.stroke();
        ctx.restore();
        return;
      }

      const viewN = Math.max(6, Math.min(this.viewBars, nAll));
      let series = seriesAll.slice(nAll - viewN);

      // Startup edge case: if we have fewer bars than the minimum view window,
      // pad with zero-volume minutes so we never index undefined.
      if (series.length < viewN) {
        const padCount = viewN - series.length;
        const first = series[0] || seriesAll[0] || {};
        const firstT0 = Number(first.t0);

        const t0 = Number.isFinite(firstT0)
          ? firstT0
          : Math.floor(Date.now() / 60000) * 60000;

        const basePx =
          (Number.isFinite(Number(first.open)) ? Number(first.open)
          : (Number.isFinite(Number(first.close)) ? Number(first.close)
          : (Number.isFinite(Number(this.lastPrice)) ? Number(this.lastPrice) : NaN)));

        const pad = [];
        for (let i = padCount; i > 0; i--) {
          pad.push({ t0: t0 - i * 60000, open: basePx, close: basePx, vol: 0 });
        }
        series = pad.concat(series);
      }

      let maxVol = 0;
      for (const b of series) maxVol = Math.max(maxVol, Number(b.vol) || 0);
      const step = this.step;
      const MIN_Y = 100000; // always show at least: 25k, 50k, 75k, 100k
      const maxY = Math.max(MIN_Y, step, (Math.ceil(maxVol / step) + 1) * step); // +25k headroom

      // grid + right-axis labels (0..maxY step 25k)
      ctx.strokeStyle = this.grid;
      ctx.fillStyle = this.axis;
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.9;

      const ticks = Math.floor(maxY / step);
      for (let i = 0; i <= ticks; i++) {
        const v = i * step;
        const y = yB - (v / maxY) * innerH;
        ctx.globalAlpha = (v === 0) ? 0.65 : 0.85;
        ctx.beginPath();
        ctx.moveTo(xL, y + 0.5);
        ctx.lineTo(xL + innerW, y + 0.5);
        ctx.stroke();

        // label on right
        ctx.globalAlpha = 0.95;
        const label = (v === 0) ? '0' : this._fmtK(v);
        ctx.fillText(label, xL + innerW + 6, y + 4);
      }

      // bars geometry (right-aligned, like most platforms)
      const gap = 1;
      const bw = Math.max(3, Math.floor((innerW - gap * (viewN - 1)) / viewN));
      const totalW = bw * viewN + gap * (viewN - 1);
      const x0 = xL + (innerW - totalW);

      // NEW: hover geometry snapshot
      this._geom = { x0, bw, gap, viewN, series };

      for (let i = 0; i < viewN; i++) {
        const b = series[i];
        const vol = Math.max(0, Number(b.vol) || 0);
        const open = Number(b.open);
        const close = Number(b.close);

        let c = this.flat;
        if (Number.isFinite(open) && Number.isFinite(close)) {
          if (close > open) c = this.up;
          else if (close < open) c = this.down;
        }

        const h = (vol / maxY) * innerH;
        const x = x0 + i * (bw + gap);
        const y = yB - h;

        ctx.globalAlpha = 0.95;
        ctx.fillStyle = c;
        ctx.fillRect(x, y, bw, h);

        // subtle outline for the max bar(s)
        if (maxVol > 0 && vol >= maxVol) {
          ctx.globalAlpha = 0.55;
          ctx.strokeStyle = this.text;
          ctx.strokeRect(x + 0.5, y + 0.5, Math.max(1, bw - 1), Math.max(1, h - 1));
        }
      }

      // time markers every 5 minutes + date marker at left
      ctx.globalAlpha = 0.95;
      ctx.fillStyle = this.axis;
      const date0 = new Date(series[0].t0);
      const dateLabel = date0.toLocaleDateString([], { month: 'numeric', day: 'numeric' });
      ctx.fillText(dateLabel, x0, H - 4);

      for (let i = 0; i < viewN; i++) {
        const b = series[i];
        const t0 = b.t0;
        if (!Number.isFinite(t0)) continue;
        const dt = new Date(t0);
        const m = dt.getMinutes();
        if ((m % 5) !== 0) continue;

        const label = this._fmtTimeLabel(t0);
        const x = x0 + i * (bw + gap) + bw * 0.5;
        // small tick
        ctx.globalAlpha = 0.55;
        ctx.strokeStyle = this.axis;
        ctx.beginPath();
        ctx.moveTo(x, yB + 2);
        ctx.lineTo(x, yB + 6);
        ctx.stroke();
        // label
        ctx.globalAlpha = 0.95;
        ctx.textAlign = 'center';
        ctx.fillText(label, x, H - 4);
        ctx.textAlign = 'start';
      }

      ctx.restore();
    }
  }

  function initVol1mChart() {
    const c = els.vol1mCanvas;
    if (!c) return;
    vol1mChart = new Vol1mChart(c, { step: 25000, viewBars: 24, maxBars: 240 });
    vol1mChart.draw(); // skeleton immediately

    // NEW: initialize the live badge to zero
    setCurrentVolBarBadge(0);

    // NEW: hover tooltip
    const wrap = c.closest('.vol1m-wrap');
    if (wrap) {
      let tip = wrap.querySelector('.vol-tooltip');
      if (!tip) {
        tip = document.createElement('div');
        tip.className = 'vol-tooltip hidden';
        wrap.appendChild(tip);
      }
      vol1mChart.attachHoverUI(wrap, tip);

      c.addEventListener('mousemove', (e) => vol1mChart.handleHover(e));
      c.addEventListener('mouseleave', () => vol1mChart.hideTooltip());
      c.addEventListener('touchstart', () => vol1mChart.hideTooltip(), { passive: true });
    }
  }
  class ObiMiniChart {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.max = opts.maxPoints || 360;           // ~last few hundred ticks
      this.data = new Float32Array(this.max);
      this.len = 0;
      this.head = 0;                               // ring buffer head
      this.lineColor = cssVar('--obi-line', '#ffcc00');
      this.gridColor = cssVar('--obi-grid', '#223149');
      this.zeroColor = cssVar('--obi-zero', '#485a70');
      this.posColor  = cssVar('--obi-line-pos', '#2ecc71'); // NEW
      this.negColor  = cssVar('--obi-line-neg', '#ff6b6b'); // NEW
      this.dpr = 1;
      this.resize();
      window.addEventListener('resize', () => { this.resize(); this.draw(); });
    }
    push(v) {
      const val = Math.max(-1, Math.min(1, Number(v)));
      this.data[this.head] = val;
      this.head = (this.head + 1) % this.max;
      this.len = Math.min(this.len + 1, this.max);
    }
    resize() {
      const rect = this.canvas.getBoundingClientRect();
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      this.dpr = dpr;
      // set backing store size for crisp lines on HiDPI
      this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    }
    draw() {
      const ctx = this.ctx;
      const dpr = this.dpr;
      const rect = this.canvas.getBoundingClientRect();
      const W = rect.width, H = rect.height;
      ctx.save();
      ctx.scale(dpr, dpr);               // draw in CSS pixels
      // clear
      ctx.clearRect(0, 0, W, H);
      const pad = 4;                      // small insets to avoid clipping stroke caps
      const innerW = Math.max(1, W - pad * 2);
      const innerH = Math.max(1, H - pad * 2);
      const mapY = (v) => pad + (1 - (v + 1) / 2) * innerH;  // v=+1 -> top, v=-1 -> bottom
      // bounds: ±1
      ctx.strokeStyle = this.gridColor;
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.75;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(pad, mapY(+1)); ctx.lineTo(W - pad, mapY(+1)); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(pad, mapY(-1)); ctx.lineTo(W - pad, mapY(-1)); ctx.stroke();
      // zero line
      ctx.setLineDash([]);
      ctx.globalAlpha = 0.95;
      ctx.strokeStyle = this.zeroColor;
      ctx.beginPath(); ctx.moveTo(pad, mapY(0)); ctx.lineTo(W - pad, mapY(0)); ctx.stroke();
      // series
      if (this.len > 0) {
        const n = this.len;
        const dx = innerW / (this.max - 1);          // fixed step → right-aligned scroll
        let x0 = W - pad - (n - 1) * dx;
        if (x0 < pad) x0 = pad;
        const start = (this.head - n + this.max) % this.max;

        const POS_T = 0.10, NEG_T = -0.10;          // thresholds for coloring

        const idx = (i) => (start + i) % this.max;
        const regionColor = (v) => {
          if (v > POS_T) return this.posColor;
          if (v < NEG_T) return this.negColor;
          return this.lineColor;
        };

        const drawSeg = (xA, yA, vA, xB, yB, vB, color) => {
          ctx.strokeStyle = color;
          ctx.beginPath();
          ctx.moveTo(xA, yA);
          ctx.lineTo(xB, yB);
          ctx.stroke();
        };

        ctx.lineWidth  = 1.6;
        ctx.lineCap    = 'round';
        ctx.lineJoin   = 'round';
        ctx.globalAlpha = 1.0;

        // For each consecutive pair of points, draw one or more tiny segments,
        // splitting exactly at crossings of -0.10 and +0.10.
        for (let i = 1; i < n; i++) {
          const v0 = this.data[idx(i - 1)];
          const v1 = this.data[idx(i)];
          const xA = x0 + (i - 1) * dx, yA = mapY(v0);
          const xB = x0 + i * dx,       yB = mapY(v1);

          // Build a list of split points at threshold crossings (at most 2).
          const pts = [{ x: xA, y: yA, v: v0 }];

          const maybeAddCross = (thr) => {
            const a = v0, b = v1;
            // Detect crossing of 'thr' in either direction (exclude identical both-sides cases).
            if ((a < thr && b >= thr) || (a > thr && b <= thr)) {
              const t = (thr - a) / (b - a);           // 0..1 along the segment
              const xc = xA + t * (xB - xA);
              pts.push({ x: xc, y: mapY(thr), v: thr });
            }
          };

          maybeAddCross(NEG_T);
          maybeAddCross(POS_T);

          pts.push({ x: xB, y: yB, v: v1 });
          pts.sort((p, q) => p.x - q.x);               // ensure left→right

          // Draw each sub‑segment in its regime color
          for (let j = 1; j < pts.length; j++) {
            const p0 = pts[j - 1], p1 = pts[j];
            const mid = (p0.v + p1.v) * 0.5;           // safe to pick color by midpoint
            drawSeg(p0.x, p0.y, p0.v, p1.x, p1.y, p1.v, regionColor(mid));
          }
        }
      }
      ctx.restore();
    }
  }
  function initObiMiniChart() {
    const c = document.getElementById('obiMiniCanvas');
    if (!c) return;
    obiChart = new ObiMiniChart(c, { maxPoints: 360 });
    // draw an empty grid immediately (nice skeleton on load)
    obiChart.draw();
  }

})();
