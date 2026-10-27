// ============================================================
// SentinelAI dashboard — MQTT over WebSocket
// Replaces the old Flask SSE + /events version.
// ============================================================

const BROKER_URL = "ws://172.20.10.10:9001";
const MQTT_USER  = "sentinel";
const MQTT_PASS  = "289961";

const TOPIC_TELEMETRY = "sentinel/sentinel/telemetry";
const TOPIC_AGENT     = "sentinel/sentinel/agent";

// ---- DOM ----
const $ = (id) => document.getElementById(id);

const connEl     = $("conn-status");
const liveCard   = $("live-card");
const levelBadge = $("level-badge");
const hazardEl   = $("hazard");
const riskFill   = $("risk-fill");
const riskValue  = $("risk-value");
const confValue  = $("conf-value");
const tsValue    = $("ts-value");
const rulesList  = $("rules-list");

const bSafe = $("b-safe"), bGas = $("b-gas"), bHeat = $("b-heat"), bFire = $("b-fire");
const bSafeV = $("b-safe-v"), bGasV = $("b-gas-v"), bHeatV = $("b-heat-v"), bFireV = $("b-fire-v");

const sTemp    = $("s-temp");
const sHum     = $("s-hum");
const sMq2     = $("s-mq2");
const sMq135   = $("s-mq135");
const sFlame   = $("s-flame");
const sPir     = $("s-pir");
const sCurrent = $("s-current");

const devicesEl = $("devices");
const eventsTbody = document.querySelector("#events tbody");

// ============================================================
// Rendering helpers
// ============================================================
function setConn(online) {
  connEl.className = "conn " + (online ? "online" : "offline");
  connEl.textContent = online ? "● online" : "● offline";
}

function setLevel(level) {
  liveCard.classList.remove("level-green", "level-yellow", "level-red");
  liveCard.classList.add("level-" + String(level || "green").toLowerCase());

  const cls = String(level || "GREEN").toUpperCase();
  levelBadge.textContent = cls;
  // badge background/text come from CSS, driven by the parent's level-* class
}

function setRisk(score) {
  const pct = Math.max(0, Math.min(100, Number(score) || 0));
  riskFill.style.width = pct + "%";
  riskValue.textContent = pct.toFixed(1);
}

function setConfidence(conf) {
  confValue.textContent = (Number(conf) || 0).toFixed(2);
}

function setTimestamp(ms) {
  if (!ms) { tsValue.textContent = "—"; return; }
  tsValue.textContent = new Date(ms).toLocaleTimeString();
}

function setBeliefs(beliefs) {
  if (!beliefs) return;
  const map = [
    [bSafe,  bSafeV,  beliefs.SAFE,     "SAFE"],
    [bGas,   bGasV,   beliefs.GAS_LEAK, "GAS"],
    [bHeat,  bHeatV,  beliefs.OVERHEAT, "HEAT"],
    [bFire,  bFireV,  beliefs.FIRE,     "FIRE"],
  ];
  for (const [bar, valEl, v, _name] of map) {
    const pct = Math.max(0, Math.min(1, Number(v) || 0));
    bar.style.width = (pct * 100) + "%";
    valEl.textContent = pct.toFixed(2);
  }
}

function setRules(rules) {
  rulesList.innerHTML = "";
  if (!rules || !rules.length) {
    rulesList.textContent = "—";
    return;
  }
  for (const r of rules) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = r;
    rulesList.appendChild(chip);
  }
}

function setDevices(actions) {
  // Fixed set of four devices, matching the actuators your firmware supports.
  const defs = [
    { name: "Exhaust fan", key: "exhaust_fan",  onIsDanger: false },
    { name: "Buzzer",      key: "buzzer",       onIsDanger: true  },
    { name: "Sprinkler",   key: "sprinkler",    onIsDanger: true  },
    { name: "Load power",  key: "relay_power",  onIsDanger: false, invertLabel: true },
  ];

  devicesEl.innerHTML = "";
  for (const d of defs) {
    const on = !!actions?.[d.key];
    const el = document.createElement("div");
    el.className = "device";
    const label = d.invertLabel
      ? (on ? "ON" : "OFF")      // relay_power true = ON
      : (on ? "ON" : "OFF");
    let stateClass = on ? "on" : "off";
    if (on && d.onIsDanger) stateClass = "danger";
    el.innerHTML = `
      <span class="name">${d.name}</span>
      <span class="state ${stateClass}">${label}</span>
    `;
    devicesEl.appendChild(el);
  }
}

function fmt(v, digits) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!isFinite(n)) return String(v);
  return digits !== undefined ? n.toFixed(digits) : String(n);
}

function setSensors(d) {
  sTemp.textContent    = fmt(d.temperature, 1) + " °C";
  sHum.textContent     = fmt(d.humidity, 0) + " %";
  sMq2.textContent     = fmt(d.mq2, 0);
  sMq135.textContent   = fmt(d.mq135, 0);
  sFlame.textContent   = fmt(d.flame, 0);
  sPir.textContent     = d.motion ? "YES" : "no";
  sCurrent.textContent = fmt(d.current, 2) + " A";
}

// ============================================================
// Event log (replaces the /events endpoint)
// ============================================================
function pushEvent(evt) {
  const row = document.createElement("tr");
  row.innerHTML = `
    <td>${new Date(evt.timestamp).toLocaleTimeString()}</td>
    <td>${evt.device_id || "sentinel"}</td>
    <td>${evt.hazard || "—"}</td>
    <td>${fmt(evt.risk_score, 1)}</td>
    <td><span class="band ${evt.level}">${evt.level}</span></td>
    <td>${(evt.rules_fired || []).join(", ") || "—"}</td>
  `;
  eventsTbody.prepend(row);
  while (eventsTbody.children.length > 25) {
    eventsTbody.removeChild(eventsTbody.lastChild);
  }
}

// Log only on transitions to a non-GREEN state, so the table doesn't spam.
let lastLoggedLevel = "GREEN";
function maybeLog(agentPayload) {
  const lvl = agentPayload.level || "GREEN";
  if (lvl !== "GREEN" && lvl !== lastLoggedLevel) {
    pushEvent(agentPayload);
  }
  lastLoggedLevel = lvl;
}

// ============================================================
// MQTT
// ============================================================
setConn(false);

const client = mqtt.connect(BROKER_URL, {
  username: MQTT_USER,
  password: MQTT_PASS,
  clientId: "sentinel-dash-" + Math.random().toString(16).slice(2, 8),
  reconnectPeriod: 3000,
  connectTimeout: 5000,
});

client.on("connect", () => {
  console.log("[dashboard] connected to", BROKER_URL);
  setConn(true);
  client.subscribe(TOPIC_TELEMETRY, { qos: 0 });
  client.subscribe(TOPIC_AGENT,     { qos: 0 });
});

client.on("reconnect", () => setConn(false));
client.on("close",     () => setConn(false));
client.on("error", (e) => {
  console.error("[dashboard] MQTT error:", e);
  setConn(false);
});

client.on("message", (topic, payloadBuf) => {
  let d;
  try {
    d = JSON.parse(payloadBuf.toString());
  } catch (e) {
    console.warn("[dashboard] bad JSON on", topic);
    return;
  }

  if (topic === TOPIC_TELEMETRY) {
    setSensors(d);
    return;
  }

  if (topic === TOPIC_AGENT) {
    setLevel(d.level);
    hazardEl.textContent = d.hazard || "—";
    setRisk(d.risk_score);
    setConfidence(d.confidence);
    setTimestamp(d.timestamp);
    setBeliefs(d.beliefs);
    setRules(d.rules_fired);
    setDevices(d.actions);
    maybeLog(d);
  }
});
