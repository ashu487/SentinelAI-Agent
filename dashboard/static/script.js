async function refresh() {
  const res = await fetch('/api/state');
  const data = await res.json();

  const devicesEl = document.getElementById('devices');
  devicesEl.innerHTML = '';
  for (const [deviceId, state] of Object.entries(data.devices)) {
    const d = state.decision;
    const card = document.createElement('div');
    card.className = `device-card band-${d.risk_band}`;
    card.innerHTML = `
      <strong>${deviceId}</strong><br>
      Hazard: ${d.primary_hazard} &nbsp; Risk: ${d.risk_score} &nbsp; Band: ${d.risk_band}<br>
      Actions: ${d.actions.join(', ')}<br>
      <small>gas=${state.reading.gas.toFixed(0)} smoke=${state.reading.smoke.toFixed(0)}
      temp=${state.reading.temp.toFixed(1)}°C flame=${state.reading.flame}</small>
    `;
    devicesEl.appendChild(card);
  }

  const tbody = document.querySelector('#events tbody');
  tbody.innerHTML = '';
  for (const ev of data.recent_events) {
    const row = document.createElement('tr');
    const time = new Date(ev.timestamp * 1000).toLocaleTimeString();
    row.innerHTML = `
      <td>${time}</td><td>${ev.device_id}</td><td>${ev.primary_hazard}</td>
      <td>${ev.risk_score}</td><td>${ev.risk_band}</td><td>${JSON.parse(ev.actions).join(', ')}</td>
    `;
    tbody.appendChild(row);
  }
}

setInterval(refresh, 2000);
refresh();
