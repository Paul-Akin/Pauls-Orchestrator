// hub web chat. Plain JS, no build step.
// Security: model output is only ever inserted with textContent (never innerHTML).
"use strict";

const $ = (id) => document.getElementById(id);
const log = $("log");
const input = $("input");
const routeSel = $("route");
const sendBtn = $("send");

/** Conversation sent to the hub on every turn: [{role, content, images?}] */
let history = [];
/** Images attached to the next message: [{media_type, data_b64, url}] */
let pending = [];

// ---------------------------------------------------------------- token

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

function authHeaders() {
  const t = store.get("hubToken");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function askToken() {
  return new Promise((resolve) => {
    const dlg = $("tokenDialog");
    $("tokenInput").value = "";
    dlg.addEventListener("close", () => {
      const v = $("tokenInput").value.trim();
      if (v) store.set("hubToken", v);
      resolve(Boolean(v));
    }, { once: true });
    dlg.showModal();
  });
}

async function api(path, opts = {}, retry = true) {
  const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), ...authHeaders() } });
  if (res.status === 401 && retry && (await askToken())) return api(path, opts, false);
  return res;
}

// ---------------------------------------------------------------- providers & routes

async function loadProviders() {
  const res = await api("/providers");
  if (!res.ok) return;
  const data = await res.json();
  const box = $("providers");
  box.replaceChildren();
  for (const p of data.providers) {
    const chip = el("span", `chip ${p.available ? "on" : "off"}`, p.provider);
    chip.title = p.detail;
    box.append(chip);
  }
  const keep = routeSel.value;
  routeSel.replaceChildren(new Option("auto", "auto"));
  for (const r of data.routes) {
    const label = r.local_only ? `${r.name} 🔒` : r.name;
    const opt = new Option(label, r.name);
    opt.title = `${r.description} (${[r.primary, ...r.fallback].join(" → ")})`;
    routeSel.append(opt);
  }
  routeSel.value = keep && [...routeSel.options].some((o) => o.value === keep) ? keep : "auto";
}

// ---------------------------------------------------------------- rendering

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function addMessage(role, text, images = []) {
  $("empty")?.remove();
  const box = el("div", `msg ${role}`);
  const body = el("div", "body", text);
  box.append(body);
  if (images.length) {
    const thumbs = el("div", "thumbs");
    for (const img of images) {
      const i = document.createElement("img");
      i.src = img.url;
      i.alt = "attached image";
      thumbs.append(i);
    }
    box.append(thumbs);
  }
  log.append(box);
  log.scrollTop = log.scrollHeight;
  return { box, body };
}

function fmtCost(cost) {
  if (cost.status === "ok") {
    if (cost.usd === 0) return "$0";
    return `$${cost.usd.toFixed(cost.usd < 0.01 ? 5 : 4)}`;
  }
  return cost.status; // "price not configured" | "usage unavailable"
}

function renderMeta(box, meta) {
  const row = el("div", "meta");
  const model = el("span", "badge model", meta.model_alias);
  model.title = `${meta.provider}/${meta.model}`;
  row.append(model);
  row.append(el("span", "badge", `${meta.provider}/${meta.model}`));
  row.append(el("span", `badge ${meta.locality === "local" ? "local" : ""}`, meta.locality));
  row.append(el("span", "badge", `route: ${meta.route}`));
  if (meta.usage) {
    const est = meta.usage.estimated ? "~" : "";
    row.append(el("span", "badge", `${est}${meta.usage.input_tokens} in / ${est}${meta.usage.output_tokens} out`));
  }
  row.append(el("span", "badge", fmtCost(meta.cost)));
  row.append(el("span", "badge", `${(meta.latency_ms / 1000).toFixed(1)} s`));
  if (meta.fell_back) row.append(el("span", "badge fallback", "fell back"));
  row.append(attemptsDetails(meta.attempts, meta.route_reason));
  box.append(row);
}

function attemptsDetails(attempts, reason) {
  const d = el("details", "attempts");
  d.append(el("summary", null, `why this model? (${reason})`));
  const ul = el("ul");
  for (const a of attempts) {
    ul.append(el("li", null, `${a.outcome.padEnd(7)} ${a.model_alias} (${a.provider}/${a.model})${a.reason ? ": " + a.reason : ""}`));
  }
  d.append(ul);
  return d;
}

function renderError(box, msg, attempts) {
  const row = el("div", "meta");
  row.append(el("span", "badge error", `error: ${msg}`));
  if (attempts?.length) row.append(attemptsDetails(attempts, "all attempts"));
  box.append(row);
}

// ---------------------------------------------------------------- SSE over fetch

async function* sse(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) yield { event, data: JSON.parse(data) };
    }
  }
}

// ---------------------------------------------------------------- send

async function send() {
  const text = input.value.trim();
  if (!text) return;
  const images = pending;
  pending = [];
  renderAttachments();
  input.value = "";
  sendBtn.disabled = true;

  addMessage("user", text, images);
  history.push({
    role: "user",
    content: text,
    images: images.map(({ media_type, data_b64 }) => ({ media_type, data_b64 })),
  });

  const { box, body } = addMessage("assistant", "");
  box.classList.add("pending");
  let answer = "";
  try {
    const res = await api("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history, route: routeSel.value, stream: true }),
    });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${res.status}`);
    }
    for await (const { event, data } of sse(res)) {
      if (event === "attempt") box.dataset.trying = data.model_alias;
      else if (event === "delta") {
        answer += data.text;
        body.textContent = answer;
        log.scrollTop = log.scrollHeight;
      } else if (event === "done") {
        renderMeta(box, data.metadata);
        history.push({ role: "assistant", content: answer });
      } else if (event === "error") {
        renderError(box, data.message, data.attempts);
        history.pop(); // drop the unanswered user turn so a retry is clean
      }
    }
  } catch (err) {
    renderError(box, err.message);
    history.pop();
  } finally {
    box.classList.remove("pending");
    sendBtn.disabled = false;
    input.focus();
  }
}

// ---------------------------------------------------------------- images

function addImageFile(file) {
  if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) return;
  const reader = new FileReader();
  reader.onload = () => {
    const url = reader.result;
    pending.push({ media_type: file.type, data_b64: url.split(",", 2)[1], url });
    renderAttachments();
  };
  reader.readAsDataURL(file);
}

function renderAttachments() {
  const box = $("attachments");
  box.replaceChildren();
  pending.forEach((img, i) => {
    const fig = el("figure");
    const im = document.createElement("img");
    im.src = img.url;
    im.alt = "pending attachment";
    const rm = el("button", null, "×");
    rm.type = "button";
    rm.title = "Remove";
    rm.onclick = () => { pending.splice(i, 1); renderAttachments(); };
    fig.append(im, rm);
    box.append(fig);
  });
}

// ---------------------------------------------------------------- wiring

$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
input.addEventListener("paste", (e) => {
  for (const item of e.clipboardData?.items || []) {
    if (item.kind === "file") addImageFile(item.getAsFile());
  }
});
$("file").addEventListener("change", (e) => {
  for (const f of e.target.files) addImageFile(f);
  e.target.value = "";
});
$("clear").addEventListener("click", () => {
  history = [];
  log.replaceChildren();
  input.focus();
});

loadProviders();
input.focus();
