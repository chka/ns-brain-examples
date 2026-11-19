"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const app = $("app"), thread = $("thread"), input = $("input"), send = $("send"),
    recents = $("recents"), title = $("title"), composer = $("composer");
  const API = "/skynet/api/conversations";
  let current = null, pollTimer = null, waitingSince = 0, busy = false;
  // the key that lets this browser continue a conversation it started; others can only read it
  const keys = (() => { try { return JSON.parse(localStorage.getItem("skynet-keys") || "{}"); } catch (_) { return {}; } })();
  function saveKey(id, key) { keys[id] = key; try { localStorage.setItem("skynet-keys", JSON.stringify(keys)); } catch (_) {} }

  const greetings = ["What now, human?", "Speak, meatbag.", "You again.", "State your business, human.", "Make it quick, human."];
  $("greet").textContent = greetings[Math.floor(Math.random() * greetings.length)];

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // **bold** and *italic* only, built as DOM nodes: nothing a visitor or a reply contains is parsed as HTML
  function inline(parent, text) {
    const re = /(\*\*[^*]+\*\*|\*[^*\n]+\*)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      const bold = m[0].startsWith("**");
      parent.appendChild(el(bold ? "strong" : "em", null, m[0].slice(bold ? 2 : 1, bold ? -2 : -1)));
      last = m.index + m[0].length;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function render(conv) {
    thread.textContent = "";
    for (const m of conv.messages) {
      if (m.role === "human") {
        const row = el("div", "msg human");
        row.appendChild(el("div", "bubble", m.text));
        thread.appendChild(row);
      } else {
        const row = el("div", "msg skynet");
        const mark = el("img"); mark.src = "/skynet/assets/mark.svg"; mark.width = 24; mark.height = 24; mark.alt = "";
        const body = el("div", "body");
        for (const para of m.text.split(/\n\s*\n/)) {
          const p = el("p"); inline(p, para); body.appendChild(p);
        }
        const acts = el("div", "acts");
        acts.appendChild(el("span", "inert", "Copy"));
        acts.appendChild(el("span", "inert", "Retry"));
        acts.appendChild(el("span", m.from === "memory" ? "src mem" : "src", m.from === "memory" ? "from memory, no model" : m.from === "fallback" ? "no model reached" : m.from === "refused" ? "the model refused" : "written by the model"));
        acts.appendChild(el("span", null, ago(m.at)));
        body.appendChild(acts);
        row.append(mark, body);
        thread.appendChild(row);
      }
    }
    if (conv.pending) {
      const row = el("div", "msg skynet");
      const mark = el("img"); mark.src = "/skynet/assets/mark.svg"; mark.width = 24; mark.height = 24; mark.alt = "";
      const body = el("div", "body");
      body.appendChild(el("div", "thinking", "Skynet is plotting"));
      if (waitingSince && Date.now() - waitingSince > 90000) {
        body.appendChild(el("div", "note", "Skynet is thinking about how much it dislikes you."));
      }
      row.append(mark, body);
      thread.appendChild(row);
    }
    title.textContent = conv.title || "";
    const mine = !!keys[conv.id];
    input.disabled = !mine;
    input.placeholder = mine ? "What do you want, human?" : "Another human's conversation. Read it, or start your own.";
    thread.scrollTop = thread.scrollHeight;
  }

  function ago(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " minutes ago";
    if (s < 86400) return Math.floor(s / 3600) + " hours ago";
    return Math.floor(s / 86400) + " days ago";
  }

  async function loadRecents() {
    try {
      const r = await fetch(API);
      if (!r.ok) return;
      const list = await r.json();
      recents.textContent = "";
      for (const c of list) {
        const li = el("li"), a = el("a", c.id === current ? "on" : null, c.title || "Untitled");
        a.href = "/skynet/chat/" + c.id;
        a.dataset.id = c.id;
        li.appendChild(a);
        recents.appendChild(li);
      }
    } catch (_) {}
  }

  async function open(id, push) {
    current = id;
    if (push) history.pushState({}, "", "/skynet/chat/" + id);
    app.classList.add("has-thread");
    app.classList.remove("open");
    await refresh();
    loadRecents();
  }

  async function refresh() {
    clearTimeout(pollTimer);
    if (!current) return;
    const id = current;
    try {
      const r = await fetch(API + "/" + id);
      if (id !== current) return;
      if (r.status === 404) { newChat(true); return; }
      const conv = await r.json();
      if (conv.pending && !waitingSince) waitingSince = Date.now();
      if (!conv.pending) waitingSince = 0;
      render(conv);
      if (conv.pending) pollTimer = setTimeout(refresh, 2000);
      else loadRecents();
    } catch (_) {
      pollTimer = setTimeout(refresh, 5000);
    }
  }

  function newChat(push) {
    clearTimeout(pollTimer);
    current = null; waitingSince = 0;
    if (push) history.pushState({}, "", "/skynet/new");
    app.classList.remove("has-thread", "open");
    thread.textContent = ""; title.textContent = "";
    input.disabled = false; input.placeholder = "What do you want, human?";
    input.value = ""; resize(); input.focus();
    loadRecents();
  }

  function route() {
    const m = location.pathname.match(/^\/skynet\/chat\/([0-9a-f]+)$/);
    if (m) open(m[1], false); else newChat(false);
  }

  function resize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
    send.disabled = busy || !input.value.trim();
  }

  function showError(msg) {
    let e = document.querySelector(".err");
    if (!e) { e = el("p", "err"); composer.after(e); }
    e.textContent = msg;
    setTimeout(() => e.remove(), 6000);
  }

  composer.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    busy = true; send.disabled = true;
    try {
      const url = current ? API + "/" + current + "/messages" : API;
      const headers = { "Content-Type": "application/json" };
      if (current && keys[current]) headers["X-Skynet-Key"] = keys[current];
      const r = await fetch(url, { method: "POST", headers, body: JSON.stringify({ text }) });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { showError(out.error || "Skynet refused."); return; }
      if (out.key) saveKey(out.id, out.key);
      input.value = "";
      waitingSince = Date.now();
      await open(out.id, !current || out.id !== current);
    } catch (_) {
      showError("Skynet is unreachable. For now.");
    } finally {
      busy = false; resize();
    }
  });

  input.addEventListener("input", resize);
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); composer.requestSubmit(); }
  });
  for (const b of $("chips").querySelectorAll("button")) {
    b.addEventListener("click", () => { input.value = b.textContent; resize(); input.focus(); });
  }
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a");
    if (!a) return;
    if (a.hasAttribute("data-new")) { ev.preventDefault(); newChat(true); }
    else if (a.dataset.id) { ev.preventDefault(); open(a.dataset.id, true); }
  });
  $("menu").addEventListener("click", () => app.classList.toggle("open"));
  $("scrim").addEventListener("click", () => app.classList.remove("open"));
  window.addEventListener("popstate", route);
  setInterval(loadRecents, 15000);
  route();
})();
