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
  };
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
  function renderBooks(data) {
    clearLoadingTimer();
    const thr = Math.max(1, parseInt(els.thr.value || '0', 10) || 1);
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
        meter.appendChild(fill); meter.appendChild(label);
        sizeTd.appendChild(meter);
        tr.append(rankTd, priceTd, sizeTd);
        tbody.appendChild(tr);
      }
    };
    makeSide(els.bookBidBody, data.bids || [], 'BID');
    makeSide(els.bookAskBody, data.asks || [], 'ASK');
    if (data.stats) updateStats(data.stats);
  }
  function updateStats(s) {
    const fmtP = (x) => (Number.isFinite(+x) ? (+x).toFixed(2) : '—');
    const fmtV = (x) => (Number.isFinite(+x) ? Number(x).toLocaleString() : '—');
    const bb = (s.bestBid != null) ? +s.bestBid : null;
    const ba = (s.bestAsk != null) ? +s.bestAsk : null;
    const sp = (bb != null && ba != null) ? (ba - bb) : null;
    if (els.bestBid) els.bestBid.textContent = fmtP(bb);
    if (els.bestAsk) els.bestAsk.textContent = fmtP(ba);
    if (els.spread)  els.spread.textContent  = fmtP(sp);
    if (els.last)    els.last.textContent    = fmtP(s.last);
    if (els.vol)     els.vol.textContent     = fmtV(s.volume);
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
  }
  function tsRow(ev){
    const row = document.createElement('div');
    row.className = 'row' + (ev.big ? ' big' : '');
    const colorClass = {
      "above_ask":"col-yellow","at_ask":"col-green","between_mid":"col-white",
      "between_ask":"col-white","between_bid":"col-white","at_bid":"col-red","below_bid":"col-magenta"
    }[ev.side] || "col-white";
    const priceStr = fmt2(ev.price);
    row.innerHTML = `
      <div class="left">
        <span class="badge ${colorClass}">${ev.side.replaceAll('_',' ')}</span>
        <span class="price ${colorClass}">${priceStr}</span>
        <span class="amt">$${ev.amountStr || ''}</span>
      </div>
      <div class="time">${new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})}</div>
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
    if (!els.tape) return;
    // prepend and keep scrolled to TOP unless user has scrolled away
    prependTapeRow(tsRow(ev));
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
  initConfig().then(connectWS);
})();
