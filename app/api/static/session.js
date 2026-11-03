// «Моя сессия»: endpoint, теги с живыми значениями, алармы, формы, журнал.
import {
  Spark,
  apiJson,
  bindCreateButton,
  connectWs,
  copyText,
  currentKey,
  el,
  ensureKey,
  escapeHtml,
  formatLeft,
  formatValue,
  renderMySessions,
} from "./common.js";

const tr = (s, vars) => window.t(s, vars);
const CFG = window.SENDBOX;
const SID = window.SESSION_ID;
const base = SID ? `/api/sessions/${encodeURIComponent(SID)}` : null;

let session = null;
let tags = [];
let alarms = [];
const tagRows = new Map(); // имя → {value, spark}
let alarmStates = {};
let stopWs = null;

// --- журнал ---------------------------------------------------------------

function log(text, kind = "") {
  const list = el("log");
  const item = document.createElement("li");
  item.className = kind;
  const time = new Date().toLocaleTimeString();
  item.innerHTML = `<time>${time}</time>${escapeHtml(text)}`;
  list.prepend(item);
  while (list.children.length > 200) list.lastChild.remove();
}

// --- отрисовка --------------------------------------------------------------

function renderHeader() {
  el("endpoint").textContent = session.endpoint_url;
  el("state").textContent = session.state;
  el("clients").textContent = String(session.clients);
}

function tickCountdown() {
  if (!session) return;
  el("idle-left").textContent = session.clients > 0 ? tr("не идёт: подключены клиенты") : formatLeft(session.idle_closes_at);
  el("life-left").textContent = formatLeft(session.expires_at);
}

function generatorLabel(kind) {
  return (CFG.generators[kind] && CFG.generators[kind].label) || kind;
}

function renderTags(values = {}) {
  const body = el("tags");
  body.innerHTML = "";
  tagRows.clear();
  for (const tag of tags) {
    const row = document.createElement("tr");
    row.innerHTML =
      `<td class="mono">${escapeHtml(tag.name)}${tag.writable ? ` <span class="pill ok">${tr("запись")}</span>` : ""}</td>` +
      `<td class="mono">${tag.data_type}</td><td>${escapeHtml(generatorLabel(tag.kind))}</td>` +
      `<td class="num mono"></td><td><canvas class="spark"></canvas></td>` +
      `<td class="act"><button type="button" class="danger" data-remove-tag="${escapeHtml(tag.name)}">${tr("Удалить")}</button></td>`;
    body.appendChild(row);
    const entry = { value: row.children[3], spark: new Spark(row.querySelector("canvas")) };
    tagRows.set(tag.name, entry);
    if (tag.name in values) entry.value.textContent = formatValue(values[tag.name]);
  }
  fillAlarmTagSelect();
}

function alarmCondition(alarm) {
  if (alarm.kind === "limit") {
    const parts = [];
    if (alarm.high !== null && alarm.high !== undefined) parts.push(`> ${alarm.high}`);
    if (alarm.low !== null && alarm.low !== undefined) parts.push(`< ${alarm.low}`);
    return `${alarm.tag} ${parts.join(tr(" или "))}`;
  }
  if (alarm.kind === "periodic") return tr("каждые {s} с", { s: alarm.period_s });
  return tr("вручную");
}

function renderAlarms() {
  el("alarms").innerHTML = alarms
    .map((alarm) => {
      const active = Boolean(alarmStates[alarm.name]);
      const name = escapeHtml(alarm.name);
      return (
        `<tr><td class="mono">${name}</td><td class="mono">${escapeHtml(alarmCondition(alarm))}</td>` +
        `<td><span class="pill ${active ? "on" : "ok"}">${active ? tr("активен") : tr("норма")}</span></td>` +
        `<td class="act"><button type="button" class="ghost" data-trigger="${name}" data-active="true">${tr("Активировать")}</button>` +
        `<button type="button" class="ghost" data-trigger="${name}" data-active="false">${tr("Сбросить")}</button>` +
        `<button type="button" class="danger" data-remove-alarm="${name}">${tr("Удалить")}</button></td></tr>`
      );
    })
    .join("");
}

function applyValues(values) {
  for (const [name, value] of Object.entries(values)) {
    const row = tagRows.get(name);
    if (!row) continue;
    row.value.textContent = formatValue(value);
    row.spark.push(value);
    row.spark.draw();
  }
}

// --- данные -------------------------------------------------------------------

async function reload() {
  const detail = await apiJson("GET", base);
  session = detail;
  tags = detail.tags;
  alarms = detail.alarms;
  alarmStates = detail.alarm_states;
  renderHeader();
  renderTags(detail.values);
  renderAlarms();
  tickCountdown();
}

function onFrame(frame) {
  switch (frame.type) {
    case "hello":
      session = { ...session, ...frame.session };
      renderHeader();
      break;
    case "tag_values":
      applyValues(frame.values);
      alarmStates = frame.alarms;
      renderAlarms();
      break;
    case "alarm":
      alarmStates[frame.alarm] = frame.active;
      renderAlarms();
      // у сообщений сервера по умолчанию имя уже в начале; у своих текстов — добавляем
      log(frame.message.startsWith(frame.alarm) ? frame.message : `${frame.alarm}: ${frame.message}`, frame.active ? "alarm" : "info");
      break;
    case "write":
      log(tr("Клиент записал {tag} = {value}", { tag: frame.tag, value: formatValue(frame.value) }), "write");
      break;
    case "client_count":
      if (session.clients !== frame.clients) {
        log(tr("OPC UA клиентов: {n}", { n: frame.clients }), "info");
      }
      session.clients = frame.clients;
      // время простоя пересчитает сервер; подтянем свежие сроки
      apiJson("GET", base).then((d) => {
        session = { ...session, ...d };
        tickCountdown();
      }).catch(() => {});
      renderHeader();
      break;
    case "log":
      log(`${frame.level}: ${frame.message}`);
      break;
    case "crashed":
      log(tr("Сервер сессии упал. Создайте новую сессию."), "alarm");
      break;
    case "closed":
      log(tr("Сессия закрыта ({reason}).", { reason: frame.reason }), "info");
      break;
  }
}

function onWsClosed(code) {
  session.state = code === 1011 ? "crashed" : "closed";
  renderHeader();
  for (const button of document.querySelectorAll("#session-view button, #session-view input")) button.disabled = true;
  showMissing(tr("Сессия закрыта. Можно создать новую."));
}

function showMissing(text) {
  el("session-missing").hidden = false;
  if (text) el("missing-text").textContent = text;
  bindCreateButton();
  renderMySessions();
}

// --- формы ------------------------------------------------------------------------

function fillTagForm() {
  const form = el("tag-form");
  form.data_type.innerHTML = CFG.dataTypes.map((t) => `<option>${t}</option>`).join("");
  form.kind.innerHTML = Object.entries(CFG.generators)
    .map(([kind, g]) => `<option value="${kind}">${escapeHtml(g.label)}</option>`)
    .join("");
  form.update_period_s.min = CFG.period.min;
  form.update_period_s.max = CFG.period.max;
  form.kind.value = "sine";
  form.kind.addEventListener("change", renderParams);
  form.data_type.addEventListener("change", syncKindToType);
  renderParams();
}

function syncKindToType() {
  const form = el("tag-form");
  const boolean = form.data_type.value === "Boolean";
  const isBit = CFG.bitKinds.includes(form.kind.value);
  if (form.kind.value !== "constant" && boolean !== isBit) {
    form.kind.value = boolean ? "toggle" : "sine";
    renderParams();
  }
}

function renderParams() {
  const form = el("tag-form");
  const generator = CFG.generators[form.kind.value];
  el("tag-params").innerHTML = generator.params
    .map((p) =>
      typeof p.default === "boolean"
        ? `<label class="check"><input type="checkbox" data-param="${p.name}" ${p.default ? "checked" : ""}> ${escapeHtml(p.label)}</label>`
        : `<label>${escapeHtml(p.label)}<input data-param="${p.name}" value="${escapeHtml(p.default)}"></label>`,
    )
    .join("");
  form.writable.disabled = form.kind.value !== "constant";
  if (form.writable.disabled) form.writable.checked = false;
}

function collectParams() {
  const params = {};
  for (const input of el("tag-params").querySelectorAll("[data-param]")) {
    const name = input.dataset.param;
    if (input.type === "checkbox") {
      params[name] = input.checked;
    } else if (name === "values") {
      params[name] = input.value.split(",").map((s) => Number(s.trim())).filter((n) => !Number.isNaN(n));
    } else if (name === "points") {
      // «время:значение, время:value» → [[t, v], ...]; остальное отклонит сервер
      params[name] = input.value
        .split(",")
        .map((s) => s.split(":").map((p) => Number(p.trim())))
        .filter((pair) => pair.length === 2 && pair.every((n) => !Number.isNaN(n)));
    } else if (name === "then" && !["hold", "loop"].includes(input.value.trim())) {
      params[name] = Number(input.value);
    } else if (name === "then") {
      params[name] = input.value.trim();
    } else if (input.value.trim() !== "") {
      params[name] = Number(input.value);
    }
  }
  return params;
}

function setStatus(id, text, ok) {
  const status = el(id);
  status.className = "form-status " + (ok ? "ok" : "err");
  status.textContent = text;
}

async function submitTag(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const spec = {
    name: form.name.value.trim(),
    data_type: form.data_type.value,
    kind: form.kind.value,
    params: collectParams(),
    update_period_s: Number(form.update_period_s.value),
    writable: form.writable.checked,
  };
  try {
    await apiJson("POST", `${base}/tags`, spec);
    setStatus("tag-status", tr("Тег {name} добавлен: ns=2;s=Tags.{name}", { name: spec.name }), true);
    form.name.value = "";
    await reload();
  } catch (error) {
    setStatus("tag-status", error.message, false);
  }
}

function fillAlarmTagSelect() {
  const select = el("alarm-form").tag;
  const numeric = tags.filter((t) => t.data_type !== "Boolean");
  select.innerHTML = numeric.map((t) => `<option>${escapeHtml(t.name)}</option>`).join("");
}

function syncAlarmKind() {
  const form = el("alarm-form");
  for (const label of form.querySelectorAll("[data-for]")) {
    label.hidden = label.dataset.for !== form.kind.value;
  }
}

async function submitAlarm(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const kind = form.kind.value;
  const spec = { name: form.name.value.trim(), kind, severity: Number(form.severity.value), message: form.message.value };
  if (kind === "limit") {
    spec.tag = form.tag.value;
    if (form.high.value !== "") spec.high = Number(form.high.value);
    if (form.low.value !== "") spec.low = Number(form.low.value);
    spec.deadband = Number(form.deadband.value || 0);
  }
  if (kind === "periodic") spec.period_s = Number(form.period_s.value);
  try {
    await apiJson("POST", `${base}/alarms`, spec);
    setStatus("alarm-status", tr("Аларм {name} добавлен", { name: spec.name }), true);
    form.name.value = "";
    await reload();
  } catch (error) {
    setStatus("alarm-status", error.message, false);
  }
}

// --- действия в таблицах ----------------------------------------------------------------

async function onTableClick(event) {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.removeTag) {
      await apiJson("DELETE", `${base}/tags/${encodeURIComponent(button.dataset.removeTag)}`);
      await reload();
    } else if (button.dataset.removeAlarm) {
      await apiJson("DELETE", `${base}/alarms/${encodeURIComponent(button.dataset.removeAlarm)}`);
      await reload();
    } else if (button.dataset.trigger) {
      await apiJson("POST", `${base}/alarms/${encodeURIComponent(button.dataset.trigger)}/trigger`, {
        active: button.dataset.active === "true",
      });
    }
  } catch (error) {
    log(error.message, "alarm");
  }
}

async function deleteSession() {
  if (!window.confirm(tr("Закрыть сессию? Сервер остановится, клиенты отключатся."))) return;
  try {
    await apiJson("DELETE", base);
  } catch (error) {
    log(error.message, "alarm");
  }
}

// --- старт ----------------------------------------------------------------------------

async function start() {
  if (!SID) {
    // /session без id: первая сессия ключа или кнопка «Создать».
    if (currentKey()) {
      try {
        const sessions = await apiJson("GET", "/api/sessions");
        if (sessions.length) {
          location.replace(sessions[0].web_ui_url + location.search);
          return;
        }
      } catch {
        /* ключ устарел */
      }
    }
    showMissing(tr("Личного сервера пока нет. Создайте его — это займёт пару секунд."));
    return;
  }
  try {
    await ensureKey();
    await reload();
  } catch (error) {
    showMissing(error.status === 404 ? null : error.message);
    return;
  }
  el("session-view").hidden = false;
  el("copy-endpoint").addEventListener("click", (e) => copyText(e.currentTarget, session.endpoint_url));
  el("delete-session").addEventListener("click", deleteSession);
  el("tags").addEventListener("click", onTableClick);
  el("alarms").addEventListener("click", onTableClick);
  el("tag-form").addEventListener("submit", submitTag);
  el("alarm-form").addEventListener("submit", submitAlarm);
  el("alarm-form").kind.addEventListener("change", syncAlarmKind);
  fillTagForm();
  syncAlarmKind();
  setInterval(tickCountdown, 1000);
  log(tr("Сервер готов: подключите клиент к {url}", { url: session.endpoint_url }), "info");
  stopWs = connectWs(`/ws/sessions/${encodeURIComponent(SID)}?key=${encodeURIComponent(currentKey())}`, onFrame, onWsClosed);
}

window.addEventListener("beforeunload", () => stopWs && stopWs());
start();
