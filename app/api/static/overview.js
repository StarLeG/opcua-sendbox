// «Обзор»: живые значения общего сервера (/ws/shared) и создание личной сессии.
import {
  Spark,
  bindCopyButtons,
  bindCreateButton,
  connectWs,
  el,
  escapeHtml,
  formatValue,
  renderMySessions,
} from "./common.js";

const tr = (s, vars) => window.t(s, vars);
const rows = new Map(); // тег → {value: td, spark: Spark}

function setStatus(status) {
  const pill = el("shared-status");
  const ok = status === "running";
  pill.className = "pill " + (ok ? "ok" : "warn");
  pill.textContent = ok ? tr("работает") : status === "restarting" ? tr("перезапускается") : tr("недоступен");
}

function renderValues(values) {
  const body = el("shared-tags");
  if (rows.size === 0) body.innerHTML = "";
  for (const [name, value] of Object.entries(values)) {
    let row = rows.get(name);
    if (!row) {
      const tr_ = document.createElement("tr");
      tr_.innerHTML = `<td class="mono">${escapeHtml(name)}</td><td class="num mono"></td><td><canvas class="spark"></canvas></td>`;
      body.appendChild(tr_);
      row = { value: tr_.children[1], spark: new Spark(tr_.querySelector("canvas")) };
      rows.set(name, row);
    }
    row.value.textContent = formatValue(value);
    row.spark.push(value);
    row.spark.draw();
  }
}

function renderAlarms(alarms) {
  el("shared-alarms").innerHTML = Object.entries(alarms)
    .map(
      ([name, active]) =>
        `<tr><td class="mono">${escapeHtml(name)}</td><td><span class="pill ${active ? "on" : "ok"}">${
          active ? tr("активен") : tr("норма")
        }</span></td></tr>`,
    )
    .join("");
}

// «48 ч 31 мин 16 с» — формат счётчика uptime (TASK-205).
function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h} ${tr("ч")} ${m} ${tr("мин")} ${s} ${tr("с")}`;
}

function renderCounters(f) {
  el("cnt-clients").textContent = String(f.clients);
  el("cnt-rps").textContent = f.requests_per_s.toFixed(2);
  el("cnt-eps").textContent = f.errors_per_s.toFixed(2);
  el("cnt-total").textContent = String(f.requests_total);
  el("cnt-errors").textContent = String(f.errors_total);
  el("cnt-uptime").textContent = formatUptime(f.uptime_s);
}

connectWs("/ws/shared", (frame) => {
  if (frame.type === "hello") setStatus(frame.shared.status);
  if (frame.type === "tag_values") {
    setStatus("running");
    renderValues(frame.values);
    renderAlarms(frame.alarms);
  }
  if (frame.type === "counters") renderCounters(frame);
  if (frame.type === "exited") setStatus("restarting");
});

bindCopyButtons();
bindCreateButton();
renderMySessions();
