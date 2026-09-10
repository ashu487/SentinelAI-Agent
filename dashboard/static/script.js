// ============================================================
// SentinelAI dashboard client
// Connects to /stream (SSE). Falls back to /events polling.
// ============================================================

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
  // beliefs
  bSafe:  { bar: document.getElementById('b-safe'),  val: document.getElementById('b-safe-v')  },
  bGas:   { bar: document.getElementById('b-gas'),   val: document.getElementById('b-gas-v')   },
  bHeat:  { bar: document.getElementById('b-heat'),  val: document.getElementById('b-heat-v')  },
  bFire:  { bar: document.getElementById('b-fire'),  val: document.getElementById('b-fire-v')  },
};

const DEVICE_MAP = [
  { key: 'exhaust_fan', label: 'Exhaust Fan',  dangerWhen: false },
  { key: 'buzzer',      label: 'Buzzer',       dangerWhen: false },
  { key: 'relay_power', label: 'Equipment',    dangerWhen: false, invert: true }, // OFF = cut power
  { key: 'red_led',     label: 'Red LED',      dangerWhen: false },
  { key: 'yellow_led',  label: 'Yellow LED',   dangerWhen: false },
  { key: 'green_led',   label: 'Green LED',    dangerWhen: false },
];

// ---------- helpers ----------
const fmt = (n, d = 2) => Number(n).toFixed(d);

function setBelief(set, value) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  set.bar.style.width = pct + '%';
  set.val.textContent = fmt(value);
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
    const raw = actions[d.key];
    const on = d.invert ? !raw : raw;   // for "Equipment" OFF = safe
    el.classList.remove('on','off','danger');
    if (d.invert) {
      // relay_power: ON means powered (safe), OFF means cut (danger state)
      el.classList.add(raw ? 'on' : 'danger');
      el.textContent = raw ? 'POWERED' : 'CUT';
    } else {
      el.classList.add(on ? 'on' : 'off');
      el.textContent = on ? 'ON' : 'OFF';
    }
  });
}

function renderRules(rules) {
  if (!rules || rules.length === 0) {
    els.rules.innerHTML = '<span class="chip">none</span>';
    return;
  }
  els.rules.innerHTML = rules.map(r => `<span class="chip">${r}</span>`).join('');
}

function renderEvent(row) {
  const tr = document.createElement('tr');
  const time = new Date(row.timestamp).toLocaleTimeString();
  const actions = Object.entries(row.actions || {})
    .map(([k, v]) => `${k}=${v ? 'ON' : 'OFF'}`)
    .join(', ');
  tr.innerHTML = `
    <td>${time}</td>
    <td>${row.device_id || 'esp32-01'}</td>
    <td>${row.hazard}</td>
    <td>${fmt(row.risk_score, 1)}</td>
    <td><span class="band ${row.level}">${row.level}</span></td>
    <td>${actions}</td>`;
  return tr;
}

// ---------- main render ----------
function render(payload) {
  const { level, hazard, confidence, risk_score, beliefs, rules_fired, actions, timestamp } = payload;

  // card theme
  els.card.classList.remove('level-green','level-yellow','level-red');
  els.card.classList.add('level-' + level.toLowerCase());

  els.badge.textContent  = level;
  els.hazard.textContent = hazard;
  els.riskVal.textContent = fmt(risk_score, 1);
  els.riskFill.style.width = Math.min(100, risk_score) + '%';
  els.confVal.textContent = fmt(confidence);
  els.tsVal.textContent   = new Date(timestamp).toLocaleTimeString();

  setBelief(els.bSafe, beliefs.SAFE     ?? 0);
  setBelief(els.bGas,  beliefs.GAS_LEAK ?? 0);
  setBelief(els.bHeat, beliefs.OVERHEAT ?? 0);
  setBelief(els.bFire, beliefs.FIRE     ?? 0);

  renderRules(rules_fired);
  renderDevices(actions || {});
}

function addEvent(payload) {
  // remove the empty placeholder if present
  const empty = els.tbody.querySelector('.empty-row');
  if (empty) empty.remove();

  const tr = renderEvent(payload);
  tr.style.opacity = 0;
  els.tbody.prepend(tr);
  requestAnimationFrame(() => tr.style.transition = 'opacity .4s', tr.style.opacity = 1);

  // cap at 25 rows
  while (els.tbody.children.length > 25) {
    els.tbody.removeChild(els.tbody.lastChild);
  }
}

function setConn(online) {
  els.conn.textContent = online ? '● live' : '● offline';
  els.conn.classList.toggle('online', online);
  els.conn.classList.toggle('offline', !online);
}

// ---------- bootstrap ----------
async function loadHistory() {
  try {
    const res = await fetch('/events?limit=25');
    const rows = await res.json();
    els.tbody.innerHTML = '';
    if (rows.length === 0) {
      els.tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No events yet.</td></tr>';
      return;
    }
    rows.reverse().forEach(r => els.tbody.appendChild(renderEvent(r)));
    render(rows[rows.length - 1]);
  } catch (e) {
    console.error('history load failed', e);
  }
}

function connectStream() {
  const es = new EventSource('/stream');
  es.onopen  = () => setConn(true);
  es.onerror = () => setConn(false);
  es.onmessage = (evt) => {
    const payload = JSON.parse(evt.data);
    render(payload);
    addEvent(payload);
  };
}

(async function init() {
  await loadHistory();
  connectStream();
})();
