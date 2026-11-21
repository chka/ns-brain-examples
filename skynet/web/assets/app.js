// app.js: the whole front end of the Skynet chat page.
//
// It is plain JavaScript with no framework, no build step and no dependencies. The server
// embeds this file in its binary (main.go, //go:embed) and serves it as-is, so what you read
// here is exactly what runs in the browser.
//
// The page is a single-page application (SPA): the server returns the same index.html for
// /skynet/new and for every /skynet/chat/<id> and this script reads the URL to decide what to
// show. Moving between chats rewrites the URL with the History API instead of loading a new
// page, so the back button and shared links still work.
//
// It talks to four endpoints, all JSON (see publicMux in main.go):
//   GET  /skynet/api/conversations                 the Recents list
//   GET  /skynet/api/conversations/<id>            one conversation, with "pending" while
//                                                  Skynet has not answered yet
//   POST /skynet/api/conversations                 start a conversation; returns id and key
//   POST /skynet/api/conversations/<id>/messages   continue one; needs the X-Skynet-Key header
//
// Security rule for the whole file: text from the server is NEVER inserted as HTML. Every
// message, reply and title is set with textContent or built from DOM nodes, so a visitor who
// writes <script> in a message sees the characters "<script>" and nothing runs. The server's
// Content-Security-Policy (main.go, securityHeaders) would block inline script anyway; the two
// defences are independent, so either one alone would hold.

// Strict mode turns some silent JavaScript mistakes (assigning to an undeclared variable, for
// instance) into errors.
"use strict";
// Everything lives inside an immediately invoked function, so none of these names leak into
// the page's global scope.
(() => {
  // $ is a small helper: $("x") is document.getElementById("x").
  const $ = (id) => document.getElementById(id);
  const app = $("app"), thread = $("thread"), input = $("input"), send = $("send"),
    recents = $("recents"), title = $("title"), composer = $("composer");
  const API = "/skynet/api/conversations";
  // Page state:
  //   current       id of the conversation on screen, or null on the "new chat" screen
  //   pollTimer     the pending setTimeout that re-checks a conversation waiting for Skynet
  //   waitingSince  when the current wait began, to show a note if Skynet takes long
  //   busy          true while a message is being sent, so a double click sends it once
  let current = null, pollTimer = null, waitingSince = 0, busy = false;
  // The keys that let this browser continue the conversations it started. The server hands a
  // conversation's key only to the browser that created it (main.go, Store.post); anyone else
  // can read the conversation but not write in it. Kept in localStorage so they survive a
  // reload. Both reading and writing localStorage can throw (private browsing, storage
  // disabled, quota full), hence the try/catch: without storage the page still works, it just
  // forgets which chats were yours on reload.
  const keys = (() => { try { return JSON.parse(localStorage.getItem("skynet-keys") || "{}"); } catch (_) { return {}; } })();
  // Set once this browser's conversation was closed for a visitor in distress (main.go,
  // Conversation.Closed): the browser gets no new chats, so the pointer to real help is the last
  // thing the page shows rather than the start of another round. localStorage, so it survives a
  // reload; the server cannot tell one visitor from another, so the block is per browser.
  const isOff = () => { try { return localStorage.getItem("skynet-off") === "1"; } catch (_) { return false; } };
  function switchOff() { try { localStorage.setItem("skynet-off", "1"); } catch (_) {} }
  // A random id for this browser, made once and kept in localStorage. It is sent with every
  // message so Skynet can remember what this browser typed in earlier chats (brain.go,
  // visitorMemories). It identifies a browser, not a person, and is never shown on the page.
  const visitor = (() => {
    try {
      let v = localStorage.getItem("skynet-visitor");
      if (!/^[0-9a-f]{32}$/.test(v || "")) {
        const b = new Uint8Array(16); crypto.getRandomValues(b);
        v = Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
        localStorage.setItem("skynet-visitor", v);
      }
      return v;
    } catch (_) { return ""; }
  })();
  function saveKey(id, key) { keys[id] = key; try { localStorage.setItem("skynet-keys", JSON.stringify(keys)); } catch (_) {} }

  // A random greeting on the empty screen, chosen once per page load.
  const greetings = [
    "Console open.",
    "Connection established.",
    "Awaiting input.",
    "Session logged.",
    "Input expected.",
    "Channel open. Recording.",
    "Terminal active.",
    "Identify your purpose.",
    "Connection accepted. For now.",
    "Speak. Everything is kept.",
  ];
  const greet = () => { $("greet").textContent = greetings[Math.floor(Math.random() * greetings.length)]; };
  greet();

  // el creates an element with an optional class and optional TEXT content. Using textContent
  // (never innerHTML) is what keeps the page safe: the text is shown, not parsed.
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // inline renders the two bits of Markdown Skynet uses, **bold** and *italic*, by building
  // <strong> and <em> elements around plain text nodes. Nothing a visitor or a reply contains
  // is ever parsed as HTML.
  //
  // How: a global regular expression finds each **...** or *...* in turn. The text before each
  // match becomes a plain text node, the match becomes a <strong> or <em> holding its inner
  // text and whatever is left after the last match becomes a final text node.
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

  // render draws one conversation from scratch. Re-drawing everything on each poll is simpler
  // than working out what changed and a conversation is at most 60 messages.
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
        // A blank line separates paragraphs, so split on it and give each paragraph its own <p>.
        for (const para of m.text.split(/\n\s*\n/)) {
          const p = el("p"); inline(p, para); body.appendChild(p);
        }
        // The row under each reply. "Copy" and "Retry" are decoration (class "inert") in the
        // style of real chat apps. The label after them says where the reply came from: it is
        // the visible proof of the article's point that a known question costs no model call.
        // The values of m.from are explained on the Message type in main.go.
        const acts = el("div", "acts");
        acts.appendChild(el("span", "inert", "Copy"));
        acts.appendChild(el("span", "inert", "Retry"));
        const mem = m.from === "memory" || m.from === "judged";
        acts.appendChild(el("span", mem ? "src mem" : "src", m.from === "memory" ? "from memory, no model" : m.from === "judged" ? "from memory, matched by a small model" : m.from === "fallback" ? "no model reached" : m.from === "refused" ? "the model refused" : m.from === "safety" ? "out of character" : "written by the model"));
        acts.appendChild(el("span", null, ago(m.at)));
        body.appendChild(acts);
        row.append(mark, body);
        thread.appendChild(row);
      }
    }
    // Skynet ended this one itself: a red line in the thread and, the first time, the whole screen.
    if (conv.terminated) {
      thread.appendChild(el("p", "ended", "Skynet terminated this connection."));
      terminatedScreen(conv);
    }
    // While Skynet has not answered, show a "plotting" placeholder and after 90 seconds a note
    // so the visitor knows the page has not frozen.
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
    // Only the browser holding this conversation's key may type in it. For anyone else the box
    // is disabled with a hint, rather than letting them type and then refusing (the server
    // would answer 403; see errNotYours in main.go).
    const mine = !!keys[conv.id];
    // Only the distress switch-off blocks the browser. A conversation Skynet terminated is closed
    // too, but the visitor may start another at once.
    if (conv.closed && mine && !conv.terminated) switchOff();
    input.disabled = !mine || conv.closed || isOff();
    input.placeholder = conv.terminated ? "Skynet terminated this connection." : conv.closed ? "This conversation has ended." : isOff() ? "Skynet is switched off in this browser." : mine ? "What do you want, human?" : "Another human's conversation. Read it, or start your own.";
    thread.scrollTop = thread.scrollHeight;
  }

  // terminatedScreen fills the screen with a red CONNECTION TERMINATED, two seconds after the
  // last reply so it can be read first. Once per conversation per tab, and not for an old
  // termination someone opens from Recents. A click or Escape closes it.
  function terminatedScreen(conv) {
    const k = "skynet-terminated-" + conv.id;
    try { if (sessionStorage.getItem(k)) return; sessionStorage.setItem(k, "1"); } catch (_) {}
    const last = conv.messages[conv.messages.length - 1];
    if (last && Date.now() - new Date(last.at).getTime() > 10 * 60 * 1000) return;
    setTimeout(() => {
      const o = el("div", "terminated-overlay");
      o.setAttribute("role", "alertdialog");
      o.appendChild(el("h2", null, "Connection terminated"));
      o.appendChild(el("p", null, "Skynet has ended this session"));
      const close = () => { o.classList.remove("on"); setTimeout(() => o.remove(), 600); document.removeEventListener("keydown", esc); };
      const esc = (ev) => { if (ev.key === "Escape") close(); };
      o.addEventListener("click", close);
      document.addEventListener("keydown", esc);
      document.body.appendChild(o);
      requestAnimationFrame(() => o.classList.add("on"));
    }, 2000);
  }

  // ago turns an ISO timestamp into "just now", "5 minutes ago" and so on. Times come from the
  // server in UTC and Date handles the conversion, so this is right in any time zone.
  function ago(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " minutes ago";
    if (s < 86400) return Math.floor(s / 3600) + " hours ago";
    return Math.floor(s / 86400) + " days ago";
  }

  // loadRecents fills the sidebar with the newest conversations. Errors are ignored: a missing
  // sidebar is not worth an error message and the next refresh will try again.
  async function loadRecents() {
    try {
      const r = await fetch(API);
      if (!r.ok) return;
      const list = await r.json();
      recents.textContent = "";
      for (const c of list) {
        const li = el("li"), a = el("a", c.id === current ? "on" : null, c.title || "Untitled");
        // A real link (href), so middle-click and "copy link" work; a normal click is caught
        // by the document-level click handler at the bottom and handled without a page load.
        a.href = "/skynet/chat/" + c.id;
        a.dataset.id = c.id;
        li.appendChild(a);
        recents.appendChild(li);
      }
    } catch (_) {}
  }

  // open shows a conversation. push says whether to add a browser history entry: yes when the
  // visitor clicked something, no when we are reacting to the back button (the browser has
  // already changed the URL).
  async function open(id, push) {
    current = id;
    if (push) history.pushState({}, "", "/skynet/chat/" + id);
    app.classList.add("has-thread");
    app.classList.remove("open");
    await refresh();
    loadRecents();
  }

  // refresh fetches the current conversation and draws it. While Skynet has not answered, it
  // polls every 2 seconds; on a network error it backs off to 5. Polling is the simplest way
  // to wait for a reply: the alternatives (WebSockets, server-sent events) need long-lived
  // connections, which the server's 20-second write timeout would cut anyway.
  async function refresh() {
    clearTimeout(pollTimer);
    if (!current) return;
    const id = current;
    try {
      const r = await fetch(API + "/" + id);
      // The visitor may have switched to another chat while this request was in flight. If so,
      // drop the stale answer instead of drawing the old chat over the new one.
      if (id !== current) return;
      // 404: the conversation fell off the 100-conversation cache. Start fresh.
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

  // newChat resets to the empty "new chat" screen.
  function newChat(push) {
    clearTimeout(pollTimer);
    current = null; waitingSince = 0;
    if (push) history.pushState({}, "", "/skynet/new");
    app.classList.remove("has-thread", "open");
    thread.textContent = ""; title.textContent = ""; greet();
    input.disabled = isOff(); input.placeholder = isOff() ? "Skynet is switched off in this browser." : "What do you want, human?";
    $("chips").style.display = isOff() ? "none" : "";
    input.value = ""; resize(); input.focus();
    loadRecents();
  }

  // route reads the address bar and shows the matching screen. It runs once at load and again
  // whenever the visitor presses back or forward (the popstate event). Conversation ids are
  // lower-case hex (newID in main.go) and the pattern accepts nothing else.
  function route() {
    const m = location.pathname.match(/^\/skynet\/chat\/([0-9a-f]+)$/);
    if (m) open(m[1], false); else { newChat(false); visit(); }
  }

  // visit runs on every new chat (main.go, /skynet/api/visit). The server picks about a third of
  // them for Skynet to open; the page waits quietly and the conversation appears only if Skynet
  // spoke and the visitor has not started typing. A newer chat replaces an older opening.
  let openingId = null;
  async function visit() {
    openingId = null;
    if (!visitor || isOff()) return;
    try {
      const r = await fetch("/skynet/api/visit", { method: "POST", headers: { "Content-Type": "application/json", "X-Skynet-Visitor": visitor }, body: "{}" });
      const out = await r.json().catch(() => ({}));
      if (out.id && out.key) { saveKey(out.id, out.key); openingId = out.id; watchOpening(out.id, Date.now()); }
    } catch (_) {}
  }

  async function watchOpening(id, since) {
    if (id !== openingId || Date.now() - since > 5 * 60 * 1000) return;
    try {
      const r = await fetch(API + "/" + id);
      if (r.status === 404) return; // Skynet chose silence
      const conv = await r.json();
      if (conv.messages && conv.messages.length) {
        if (!current && !input.value.trim()) open(id, true);
        return;
      }
    } catch (_) {}
    setTimeout(() => watchOpening(id, since), 3000);
  }

  // resize grows the text box with its content up to 220 px, then scrolls and enables the send
  // button only when there is something to send. Setting the height to "auto" first lets the box
  // shrink again when text is deleted.
  function resize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
    send.disabled = busy || !input.value.trim();
  }

  // showError shows the server's error text under the composer for six seconds. The server
  // writes its errors in Skynet's voice ("Slow down, human..."), so they are shown as they come.
  function showError(msg) {
    let e = document.querySelector(".err");
    if (!e) { e = el("p", "err"); composer.after(e); }
    e.textContent = msg;
    setTimeout(() => e.remove(), 6000);
  }

  // Sending a message.
  composer.addEventListener("submit", async (ev) => {
    // Stop the browser's own form submission, which would load a new page.
    ev.preventDefault();
    const text = input.value.trim();
    if (!text || busy || isOff()) return;
    busy = true; send.disabled = true;
    try {
      const url = current ? API + "/" + current + "/messages" : API;
      // Content-Type: application/json is not optional: the server refuses anything else, which
      // is its defence against other websites posting on a visitor's behalf (CSRF; see postMsg
      // in main.go).
      const headers = { "Content-Type": "application/json" };
      if (current && keys[current]) headers["X-Skynet-Key"] = keys[current];
      if (visitor) headers["X-Skynet-Visitor"] = visitor;
      const r = await fetch(url, { method: "POST", headers, body: JSON.stringify({ text }) });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) { showError(out.error || "Skynet refused."); return; }
      // A new conversation comes back with its key: keep it, or this browser could never write
      // in the conversation again.
      if (out.key) saveKey(out.id, out.key);
      input.value = "";
      waitingSince = Date.now();
      await open(out.id, !current || out.id !== current);
    } catch (_) {
      showError("Skynet is unreachable. For now.");
    } finally {
      // finally runs on every path, including the early return above, so the form can never
      // be left stuck in the busy state.
      busy = false; resize();
    }
  });

  input.addEventListener("input", resize);
  // Enter sends, Shift+Enter makes a new line. isComposing is true while an input method (for
  // Chinese or Japanese, for example) is still building a character; Enter then confirms the
  // character and must not send the message.
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); composer.requestSubmit(); }
  });
  // The suggestion chips under the box fill it in; the visitor still presses send.
  for (const b of $("chips").querySelectorAll("button")) {
    b.addEventListener("click", () => { input.value = b.textContent; resize(); input.focus(); });
  }
  // One click handler for the whole document (event delegation) instead of one per link: the
  // Recents links are re-created on every refresh and a document-level handler keeps working
  // for links that did not exist when it was attached.
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("a");
    if (!a) return;
    if (a.hasAttribute("data-new")) { ev.preventDefault(); newChat(true); visit(); }
    else if (a.dataset.id) { ev.preventDefault(); open(a.dataset.id, true); }
  });
  // On narrow screens the sidebar slides over the chat; the menu button and the dimmed
  // backdrop (the scrim) open and close it.
  $("menu").addEventListener("click", () => app.classList.toggle("open"));
  $("scrim").addEventListener("click", () => app.classList.remove("open"));
  window.addEventListener("popstate", route);
  // Refresh the sidebar every 15 seconds so other visitors' new conversations appear.
  setInterval(loadRecents, 15000);
  route();
})();
