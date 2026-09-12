// ============================================================
// SentinelAI dashboard — MQTT client
// Subscribes to:  sentinel/<device>/telemetry
// ============================================================

const DEVICE_ID   = "sentinel";
const BROKER_URL  = "ws://10.87.61.232:9001";
const MQTT_USER   = "sentinel";
const MQTT_PASS   = "87654321";

const TOPIC_TELEMETRY = `sentinel/${DEVICE_ID}/telemetry`;

// ---------- DOM handles ----------
const els = {
  conn:      document.getElementById('conn-status'),
  card:      document.getElementById('live-card'),
  badge:     document.getElementById('level-badge'),
  hazard:    document.getElementById('hazard'),
  riskFill:  document.getElementById('risk-fill'),
  riskVal:   document.getElementById('risk-value'),
  confVal:   document.getElementById('conf-value'),
  tsVal:     document.getElementById('ts-value'),
  rules:     document.getElementById('rules-list'),
  devices:   document.getElementById('devices'),
  tbody:     document.querySelector('#events tbody'),
  sensors: {
    temp:    document.getElementById('s-temp'),
    hum:     document.getElementById('s-hum'),
    mq2:     document.getElementById('s-mq2'),
    mq135:   document.getElementById('s-mq135'),
    flame:   document.getElementById('s-flame'),
    pir:     document.getElementById('s-pir'),
    current: document.getElementById('s-current'),
  },
  beliefs: {
    safe: { bar: document.getElementById('b-safe'), val: document.getElementById('b-safe-v') },
    gas:  { bar: document.getElementById('b-gas'),  val: document.getElementById('b-gas-v') },
    heat: { bar: document.getElementById('b-heat'), val: document.getElementById('b-heat-v') },
    fire: { bar: document.getElementById('b-fire'), val: document.getElementById('b-fire-v') },
  },
};

// ---------- device grid ----------
const DEVICE_MAP = [
  { key: 'exhaust_fan', label: 'Exhaust Fan' },
  { key: 'buzzer',      label: 'Buzzer' },
  { key: 'relay_power', label: 'Equipment Power', invertDisplay: true },
  { key: 'red_led',     label: 'Red LED' },
  { key: 'yellow_led',  label: 'Yellow LED' },
  { key: 'green_led',   label: 'Green LED' },
];

// ---------- helpers ----------
const fmt = (n, d = 1) =>
  (typeof n === 'number' && Number.isFinite(n)) ? n.toFixed(d) : '—';

function setConn(online, label) {
  els.conn.textContent = label || (online ? '● live' : '● offline');
  els.conn.classList.toggle('online', online);
  els.conn.classList.toggle('offline', !online);
}

function setText(el, txt) {
  if (el) el.textContent = txt;
}

function setBelief(set, value) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  set.bar.style.width = pct + '%';
  set.val.textContent = value.toFixed(2);
}

function renderDevices(actions) {
  if (!els.devices.dataset.built) {
    els.devices.innerHTML = DEVICE_MAP.map(d => `
      <div class="device" data-key="${d.key}">
        <span class="name">${d.label}</span>
        <span class="state off">—</span>
      </div>`).join('');
    els.devices.dataset.built = '1';
  }
  DEVICE_MAP.forEach(d => {
    const el = els.devices.querySelector(`[data-key="${d.key}"] .state`);
    if (!el) return;
    const raw = !!actions[d.key];
    el.classList.remove('on', 'off', 'danger');

    if (d.invertDisplay) {
      el.classList.add(raw ? 'on' : 'danger');
      el.textContent = raw ? 'POWERED' : 'CUT';
    } else {
      el.classList.add(raw ? 'on' : 'off');
      el.textContent = raw ? 'ON' : 'OFF';
    }
  });
}

function renderRules(rules) {
  if (!Array.isArray(rules) || rules.length === 0) {
    els.rules.innerHTML = '<span class="chip">none</span>';
    return;
  }
  els.rules.innerHTML = rules.map(r => `<span class="chip">${r}</span>`).join('');
}

function renderSensors(reading) {
  if (!reading) return;
  setText(els.sensors.temp,    fmt(reading.temp, 1) + ' °C');
  setText(els.sensors.hum,     fmt(reading.hum,  1) + ' %');
  setText(els.sensors.mq2,     reading.mq2     ?? '—');
  setText(els.sensors.mq135,   reading.mq135   ?? '—');
  setText(els.sensors.flame,   (reading.flame === 0 || reading.flame === false) ? 'DETECTED' : 'clear');
  setText(els.sensors.pir,     (reading.pir   === 1 || reading.pir   === true)  ? 'occupied' : 'vacant');
  setText(els.sensors.current, fmt(reading.current, 2) + ' A');
}

function render(payload) {
  const { level, hazard, confidence, risk_score, beliefs, rules_fired, actions, timestamp, reading } = payload;

  els.card.classList.remove('level-green', 'level-yellow', 'level-red');
  els.card.classList.add('level-' + String(level).toLowerCase());

  els.badge.textContent  = level;
  els.hazard.textContent = hazard;
  els.riskVal.textContent = Number(risk_score).toFixed(1);
  els.riskFill.style.width = Math.min(100, Number(risk_score)) + '%';
  els.confVal.textContent = Number(confidence).toFixed(2);
  els.tsVal.textContent   = timestamp
    ? new Date(Number(timestamp)).toLocaleTimeString()
    : new Date().toLocaleTimeString();

  setBelief(els.beliefs.safe, beliefs?.SAFE     ?? 0);
  setBelief(els.beliefs.gas,  beliefs?.GAS_LEAK ?? 0);
  setBelief(els.beliefs.heat, beliefs?.OVERHEAT ?? 0);
  setBelief(els.beliefs.fire, beliefs?.FIRE     ?? 0);

  renderRules(rules_fired);
  renderDevices(actions || {});
  renderSensors(reading);
}

function addEvent(payload) {
  const empty = els.tbody.querySelector('.empty-row');
  if (empty) empty.remove();

  const tr = document.createElement('tr');
  const time = new Date(Number(payload.timestamp || Date.now())).toLocaleTimeString();
  const actions = Object.entries(payload.actions || {})
    .map(([k, v]) => `${k}=${v ? 'ON' : 'OFF'}`).join(', ');

  tr.innerHTML = `
    <td>${time}</td>
    <td>${payload.device_id || 'esp32-01'}</td>
    <td>${payload.hazard}</td>
    <td>${Number(payload.risk_score).toFixed(1)}</td>
    <td><span class="band ${payload.level}">${payload.level}</span></td>
    <td>${actions}</td>`;
  els.tbody.prepend(tr);

  while (els.tbody.children.length > 25) {
    els.tbody.removeChild(els.tbody.lastChild);
  }
}

// ---------- MQTT ----------
setConn(false, '● connecting…');

const client = mqtt.connect(BROKER_URL, {
  username: MQTT_USER,
  password: MQTT_PASS,
  reconnectPeriod: 2000,
  clean: true,
  clientId: 'sentinel-dash-' + Math.random().toString(16).slice(2, 8),
});

client.on('connect', () => {
  console.log('[mqtt] connected');
  setConn(true, '● live');
  client.subscribe(TOPIC_TELEMETRY, { qos: 1 }, (err) => {
    if (err) console.error('[mqtt] subscribe failed', err);
    else      console.log('[mqtt] subscribed to', TOPIC_TELEMETRY);
  });
});

client.on('reconnect', () => setConn(false, '● reconnecting…'));
client.on('close',     () => setConn(false, '● offline'));
client.on('offline',   () => setConn(false, '● offline'));
client.on('error', (err) => {
  console.error('[mqtt] error', err);
  setConn(false, '● error');
});

client.on('message', (topic, payloadBuf) => {
  if (topic !== TOPIC_TELEMETRY) return;

  // ---- 1. decode ----
  let text;
  try {
    text = payloadBuf.toString();
  } catch (e) {
    return;
  }

  // ---- 2. drop empty / near-empty payloads ----
  // retained deletions arrive as zero-length or "{}" payloads
  if (!text || text.trim() === '' || text.trim() === '{}') {
    return;
  }

  // ---- 3. parse ----
  let payload;
  try {
    payload = JSON.parse(text);
  } catch (e) {
    console.warn('[mqtt] skipping non-JSON message:', text);
    return;
  }

  // ---- 4. require the fields we actually need ----
  const required = ['level', 'hazard', 'risk_score', 'beliefs', 'actions'];
  const missing = required.filter(k => payload[k] === undefined);
  if (missing.length) {
    console.warn('[mqtt] skipping malformed telemetry, missing:', missing, payload);
    return;
  }

  // ---- 5. render ----
  render(payload);
  addEvent(payload);
});

// ---------- placeholder until first message ----------
els.tbody.innerHTML = '<tr class="empty-row"><td colspan="6">Waiting for data on ' + TOPIC_TELEMETRY + '…</td></tr>';
