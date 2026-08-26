const WS_URL = "ws://localhost:8765";
const statusEl = document.getElementById("conn-status");
const logEl = document.getElementById("log");

function connect() {
  const ws = new WebSocket(WS_URL);

  ws.onopen = () => { statusEl.textContent = "connected"; statusEl.style.color = "#3dff5a"; };
  ws.onclose = () => {
    statusEl.textContent = "disconnected — retrying...";
    statusEl.style.color = "#ff5c5c";
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.kind === "telemetry") render(data);
  };
}

function setLamp(id, on, caution) {
  const el = document.getElementById(id);
  el.classList.toggle("on", !!on);
  el.classList.toggle("caution", !!on && !!caution);
}

function pad(n, w) { return String(n).padStart(w, "0"); }

function secondsToGet(s) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return `${pad(h, 3)}:${pad(m, 2)}:${pad(sec, 2)}`;
}

function render(d) {
  document.getElementById("prog-val").textContent = (d.program || "").replace("P", "").padStart(2, "0");
  document.getElementById("verb-val").textContent = pad(d.verb, 2);
  document.getElementById("noun-val").textContent = pad(d.noun, 2);
  document.getElementById("r1-val").textContent = d.r1;
  document.getElementById("r2-val").textContent = d.r2;
  document.getElementById("r3-val").textContent = d.r3;
  document.getElementById("get-clock").textContent = secondsToGet(d.sim_get_seconds);

  setLamp("lamp-prog", d.prog_alarm, true);
  setLamp("lamp-restart", d.restart_lamp, true);
  setLamp("lamp-keyrel", d.key_rel, false);
  setLamp("lamp-stby", d.stby, false);
  // Lamps not modeled by the simulator (no source signal driving them yet):
  // UPLINK ACTY, TEMP, NO ATT, GIMBAL LOCK, OPR ERR, TRACKER, ALT, VEL.
  document.getElementById("comp-acty").classList.toggle("on", Math.floor(d.sim_get_seconds * 2) % 2 === 0);

  const pct = Math.min(100, Math.round((d.core_sets_used / d.max_core_sets) * 100));
  document.getElementById("core-fill").style.width = pct + "%";
  document.getElementById("core-count").textContent = d.core_sets_used;
  document.getElementById("flag-radar-val").textContent = d.radar_auto_slew ? "ON (flooding executive)" : "OFF";
  document.getElementById("flag-radar-val").style.color = d.radar_auto_slew ? "#ff5c5c" : "#3dff5a";

  (d.events || []).forEach(e => {
    const div = document.createElement("div");
    div.className = e.event_type;
    const tag = e.code ? ` [${e.code}]` : "";
    div.textContent = `${e.mission_get}  ${e.event_type.toUpperCase()}${tag}  ${e.note}`;
    logEl.prepend(div);
  });
}

connect();
