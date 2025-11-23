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

    /* NEW: DOM history canvases + footprint strip */
    domHistBid: document.getElementById('domHistoryBid'),
    domHistAsk: document.getElementById('domHistoryAsk'),
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
    dollarHidden: document.getElementById('dollarHidden'),
    microVwapHidden: document.getElementById('microVwapHidden'),
    microBandSelect: document.getElementById('microBandSelect'),
    microVwapVal: document.getElementById('microVwapVal'),
    actionHintPill: document.getElementById('actionHintPill'),
  };

  // --- DOM history + orderflow state (client-side only) ---
  const DOM_HISTORY_LEN = 60;            // number of DOM snapshots per side
  const BUBBLE_LIFETIME_MS = 1500;       // big-trade bubble lifetime
  const FOOTPRINT_WINDOW_MS = 5000;      // recent-flow window for footprint

  // Per-rank recent delta accumulators (shares) for BID/ASK ladders
  const footprint = {
    BID: Array.from({ length: 10 }, () => ({ buyVol: 0, sellVol: 0, lastUpdate: 0 })),
    ASK: Array.from({ length: 10 }, () => ({ buyVol: 0, sellVol: 0, lastUpdate: 0 })),
  };

  // Big-trade bubbles keyed by side + priceKey
  const tradeBubbles = {
    BID: Object.create(null),
    ASK: Object.create(null),
  };

  // Last DOM snapshot (top 10 per side) for trade → DOM mapping
  let currentBook = { bids: [], asks: [] };

  // DOM history heatmap instances per side
  let domHistBidChart = null;
  let domHistAskChart = null;

  // --- OBI mini chart handle ---
  let obiChart = null;
  let ws;
  let audio;
  let audioReady = false;
  let soundURL = '';
  let soundAvailable = false;
  let globalSilent = false;
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
    // Compact preference: default ON if unset
    const saved = localStorage.getItem('ei.compact');
    const savedCompact = (saved === null) ? true : (saved === '1');
    if (els.compact) {
      els.compact.checked = savedCompact;
    }
    document.body.classList.toggle('compact', savedCompact);
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
    constructor(map){ this.map = map; this.ctx=null; this.buffers=new Map(); this.gain=null; this.active=[]; }
    async init(){
      const AC = window.AudioContext || window.webkitAudioContext; if(!AC) return false;
      this.ctx = new AC(); this.gain = this.ctx.createGain(); this.gain.connect(this.ctx.destination);
      for (const [k,u] of Object.entries(this.map)) {
        try { const resp = await fetch(u, {cache:"force-cache"}); const buf=await resp.arrayBuffer();
              this.buffers.set(k, await this.ctx.decodeAudioData(buf)); } catch {}
      }
      return this.buffers.size>0;
    }
    async resume(){ if(this.ctx && this.ctx.state==="suspended") try{ await this.ctx.resume(); }catch{} }
    play(k, rate=1){ if(globalSilent) return; const b=this.buffers.get(k); if(!b) return;
      const s=this.ctx.createBufferSource(); s.buffer=b; s.playbackRate.value=rate; s.connect(this.gain); s.start();
      this.active.push(s); s.onended=()=>{ const i=this.active.indexOf(s); if(i>=0) this.active.splice(i,1); };
    }
    stop(){ for(const s of this.active){ try{s.stop(0);}catch{} } this.active.length=0; }
  }
  (async () => { const m = new Mixer(TS_AUDIO.urls); TS_AUDIO.ready = await m.init(); TS_AUDIO.engine = m; })();
  function tsPlay(key, rate=1){ if(!TS_AUDIO.ready) return; TS_AUDIO.engine.play(key, rate); }
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
    ws.onopen = () => { try { TS_AUDIO.engine && TS_AUDIO.engine.resume(); } catch {} };
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
        } else if (msg.type === 'error') {
          // MODIFIED: Ignore harmless Error 310
          if (!msg.data.message.includes('Error 310')) {
            appendError(msg.data.message || 'Error');
          }
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

  function syncFootprintHeights() {
    const tbody = els.footprintBody;
    if (!tbody) return;
    const fpRows = tbody.querySelectorAll('tr');
    const bidRows = els.bookBidBody ? els.bookBidBody.querySelectorAll('tr') : [];
    const askRows = els.bookAskBody ? els.bookAskBody.querySelectorAll('tr') : [];
    const count = Math.min(fpRows.length, bidRows.length, askRows.length);
    if (!count) return;
    for (let i = 0; i < count; i++) {
      const br = bidRows[i];
      const ar = askRows[i];
      const h = Math.max(
        br ? br.getBoundingClientRect().height : 0,
        ar ? ar.getBoundingClientRect().height : 0
      );
      if (h > 0) {
        fpRows[i].style.height = h + 'px';
      }
    }
  }

  function renderFootprintStrip(threshold) {
    const tbody = els.footprintBody;
    if (!tbody) return;
    const now = Date.now();

    // Decay / windowing
    for (const side of ['BID', 'ASK']) {
      const arr = footprint[side];
      if (!arr) continue;
      for (let i = 0; i < arr.length; i++) {
        const cell = arr[i];
        if (!cell) continue;
        if (cell.lastUpdate && (now - cell.lastUpdate) > FOOTPRINT_WINDOW_MS) {
          cell.buyVol = 0;
          cell.sellVol = 0;
          cell.lastUpdate = 0;
        }
      }
    }

    tbody.innerHTML = '';
    const levels = 10;
    const norm = Math.max(1, threshold || 1);

    for (let i = 0; i < levels; i++) {
      const askCell = footprint.ASK[i] || { buyVol: 0, sellVol: 0 };
      const bidCell = footprint.BID[i] || { buyVol: 0, sellVol: 0 };

      // Net aggressive flow at this rank across both sides:
      // buys (at/above ask) – sells (at/below bid)
      const buy = (askCell.buyVol || 0) + (bidCell.buyVol || 0);
      const sell = (askCell.sellVol || 0) + (bidCell.sellVol || 0);
      const net = buy - sell;
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

    // Try to keep rows visually aligned with the DOM tables
    syncFootprintHeights();
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

    // --- bubbles ---
    const bucket = tradeBubbles[bookSide] || (tradeBubbles[bookSide] = Object.create(null));
    bucket[key] = {
      kind: isBuyAgg ? 'buy' : 'sell',
      ts: now,
      big: !!ev.big,
    };

    // --- footprint ---
    const rank = (row && typeof row.rank === 'number') ? row.rank : match.levelIdx;
    if (rank < 0 || rank >= 10) return;
    const fpCell = footprint[bookSide][rank];
    const sz = Number(ev.size) || 0;
    if (sz <= 0) return;

    if (isBuyAgg) {
      fpCell.buyVol += sz;
    } else {
      fpCell.sellVol += sz;
    }
    fpCell.lastUpdate = now;
  }

  function renderBooks(data) {
    clearLoadingTimer();
    const thr = Math.max(1, parseInt(els.thr.value || '0', 10) || 1);

    // Snapshot current book for all order‑flow visuals
    currentBook.bids = Array.isArray(data.bids) ? data.bids.slice(0, 10) : [];
    currentBook.asks = Array.isArray(data.asks) ? data.asks.slice(0, 10) : [];

    // DOM history mini heatmaps (per side)
    if (domHistBidChart || domHistAskChart) {
      const bidVec = buildDomSnapshot(currentBook.bids);
      const askVec = buildDomSnapshot(currentBook.asks);
      if (domHistBidChart) {
        domHistBidChart.push(bidVec);
        domHistBidChart.draw();
      }
      if (domHistAskChart) {
        domHistAskChart.push(askVec);
        domHistAskChart.draw();
      }
    }

    // Age out expired bubbles
    pruneBubbles(Date.now());

    const makeSide = (tbody, rows, side) => {
      tbody.innerHTML = '';
      // Ensure exactly 10 rows rendered
      for (let i = 0; i < 10; i++) {
        const r = rows[i];
        const tr = document.createElement('tr');
        if (r) tr.dataset.price = priceKey(r.price);
        const rankTd = document.createElement('td');
        rankTd.className = 'col-rank';
        rankTd.textContent = r ? r.rank : '';
        const priceTd = document.createElement('td');
        priceTd.className = 'col-price';
        if (r) {
          const priceNum = (typeof r.price === 'string') ? parseFloat(r.price) : r.price;
          priceTd.textContent = Number.isFinite(priceNum) ? priceNum.toFixed(2) : String(r.price);
        } else {
          priceTd.textContent = '';
        }
        const sizeTd = document.createElement('td');
        sizeTd.className = 'col-size';
        const meter = document.createElement('div'); meter.className = 'meter';
        const fill = document.createElement('div'); fill.className = 'fill';
        const label = document.createElement('span'); label.className = 'label';

        if (r) {
          const size = r.sumShares || 0;
          const ratio = size / thr;
          const width = Math.min(1, ratio) * 100;
          if (ratio >= 2) fill.classList.add('danger');
          else if (ratio >= 1.5) fill.classList.add('hot');
          else if (ratio >= 1.0) fill.classList.add('warn');
          fill.style.width = width.toFixed(2) + '%';
          const ratioTxt = ratio.toFixed(2) + '×';
          label.textContent = `${formatShares(size)} ${ratioTxt}`;
          if (r.rank === 0) tr.classList.add('best');
          if (ratio >= 1.0) tr.classList.add('over');
        } else {
          fill.style.width = '0%';
          label.textContent = '';
        }

        meter.appendChild(fill);
        meter.appendChild(label);

        // Big‑trade bubble overlay
        if (r) {
          const sideKey = side === 'BID' ? 'BID' : 'ASK';
          const bubblesForSide = tradeBubbles[sideKey];
          const b = bubblesForSide && bubblesForSide[priceKey(r.price)];
          if (b) {
            const bubble = document.createElement('span');
            bubble.className = 'bubble ' + (b.kind === 'buy' ? 'bubble-buy' : 'bubble-sell');
            if (b.big) bubble.classList.add('bubble-big');
            meter.appendChild(bubble);
          }
        }

        sizeTd.appendChild(meter);
        tr.append(rankTd, priceTd, sizeTd);
        tbody.appendChild(tr);
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
  function onTSQuote(q){
    const { bid, ask } = q;
    if (els.tapeBid && bid!=null) els.tapeBid.textContent = fmt2(bid);
    if (els.tapeAsk && ask!=null) els.tapeAsk.textContent = fmt2(ask);
    if (els.tapeSpread && bid!=null && ask!=null) els.tapeSpread.textContent = fmt2(ask - bid);
    // Refresh volume whenever server includes it on quotes
    if (els.vol && q && q.volume != null) {
      els.vol.textContent = formatVolumeK(q.volume);
    }
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

    // Feed DOM bubbles + micro-footprint accumulators
    updateOrderflowFromTrade(ev);

    // sound (mute respected)
    if (globalSilent || !TS_AUDIO.ready) return;
    const big = !!ev.big;
    switch (ev.side){
      case "above_ask":   tsPlay("above_ask", big?1.5:1.0); break;
      case "at_ask":      tsPlay("buy",       big?1.5:1.0); break;
      case "between_mid": tsPlay("between",   1.0); break;
      case "between_ask": tsPlay("u",         1.0); break;
      case "between_bid": tsPlay("d",         1.0); break;
      case "at_bid":      tsPlay("sell",      big?0.85:1.0); break;
      case "below_bid":   tsPlay("below_bid", big?0.85:1.0); break;
    }
  }
  async function start() {
    const symbol = (els.sym.value || '').trim().toUpperCase(); // Uppercase symbol
    if (!symbol) {
      els.sym.focus();
      return;
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
  els.sym.addEventListener('keydown', (e) => { if (e.key === 'Enter') start(); });
  els.thr.addEventListener('keydown', (e) => { if (e.key === 'Enter') updateThreshold(); });
  els.thr.addEventListener('change', updateThreshold);
  if (els.sideAsk) els.sideAsk.addEventListener('change', () => { if (els.sideAsk.checked) { setBookTitle('ASK'); setSide('ASK'); } });
  if (els.sideBid) els.sideBid.addEventListener('change', () => { if (els.sideBid.checked) { setBookTitle('BID'); setSide('BID'); } });
  if (els.compact) {
    els.compact.addEventListener('change', () => {
      document.body.classList.toggle('compact', els.compact.checked);
      localStorage.setItem('ei.compact', els.compact.checked ? '1' : '0');

      // Re-layout canvases when height changes
      if (obiChart && typeof obiChart.resize === 'function') {
        obiChart.resize();
        obiChart.draw();
      }
      if (domHistBidChart && typeof domHistBidChart.resize === 'function') {
        domHistBidChart.resize();
        domHistBidChart.draw();
      }
      if (domHistAskChart && typeof domHistAskChart.resize === 'function') {
        domHistAskChart.resize();
        domHistAskChart.draw();
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
    initObiMiniChart();
    initDomHistoryCharts();
    connectWS();
  });

  // --------- OBI mini chart implementation ----------
  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
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

  // --------- DOM history mini heatmap implementation ----------
  class DomHistoryHeatmap {
    constructor(canvas, side, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.side = side;
      this.levels = opts.levels || 10;
      this.maxSnapshots = opts.maxSnapshots || DOM_HISTORY_LEN;
      this.data = new Array(this.maxSnapshots); // ring buffer of Float32Array(levels)
      this.len = 0;
      this.head = 0;
      this.dpr = 1;
      this.bg = '#050910';

      const bidColor = cssVar('--dom-bid', 'rgba(79,179,255,1)');
      const askColor = cssVar('--dom-ask', 'rgba(255,123,123,1)');
      this.baseColor = (side === 'BID') ? bidColor : askColor;

      this.resize();
      window.addEventListener('resize', () => { this.resize(); this.draw(); });
    }
    push(vec) {
      if (!(vec instanceof Float32Array)) {
        vec = new Float32Array(vec);
      }
      if (vec.length !== this.levels) {
        const v = new Float32Array(this.levels);
        const copyLen = Math.min(this.levels, vec.length);
        for (let i = 0; i < copyLen; i++) v[i] = vec[i];
        vec = v;
      }
      this.data[this.head] = vec;
      this.head = (this.head + 1) % this.maxSnapshots;
      if (this.len < this.maxSnapshots) this.len++;
    }
    resize() {
      if (!this.canvas) return;
      const rect = this.canvas.getBoundingClientRect();
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      this.dpr = dpr;
      this.canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      this.canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    }
    _parseBaseColor() {
      const ctx = this.ctx;
      ctx.save();
      ctx.fillStyle = this.baseColor;
      const computed = ctx.fillStyle;
      ctx.restore();
      const m = computed.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
      if (!m) return { r: 79, g: 179, b: 255 };
      return { r: +m[1], g: +m[2], b: +m[3] };
    }
    draw() {
      const ctx = this.ctx;
      if (!ctx) return;
      const rect = this.canvas.getBoundingClientRect();
      const Wcss = rect.width || 1;
      const Hcss = rect.height || 1;
      const dpr = this.dpr || 1;

      ctx.save();
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, Wcss, Hcss);

      // background
      ctx.fillStyle = this.bg;
      ctx.fillRect(0, 0, Wcss, Hcss);

      const n = this.len;
      if (!n) {
        ctx.restore();
        return;
      }

      const levels = this.levels;
      const cellW = Wcss / this.maxSnapshots;
      const cellH = Hcss / levels;
      const x0 = Wcss - n * cellW; // right-align snapshots

      const { r, g, b } = this._parseBaseColor();

      for (let i = 0; i < n; i++) {
        const idx = (this.head - n + i + this.maxSnapshots) % this.maxSnapshots;
        const vec = this.data[idx];
        if (!vec) continue;
        const x = x0 + i * cellW;
        for (let level = 0; level < levels; level++) {
          const v = Math.max(0, Math.min(1, vec[level] || 0));
          if (v <= 0) continue;
          const y = level * cellH;
          const alpha = 0.12 + 0.88 * v;
          ctx.fillStyle = `rgba(${r},${g},${b},${alpha.toFixed(3)})`;
          ctx.fillRect(x, y, cellW + 0.5, cellH + 0.5);
        }
      }
      ctx.restore();
    }
  }

  function initDomHistoryCharts() {
    const bidCanvas = els.domHistBid;
    const askCanvas = els.domHistAsk;
    if (bidCanvas) {
      domHistBidChart = new DomHistoryHeatmap(bidCanvas, 'BID', { maxSnapshots: DOM_HISTORY_LEN, levels: 10 });
      domHistBidChart.draw();
    }
    if (askCanvas) {
      domHistAskChart = new DomHistoryHeatmap(askCanvas, 'ASK', { maxSnapshots: DOM_HISTORY_LEN, levels: 10 });
      domHistAskChart.draw();
    }
  }
})();
