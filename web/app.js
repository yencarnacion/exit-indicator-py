(() => {
  const els = {
    status: document.getElementById('statusBadge'),
    sym: document.getElementById('symbolInput'),
    thr: document.getElementById('thresholdInput'),
    start: document.getElementById('startBtn'),
    stop: document.getElementById('stopBtn'),
    sideAsk: document.getElementById('sideAsk'),
    sideBid: document.getElementById('sideBid'),
    bookTitle: document.getElementById('bookTitle'),
    test: document.getElementById('testSoundBtn'),
    bookBody: document.querySelector('#bookTable tbody'),
    log: document.getElementById('alertLog'),
    compact: document.getElementById('compactToggle'),
  };

  let ws;
  let audio;
  let audioReady = false;
  let soundURL = '';
  let soundAvailable = false;

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

  function setBookTitle(side) {
    if (!els.bookTitle) return;
    const label = side === 'BID' ? 'Bid' : 'Offer';
    els.bookTitle.textContent = `Top‑10 ${label} Levels (SMART aggregated)`;
  }

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
      setBookTitle(currentSide());
      soundURL = cfg.soundURL || '';
      soundAvailable = !!cfg.soundAvailable;
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

    // Compact preference
    const savedCompact = localStorage.getItem('ei.compact') === '1';
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

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {};
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'status') {
          setStatus(!!msg.data.connected, msg.data.symbol || '');
          if (msg.data.side) {
            if (msg.data.side === 'BID') { if (els.sideBid) els.sideBid.checked = true; } else { if (els.sideAsk) els.sideAsk.checked = true; }
            setBookTitle(msg.data.side);
          }
        } else if (msg.type === 'book') {
          if (msg.data.side) setBookTitle(msg.data.side);
          renderBook(msg.data.levels || msg.data.asks || []);
        } else if (msg.type === 'alert') {
          appendAlert(msg.data);
          pulseRowForAlert(msg.data);
          playSound();
        } else if (msg.type === 'error') {
          appendError(msg.data.message || 'Error');
        }
      } catch (e) {
        console.warn('bad ws message', e);
      }
    };
    ws.onclose = () => {
      setStatus(false, '');
      setTimeout(connectWS, 1000);
    };
  }

  function renderBook(asks) {
    const tbody = els.bookBody;
    const thr = Math.max(1, parseInt(els.thr.value || '0', 10) || 1);
    tbody.innerHTML = '';

    asks.forEach(row => {
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
      label.textContent = `${formatShares(size)}  ${ratioTxt}`;
      meter.appendChild(label);
      sizeTd.appendChild(meter);

      if (row.rank === 0) tr.classList.add('best');
      if (ratio >= 1.0) tr.classList.add('over');

      tr.append(rankTd, priceTd, sizeTd);
      tbody.appendChild(tr);
    });
  }

  function pulseRowForAlert(a) {
    const key = priceKey(a.price);
    const row = els.bookBody.querySelector(`tr[data-price="${key}"]`);
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
    els.log.prepend(el);
  }

  function appendError(msg) {
    const el = document.createElement('div');
    el.className = 'log-item error';
    el.textContent = `Error: ${msg}`;
    els.log.prepend(el);
  }

  async function start() {
    const symbol = (els.sym.value || '').trim();
    if (!symbol) {
      els.sym.focus();
      return;
    }
    const threshold = parseInt(els.thr.value || '0', 10);
    const res = await fetch('/api/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ symbol, threshold, side: currentSide() })
    });
    if (!res.ok) {
      const txt = await res.text();
      appendError(`Start failed: ${txt}`);
    }
  }

  async function stop() {
    const res = await fetch('/api/stop', { method: 'POST' });
    if (!res.ok) {
      const txt = await res.text();
      appendError(`Stop failed: ${txt}`);
    }
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

  // Boot
  initConfig().then(connectWS);
})();
