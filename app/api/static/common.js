// Общие функции UI: ключ API, запросы, WebSocket с переподключением, мини-графики.
// ES-модуль без сборки (как в Modbus SendBox).

const KEY_STORAGE = "opcua-sendbox-key";
const tr = (s, vars) => (window.t ? window.t(s, vars) : s);

export function el(id) {
  return document.getElementById(id);
}

function storedKey() {
  try {
    return localStorage.getItem(KEY_STORAGE);
  } catch {
    return null; // приватный режим или запрет хранилища
  }
}

function storeKey(key) {
  try {
    localStorage.setItem(KEY_STORAGE, key);
  } catch {
    /* ключ проживёт до перезагрузки страницы */
  }
}

let memoryKey = null;

// Ключ выдаётся без регистрации; после перезапуска сервиса старый ключ
// неизвестен (401) — тогда молча берём новый.
export async function ensureKey(force = false) {
  if (!force) {
    const known = memoryKey || storedKey();
    if (known) return known;
  }
  const response = await fetch("/api/keys", { method: "POST" });
  if (!response.ok) throw new Error(await errorText(response));
  const { key } = await response.json();
  memoryKey = key;
  storeKey(key);
  return key;
}

export function currentKey() {
  return memoryKey || storedKey();
}

async function errorText(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    }
  } catch {
    /* не JSON */
  }
  return tr("Ошибка {status}", { status: response.status });
}

// Запрос с ключом; при 401 ключ перевыпускается один раз.
export async function api(method, url, body) {
  const send = (key) =>
    fetch(url, {
      method,
      headers: { "X-API-Key": key, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  let response = await send(await ensureKey());
  if (response.status === 401) response = await send(await ensureKey(true));
  return response;
}

// То же, но с разбором JSON; ошибка — исключение с текстом сервера.
export async function apiJson(method, url, body) {
  const response = await api(method, url, body);
  if (!response.ok) {
    const error = new Error(await errorText(response));
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

// WebSocket с переподключением (backoff до 15 с). Коды 1000/1008/1011 — сервер
// закрыл канал сознательно (сессия закрыта, нет доступа, авария): не переподключаемся.
export function connectWs(path, onFrame, onClose) {
  let delay = 1000;
  let stopped = false;
  function open() {
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + path);
    ws.onopen = () => {
      delay = 1000;
    };
    ws.onmessage = (event) => onFrame(JSON.parse(event.data));
    ws.onclose = (event) => {
      if (stopped) return;
      if ([1000, 1008, 1011].includes(event.code)) {
        if (onClose) onClose(event.code);
        return;
      }
      setTimeout(open, delay);
      delay = Math.min(delay * 2, 15000);
    };
  }
  open();
  return () => {
    stopped = true;
  };
}

export function formatValue(value) {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/\.?0+$/, "");
  }
  return String(value);
}

// Мини-график: последние 60 с значений тега (canvas, без библиотек).
export class Spark {
  constructor(canvas) {
    this.canvas = canvas;
    this.points = [];
  }

  push(value) {
    const now = Date.now();
    this.points.push({ t: now, v: typeof value === "boolean" ? Number(value) : value });
    while (this.points.length && now - this.points[0].t > 60000) this.points.shift();
  }

  draw() {
    const c = this.canvas;
    const ratio = window.devicePixelRatio || 1;
    const w = c.clientWidth || 120;
    const h = c.clientHeight || 26;
    if (c.width !== w * ratio) {
      c.width = w * ratio;
      c.height = h * ratio;
    }
    const ctx = c.getContext("2d");
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (this.points.length < 2) return;
    let lo = Infinity;
    let hi = -Infinity;
    for (const p of this.points) {
      lo = Math.min(lo, p.v);
      hi = Math.max(hi, p.v);
    }
    if (hi === lo) {
      hi += 1;
      lo -= 1;
    }
    const now = Date.now();
    ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent") || "#35d0b0";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    this.points.forEach((p, i) => {
      const x = w - ((now - p.t) / 60000) * w;
      const y = h - 2 - ((p.v - lo) / (hi - lo)) * (h - 4);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
}

export async function copyText(button, text) {
  try {
    await navigator.clipboard.writeText(text);
    const old = button.textContent;
    button.textContent = tr("Скопировано");
    setTimeout(() => {
      button.textContent = old;
    }, 1500);
  } catch {
    window.prompt(tr("Скопируйте адрес"), text);
  }
}

// Кнопки «Копировать» с готовым текстом (data-copy-text).
export function bindCopyButtons() {
  for (const button of document.querySelectorAll("[data-copy-text]")) {
    button.addEventListener("click", () => copyText(button, button.dataset.copyText));
  }
}

export function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}

// Создать личную сессию и перейти на её страницу (кнопка на «Обзоре» и «Моей сессии»).
export function bindCreateButton() {
  const button = el("create-session");
  if (!button) return;
  button.addEventListener("click", async () => {
    const status = el("create-status");
    button.disabled = true;
    status.className = "form-status";
    status.textContent = tr("Запускаем сервер…");
    try {
      const session = await apiJson("POST", "/api/sessions");
      location.href = session.web_ui_url + location.search;
    } catch (error) {
      status.className = "form-status err";
      status.textContent = error.message;
      button.disabled = false;
    }
  });
}

// Список сессий этого браузера (по ключу) со ссылками.
export async function renderMySessions() {
  const list = el("my-sessions");
  if (!list || !currentKey()) return;
  try {
    const sessions = await apiJson("GET", "/api/sessions");
    list.innerHTML = sessions
      .map((s) => `<li><a href="${s.web_ui_url}${location.search}">${escapeHtml(s.endpoint_url)}</a></li>`)
      .join("");
    if (sessions.length) list.insertAdjacentHTML("afterbegin", `<li>${escapeHtml(tr("Ваши сессии:"))}</li>`);
  } catch {
    /* ключ устарел — список пуст */
  }
}

export function formatLeft(isoTime) {
  if (!isoTime) return tr("не идёт: подключены клиенты");
  const seconds = Math.max(0, Math.round((Date.parse(isoTime) - Date.now()) / 1000));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h ? `${h} ${tr("ч")} ${m} ${tr("мин")}` : `${m} ${tr("мин")} ${String(s).padStart(2, "0")} ${tr("с")}`;
}
