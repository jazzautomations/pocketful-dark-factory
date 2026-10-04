/* Pocketful browser client. Plain JavaScript, no external dependencies. */
(function () {
  "use strict";

  var TOKEN_KEY = "pocketful.token";
  var session = { token: null, me: null };
  try { session.token = window.localStorage.getItem(TOKEN_KEY); } catch (e) { /* ignore */ }

  // ---------------------------------------------------------------------------
  // small DOM helper

  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "text") el.textContent = v;
        else if (k === "class") el.className = v;
        else if (k === "testid") el.setAttribute("data-testid", v);
        else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
        else if (k === "value") el.value = v;
        else el.setAttribute(k, v === true ? "" : v);
      });
    }
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(el, c); }); return; }
    el.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function replace(el, child) { clear(el); append(el, child); return el; }

  function newKey() {
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
  }

  // ---------------------------------------------------------------------------
  // money and time

  function fmtMoney(minor) {
    var mu = session.me ? session.me.minor_units : 2;
    var cur = session.me ? session.me.currency : "";
    var neg = minor < 0;
    var digits = String(Math.abs(minor));
    var text;
    if (mu === 0) {
      text = digits;
    } else {
      while (digits.length < mu + 1) digits = "0" + digits;
      text = digits.slice(0, digits.length - mu) + "." + digits.slice(digits.length - mu);
    }
    return (neg ? "-" : "") + text + " " + cur;
  }

  function toDecimalInput(minor) {
    return fmtMoney(minor).split(" ")[0];
  }

  // Parse a typed decimal into minor units. Returns {ok, value} or {ok:false, error}.
  function parseAmount(raw) {
    var mu = session.me ? session.me.minor_units : 2;
    var text = String(raw || "").trim();
    if (!text) return { ok: false, error: "Enter an amount." };
    var m = /^(\d+)(?:\.(\d+))?$/.exec(text);
    if (!m) return { ok: false, error: "Enter the amount as a number, for example " + (mu ? "15." + "0".repeat(mu) : "15") + "." };
    var frac = m[2] || "";
    if (frac.length > mu) {
      return { ok: false, error: mu === 0
        ? "This currency has no decimal places."
        : "Use at most " + mu + " decimal places." };
    }
    while (frac.length < mu) frac += "0";
    var whole = m[1].replace(/^0+(?=\d)/, "");
    if (whole.length > 12) return { ok: false, error: "That amount is too large." };
    var value = Number(whole) * Math.pow(10, mu) + (frac ? Number(frac) : 0);
    if (value < 1) return { ok: false, error: "Enter an amount greater than zero." };
    if (value > 1000000000) return { ok: false, error: "That amount is above the single-payment limit." };
    return { ok: true, value: value };
  }

  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    try {
      return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    } catch (e) {
      return d.toLocaleString();
    }
  }

  function equalSplit(amount, n) {
    var base = Math.floor(amount / n);
    var rem = amount - base * n;
    var out = [];
    for (var i = 0; i < n; i++) out.push(base + (i < rem ? 1 : 0));
    return out;
  }

  // ---------------------------------------------------------------------------
  // API

  // Resolves to {kind: "ok"|"error"|"uncertain", status, data}. Never rejects.
  // "uncertain" means we cannot know whether the server applied the request.
  function api(method, path, opts) {
    opts = opts || {};
    var headers = { "Accept": "application/json" };
    if (session.token) headers["Authorization"] = "Bearer " + session.token;
    if (opts.key) headers["Idempotency-Key"] = opts.key;
    var init = { method: method, headers: headers, cache: "no-store" };
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    var controller = window.AbortController ? new AbortController() : null;
    var timer = null;
    if (controller) {
      init.signal = controller.signal;
      timer = setTimeout(function () { controller.abort(); }, 15000);
    }
    return fetch(path, init).then(function (resp) {
      return resp.text().then(function (text) {
        if (timer) clearTimeout(timer);
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (resp.status >= 500 || (resp.status >= 200 && resp.status < 300 && text && data === null)) {
          return { kind: "uncertain", status: resp.status, data: data };
        }
        if (resp.ok) return { kind: "ok", status: resp.status, data: data };
        if (resp.status === 401 && session.token && !opts.noAuthRedirect) {
          signOut();
        }
        return { kind: "error", status: resp.status, data: data };
      });
    }, function () {
      if (timer) clearTimeout(timer);
      return { kind: "uncertain", status: 0, data: null };
    });
  }

  var ERROR_TEXT = {
    insufficient_funds: "There isn't enough available money in your wallet for this.",
    self_payment: "You can't send money to yourself.",
    self_request: "You can't request money from yourself.",
    not_found: "We couldn't find anyone with that handle.",
    validation_failed: "Please check the details and try again.",
    request_not_pending: "This request is no longer pending — it was paid, declined or cancelled.",
    authorization_not_open: "This hold is no longer open.",
    authorization_expired: "This hold has expired.",
    capture_exceeds_authorization: "That is more than the amount still on hold.",
    forbidden: "You're not allowed to do that.",
    idempotency_key_reuse: "This form was already used for a different payment. Edit it and try again.",
    email_taken: "An account with that email already exists.",
    handle_taken: "The handle from that email is already taken. Try a different email address.",
    unauthenticated: "That email and password don't match an account."
  };

  function errorText(res, fallback) {
    var code = res && res.data && res.data.error && res.data.error.code;
    if (code && ERROR_TEXT[code]) return ERROR_TEXT[code];
    var msg = res && res.data && res.data.error && res.data.error.message;
    return msg || fallback || "Something went wrong. Please try again.";
  }

  // ---------------------------------------------------------------------------
  // session and chrome

  function setToken(token) {
    session.token = token;
    try {
      if (token) window.localStorage.setItem(TOKEN_KEY, token);
      else window.localStorage.removeItem(TOKEN_KEY);
    } catch (e) { /* ignore */ }
  }

  function signOut() {
    setToken(null);
    session.me = null;
    window.location.assign("/login");
  }

  var NAV = [
    ["/", "Wallet"],
    ["/requests", "Requests"],
    ["/split", "Split a bill"],
    ["/authorizations", "Holds"]
  ];

  function renderTopbar() {
    var bar = document.getElementById("topbar");
    var path = window.location.pathname;
    var inner = h("div", { class: "topbar-inner" },
      h("a", { class: "brand", href: "/" }, h("span", { class: "brand-mark", "aria-hidden": "true" }), "Pocketful"));
    if (session.me) {
      var nav = h("nav", { class: "nav", "aria-label": "Main" });
      NAV.forEach(function (n) {
        nav.appendChild(h("a", { href: n[0], "aria-current": path === n[0] ? "page" : null, text: n[1] }));
      });
      inner.appendChild(nav);
      inner.appendChild(h("div", { class: "user-chip" },
        h("div", { class: "user-names" },
          h("span", { class: "name", testid: "current-user", text: session.me.display_name }),
          h("span", { class: "handle-line" }, h("span", { testid: "current-handle", text: session.me.handle }))),
        h("button", { type: "button", class: "btn btn-ghost btn-sm", testid: "logout-button", onclick: signOut }, "Log out")));
    } else {
      var nav2 = h("nav", { class: "nav", "aria-label": "Account" },
        h("a", { href: "/login", "aria-current": path === "/login" ? "page" : null, text: "Log in" }),
        h("a", { href: "/signup", "aria-current": path === "/signup" ? "page" : null, text: "Create account" }));
      inner.appendChild(nav2);
    }
    replace(bar, inner);
  }

  function main() { return document.getElementById("main"); }

  function loadingState(text) {
    return h("div", { class: "state state-loading", role: "status" }, text || "Loading…");
  }

  function errorState(text, retry) {
    return h("div", { class: "msg msg-error", role: "alert" },
      h("strong", { text: text || "We couldn't load this right now." }),
      retry ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: retry, style: "margin-top:8px" }, "Try again") : null);
  }

  // A slot that shows at most one feedback message, identified by a data-testid.
  function feedback() {
    var el = h("div", { "aria-live": "polite" });
    return {
      el: el,
      clear: function () { clear(el); },
      error: function (testid, text) {
        replace(el, h("div", { class: "msg msg-error", role: "alert", testid: testid }, text));
      },
      uncertain: function (testid, title, text) {
        replace(el, h("div", { class: "msg msg-warn", role: "status", testid: testid },
          h("strong", { text: title }), text));
      },
      ok: function (testid, text) {
        replace(el, h("div", { class: "msg msg-ok", role: "status", testid: testid }, text));
      }
    };
  }

  function field(id, label, input, hint) {
    input.id = id;
    return h("div", { class: "field" },
      h("label", { for: id, text: label }), input,
      hint ? h("span", { class: "hint", text: hint }) : null);
  }

  function amountInput(testid, id, label) {
    var input = h("input", { type: "text", inputmode: "decimal", autocomplete: "off", testid: testid, placeholder: session.me && session.me.minor_units ? "0." + "0".repeat(session.me.minor_units) : "0" });
    input.id = id;
    return {
      input: input,
      el: h("div", { class: "field" }, h("label", { for: id, text: label }),
        h("div", { class: "amount-input" }, input, h("span", { class: "cur", text: session.me ? session.me.currency : "" })))
    };
  }

  function visibilitySelect(testid) {
    return h("select", { testid: testid },
      h("option", { value: "public", text: "Public" }),
      h("option", { value: "private", text: "Private" }));
  }

  function busy(button, on, label) {
    if (on) {
      button.dataset.label = button.textContent;
      button.textContent = label || "Working…";
      button.setAttribute("aria-busy", "true");
      button.disabled = true;
    } else {
      if (button.dataset.label) button.textContent = button.dataset.label;
      button.removeAttribute("aria-busy");
      button.disabled = false;
    }
  }

  // ---------------------------------------------------------------------------
  // wallet summary (shared, "latest refresh wins")

  var wallet = { seq: 0, el: null, feedEl: null, note: null };

  function renderWalletCard(me) {
    var figs = h("div", { class: "figures" },
      h("div", { class: "figure" }, h("span", { class: "k", text: "Total balance" }),
        h("span", { class: "v", testid: "wallet-balance", "data-amount": String(me.total !== undefined ? me.total : me.balance), text: fmtMoney(me.total !== undefined ? me.total : me.balance) })));
    if (me.held > 0) {
      figs.appendChild(h("div", { class: "figure held" }, h("span", { class: "k", text: "On hold" }),
        h("span", { class: "v", testid: "wallet-held", "data-amount": String(me.held), text: fmtMoney(me.held) })));
    }
    return [
      h("p", { class: "label", text: "Available to spend" }),
      h("p", { class: "headline", testid: "wallet-available", "data-amount": String(me.available), text: fmtMoney(me.available) }),
      figs
    ];
  }

  function walletCard(withRefresh) {
    var body = h("div", { "aria-live": "polite" }, renderWalletCard(session.me));
    var note = h("span", { class: "refresh-note", role: "status" });
    wallet.el = body;
    wallet.note = note;
    var card = h("section", { class: "card wallet", "aria-label": "Your wallet" }, body);
    if (withRefresh) {
      card.appendChild(h("div", { class: "wallet-actions" },
        h("button", { type: "button", class: "btn btn-ghost btn-sm", testid: "wallet-refresh", onclick: function () { refreshWallet(); } }, "Refresh"),
        note));
    }
    return card;
  }

  // Reload balance (and the feed when present). Only the latest refresh may render.
  function refreshWallet() {
    var my = ++wallet.seq;
    if (wallet.note) wallet.note.textContent = "Updating…";
    var reqs = [api("GET", "/me")];
    if (wallet.feedEl) reqs.push(api("GET", "/activity?limit=50"));
    return Promise.all(reqs).then(function (results) {
      if (my !== wallet.seq) return;          // a newer refresh has started: drop this one
      var me = results[0];
      if (me.kind === "ok") {
        session.me = me.data;
        if (wallet.el) replace(wallet.el, renderWalletCard(me.data));
        var cu = document.querySelector("[data-testid='current-user']");
        if (cu) cu.textContent = me.data.display_name;
      }
      if (wallet.feedEl && results[1]) renderFeed(results[1]);
      var failed = results.some(function (r) { return r.kind !== "ok"; });
      if (wallet.note) wallet.note.textContent = failed ? "Couldn't refresh — showing the last known figures." : "Up to date";
    });
  }

  function renderFeed(res) {
    var el = wallet.feedEl;
    if (res.kind !== "ok") {
      replace(el, errorState("We couldn't load your activity.", function () { refreshWallet(); }));
      return;
    }
    var payments = res.data.payments || [];
    if (!payments.length) {
      replace(el, h("div", { class: "empty", testid: "empty-activity" },
        h("strong", { text: "No activity yet" }),
        "Payments you send or receive, and public payments from others, will appear here."));
      return;
    }
    var me = session.me;
    var list = h("ul", { class: "list", testid: "activity-list" });
    payments.forEach(function (p) {
      var outgoing = me && p.from_user_id === me.user_id;
      var incoming = me && p.to_user_id === me.user_id;
      var dirText = outgoing ? "You paid" : incoming ? "You received" : "Payment";
      var tags = [h("span", { class: "badge badge-" + p.visibility, text: p.visibility === "private" ? "Private" : "Public" })];
      if (p.request_id) tags.push(h("span", { class: "badge", text: "Request" }));
      if (p.authorization_id) tags.push(h("span", { class: "badge badge-held", text: "From a hold" }));
      if (p.settlement_id) tags.push(h("span", { class: "badge", text: "Settlement" }));
      list.appendChild(h("li", { class: "item", testid: "activity-item-" + p.payment_id, "data-visibility": p.visibility },
        h("div", null,
          h("div", { class: "dir", text: dirText }),
          h("div", { class: "who", testid: "activity-parties-" + p.payment_id, text: "@" + p.from_handle + " → @" + p.to_handle }),
          h("div", { class: "note", testid: "activity-note-" + p.payment_id, text: p.note }),
          h("div", { class: "meta" }, h("time", { datetime: p.created_at, text: fmtTime(p.created_at) }), tags)),
        h("div", { class: "side" },
          h("span", { class: "amt " + (outgoing ? "out" : incoming ? "in" : ""), "aria-label": (outgoing ? "minus " : incoming ? "plus " : "") + fmtMoney(p.amount) },
            outgoing ? "− " : incoming ? "+ " : "",
            h("span", { testid: "activity-amount-" + p.payment_id, text: fmtMoney(p.amount) })))));
    });
    replace(el, list);
  }

  // ---------------------------------------------------------------------------
  // idempotent form helper: one key per unchanged form; any edit starts a new one

  function keyedForm(form) {
    var state = { key: null };
    function reset() { state.key = null; }
    form.addEventListener("input", reset);
    form.addEventListener("change", reset);
    return {
      key: function () { if (!state.key) state.key = newKey(); return state.key; }
    };
  }

  // ---------------------------------------------------------------------------
  // screens

  function pageHead(title, sub) {
    return h("div", { class: "page-head" }, h("h1", { text: title }), sub ? h("p", { text: sub }) : null);
  }

  function payForm() {
    var handle = h("input", { type: "text", testid: "pay-handle", autocomplete: "off", autocapitalize: "none", spellcheck: "false", placeholder: "e.g. bob" });
    var amt = amountInput("pay-amount", "pay-amount", "Amount");
    var note = h("input", { type: "text", testid: "pay-note", maxlength: "200", placeholder: "What's it for? (optional)" });
    var vis = visibilitySelect("pay-visibility");
    var submit = h("button", { type: "submit", class: "btn btn-primary", testid: "pay-submit" }, "Send money");
    var fb = feedback();
    var form = h("form", { novalidate: true, "aria-label": "Send money" },
      field("pay-handle", "To (handle)", handle),
      amt.el,
      field("pay-note", "Note", note),
      field("pay-visibility", "Who can see it", vis, "Public payments appear in everyone's feed; private ones only to the two of you."),
      fb.el, submit);
    var keys = keyedForm(form);
    var inFlight = false;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (inFlight) return;
      var parsed = parseAmount(amt.input.value);
      if (!parsed.ok) { fb.error("pay-error", parsed.error); return; }
      var to = handle.value.trim().replace(/^@/, "");
      if (!to) { fb.error("pay-error", "Enter the handle of the person you're paying."); return; }
      var body = { to_handle: to, amount: parsed.value, note: note.value, visibility: vis.value };
      var key = keys.key();
      inFlight = true;
      busy(submit, true, "Sending…");
      api("POST", "/payments", { body: body, key: key }).then(function (res) {
        inFlight = false;
        busy(submit, false);
        if (res.kind === "ok") {
          fb.ok("pay-success", (res.status === 200 ? "Already sent: " : "Sent ") + fmtMoney(res.data.amount) + " to @" + res.data.to_handle + ".");
          refreshWallet();
        } else if (res.kind === "uncertain") {
          fb.uncertain("pay-uncertain", "We couldn't confirm this payment.",
            " It may or may not have gone through. Press “Send money” again without changing anything — we'll check and it will never be sent twice.");
        } else {
          fb.error("pay-error", errorText(res));
          refreshWallet();
        }
      });
    });
    return h("section", { class: "card", "aria-labelledby": "pay-title" },
      h("h2", { id: "pay-title", text: "Send money" }),
      h("p", { class: "sub", text: "Money moves instantly from your available balance." }), form);
  }

  function requestForm() {
    var handle = h("input", { type: "text", testid: "request-handle", autocomplete: "off", autocapitalize: "none", spellcheck: "false", placeholder: "e.g. ada" });
    var amt = amountInput("request-amount", "request-amount", "Amount");
    var note = h("input", { type: "text", testid: "request-note", maxlength: "200", placeholder: "What's it for? (optional)" });
    var submit = h("button", { type: "submit", class: "btn btn-secondary", testid: "request-submit" }, "Request money");
    var fb = feedback();
    var form = h("form", { novalidate: true, "aria-label": "Request money" },
      field("request-handle", "From (handle)", handle), amt.el, field("request-note", "Note", note), fb.el, submit);
    var keys = keyedForm(form);
    var inFlight = false;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (inFlight) return;
      var parsed = parseAmount(amt.input.value);
      if (!parsed.ok) { fb.error("request-error", parsed.error); return; }
      var from = handle.value.trim().replace(/^@/, "");
      if (!from) { fb.error("request-error", "Enter the handle of the person you're asking."); return; }
      var body = { payer_handle: from, amount: parsed.value, note: note.value };
      inFlight = true;
      busy(submit, true, "Sending request…");
      api("POST", "/requests", { body: body, key: keys.key() }).then(function (res) {
        inFlight = false;
        busy(submit, false);
        if (res.kind === "ok") {
          fb.ok("request-success", "Asked @" + res.data.payer_handle + " for " + fmtMoney(res.data.amount) + ". You'll find it under Requests.");
        } else if (res.kind === "uncertain") {
          fb.uncertain("request-uncertain", "We couldn't confirm this request.", " Press “Request money” again without changing anything to retry safely.");
        } else {
          fb.error("request-error", errorText(res));
        }
      });
    });
    return h("section", { class: "card", "aria-labelledby": "req-title" },
      h("h2", { id: "req-title", text: "Request money" }),
      h("p", { class: "sub", text: "They can pay, or decline, from their Requests page." }), form);
  }

  function authorizeForm(onDone) {
    var handle = h("input", { type: "text", testid: "authorize-handle", autocomplete: "off", autocapitalize: "none", spellcheck: "false", placeholder: "e.g. bob" });
    var amt = amountInput("authorize-amount", "authorize-amount", "Amount to hold");
    var note = h("input", { type: "text", testid: "authorize-note", maxlength: "200", placeholder: "e.g. deposit (optional)" });
    var vis = visibilitySelect("authorize-visibility");
    var submit = h("button", { type: "submit", class: "btn btn-secondary", testid: "authorize-submit" }, "Place hold");
    var fb = feedback();
    var form = h("form", { novalidate: true, "aria-label": "Place a hold" },
      field("authorize-handle", "For (handle)", handle), amt.el, field("authorize-note", "Note", note),
      field("authorize-visibility", "Who can see the payment", vis, "Applies to the payment made when they collect."), fb.el, submit);
    var keys = keyedForm(form);
    var inFlight = false;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (inFlight) return;
      var parsed = parseAmount(amt.input.value);
      if (!parsed.ok) { fb.error("authorize-error", parsed.error); return; }
      var to = handle.value.trim().replace(/^@/, "");
      if (!to) { fb.error("authorize-error", "Enter the handle of the person who will collect."); return; }
      var body = { to_handle: to, amount: parsed.value, note: note.value, visibility: vis.value };
      inFlight = true;
      busy(submit, true, "Placing hold…");
      api("POST", "/authorizations", { body: body, key: keys.key() }).then(function (res) {
        inFlight = false;
        busy(submit, false);
        if (res.kind === "ok") {
          fb.ok("authorize-success", "Holding " + fmtMoney(res.data.amount) + " for @" + res.data.to_handle + " until " + fmtTime(res.data.expires_at) + ".");
          refreshWallet();
          if (onDone) onDone();
        } else if (res.kind === "uncertain") {
          fb.uncertain("authorize-uncertain", "We couldn't confirm this hold.", " Press “Place hold” again without changing anything to retry safely.");
        } else {
          fb.error("authorize-error", errorText(res));
          refreshWallet();
          if (onDone) onDone();
        }
      });
    });
    return h("section", { class: "card", "aria-labelledby": "auth-title" },
      h("h2", { id: "auth-title", text: "Hold money for someone" }),
      h("p", { class: "sub", text: "Reserve money now; they collect it later. Held money can't be spent elsewhere." }), form);
  }

  function homeScreen() {
    var feedEl = h("div", null, loadingState("Loading activity…"));
    wallet.feedEl = feedEl;
    var page = h("div", null,
      pageHead("Hi, " + session.me.display_name, null),
      h("div", { class: "home-grid" },
        h("div", { class: "home-left" },
          h("div", { class: "o-wallet" }, walletCard(true)),
          h("div", { class: "o-pay" }, payForm()),
          h("div", { class: "o-request" }, requestForm()),
          h("div", { class: "o-hold" }, authorizeForm(null))),
        h("section", { class: "card o-feed", "aria-labelledby": "feed-title" },
          h("div", { class: "section-title" }, h("h2", { id: "feed-title", text: "Activity" })),
          feedEl)));
    replace(main(), page);
    refreshWallet();
  }

  // -- requests ---------------------------------------------------------------

  function requestsScreen() {
    var fb = feedback();
    var body = h("div", { class: "stack" }, loadingState("Loading requests…"));
    var payKeys = {};
    var seq = 0;
    replace(main(), h("div", null,
      pageHead("Requests", "Money you've been asked for, and money you've asked others for."),
      h("div", { class: "stack" }, walletCard(false), fb.el, body)));

    function load() {
      var my = ++seq;
      return Promise.all([
        api("GET", "/requests?direction=incoming&limit=200"),
        api("GET", "/requests?direction=outgoing&limit=200")
      ]).then(function (r) {
        if (my !== seq) return;
        if (r[0].kind !== "ok" || r[1].kind !== "ok") {
          replace(body, errorState("We couldn't load your requests.", load));
          return;
        }
        render(r[0].data.requests, r[1].data.requests);
      });
    }

    function after(res, okText, uncertainTitle) {
      if (res.kind === "ok") fb.ok("request-success", okText(res.data));
      else if (res.kind === "uncertain") fb.uncertain("request-uncertain", uncertainTitle, " Try again — a retry will never pay twice.");
      else fb.error("request-error", errorText(res));
      load();
      refreshWallet();
    }

    function item(r, incoming) {
      var pending = r.status === "pending";
      var other = incoming ? r.requester_handle : r.payer_handle;
      var actions = null;
      if (pending && incoming) {
        var vis = visibilitySelect("request-visibility-" + r.request_id);
        vis.id = "rv-" + r.request_id;
        var payBtn = h("button", { type: "button", class: "btn btn-primary btn-sm", testid: "request-pay-" + r.request_id }, "Pay " + fmtMoney(r.amount));
        var decBtn = h("button", { type: "button", class: "btn btn-ghost btn-sm", testid: "request-decline-" + r.request_id }, "Decline");
        payBtn.addEventListener("click", function () {
          var bodyObj = { visibility: vis.value };
          var sig = r.request_id + "|" + vis.value;
          if (!payKeys[sig]) payKeys[sig] = newKey();
          fb.clear();
          busy(payBtn, true, "Paying…");
          api("POST", "/requests/" + encodeURIComponent(r.request_id) + "/pay", { body: bodyObj, key: payKeys[sig] }).then(function (res) {
            busy(payBtn, false);
            after(res, function (p) { return "Paid " + fmtMoney(p.amount) + " to @" + p.to_handle + "."; }, "We couldn't confirm this payment.");
          });
        });
        decBtn.addEventListener("click", function () {
          fb.clear();
          busy(decBtn, true, "Declining…");
          api("POST", "/requests/" + encodeURIComponent(r.request_id) + "/decline").then(function (res) {
            busy(decBtn, false);
            after(res, function () { return "Request from @" + other + " declined."; }, "We couldn't confirm the decline.");
          });
        });
        actions = h("div", { class: "actions" }, h("div", { class: "field" }, h("label", { for: vis.id, text: "Payment visibility" }), vis), payBtn, decBtn);
      } else if (pending && !incoming) {
        var canBtn = h("button", { type: "button", class: "btn btn-danger btn-sm", testid: "request-cancel-" + r.request_id }, "Cancel request");
        canBtn.addEventListener("click", function () {
          fb.clear();
          busy(canBtn, true, "Cancelling…");
          api("POST", "/requests/" + encodeURIComponent(r.request_id) + "/cancel").then(function (res) {
            busy(canBtn, false);
            after(res, function () { return "Request to @" + other + " cancelled."; }, "We couldn't confirm the cancellation.");
          });
        });
        actions = h("div", { class: "actions" }, canBtn);
      }
      var statusLabel = { pending: "Pending", paid: "Paid", declined: "Declined", cancelled: "Cancelled" }[r.status] || r.status;
      return h("li", { class: "item", testid: "request-item-" + r.request_id, "data-status": r.status },
        h("div", null,
          h("div", { class: "dir", text: incoming ? "Asked by" : "You asked" }),
          h("div", { class: "who", text: "@" + other }),
          h("div", { class: "note", text: r.note }),
          h("div", { class: "meta" }, h("time", { datetime: r.created_at, text: fmtTime(r.created_at) }))),
        h("div", { class: "side" },
          h("span", { class: "amt", testid: "request-amount-" + r.request_id, text: fmtMoney(r.amount) }),
          h("span", { class: "badge badge-" + r.status, text: statusLabel })),
        actions);
    }

    function listSection(title, testid, items, incoming, emptyText, quiet) {
      var list = h("ul", { class: "list", testid: testid });
      items.forEach(function (r) { list.appendChild(item(r, incoming)); });
      return h("section", { class: "card", "aria-label": title },
        h("div", { class: "section-title" }, h("h2", { text: title }), h("span", { class: "count", text: items.length + (items.length === 1 ? " request" : " requests") })),
        list,
        items.length || quiet ? null : h("p", { class: "empty", text: emptyText }));
    }

    function render(incoming, outgoing) {
      var none = !incoming.length && !outgoing.length;
      replace(body, [
        none
          ? h("div", { class: "empty", testid: "empty-requests" }, h("strong", { text: "No requests yet" }), "Ask someone for money from your Wallet page, or split a bill.")
          : null,
        h("div", { class: "grid grid-2" },
          listSection("Incoming", "incoming-list", incoming, true, "Nobody has asked you for money.", none),
          listSection("Outgoing", "outgoing-list", outgoing, false, "You haven't asked anyone for money.", none))
      ]);
    }

    load();
    refreshWallet();
  }

  // -- split ------------------------------------------------------------------

  function splitScreen() {
    var amt = amountInput("split-amount", "split-amount", "Total amount you paid");
    var handles = h("input", { type: "text", testid: "split-handles", autocomplete: "off", autocapitalize: "none", spellcheck: "false", placeholder: "e.g. " + session.me.handle + ", bob, cy" });
    var note = h("input", { type: "text", testid: "split-note", maxlength: "200", placeholder: "e.g. dinner (optional)" });
    var submit = h("button", { type: "submit", class: "btn btn-primary", testid: "split-submit" }, "Split and send requests");
    var preview = h("div", { "aria-live": "polite" });
    var fb = feedback();
    var result = h("div");
    var form = h("form", { novalidate: true, "aria-label": "Split a bill" },
      amt.el,
      field("split-handles", "Who's splitting", handles, "Handles separated by commas, in order. Include yourself to take a share."),
      field("split-note", "Note", note), preview, fb.el, submit);
    var keys = keyedForm(form);

    function participants() {
      return handles.value.split(",").map(function (s) { return s.trim().replace(/^@/, "").toLowerCase(); })
        .filter(function (s) { return s.length > 0; });
    }

    function renderPreview() {
      var parsed = parseAmount(amt.input.value);
      var people = participants();
      if (!parsed.ok || !people.length) {
        replace(preview, h("p", { class: "hint", text: "Enter an amount and at least one handle to preview the shares." }));
        return;
      }
      var shares = equalSplit(parsed.value, people.length);
      var ul = h("ul");
      people.forEach(function (p, i) {
        ul.appendChild(h("li", null,
          h("span", { class: "h" }, p, p === session.me.handle ? h("span", { class: "you", text: "(you)" }) : null),
          h("span", { class: "s", testid: "split-share-" + p, text: fmtMoney(shares[i]) })));
      });
      replace(preview, h("div", { class: "preview", testid: "split-preview" }, h("h3", { text: "Each person's share" }), ul));
    }

    amt.input.addEventListener("input", renderPreview);
    handles.addEventListener("input", renderPreview);
    renderPreview();

    var inFlight = false;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (inFlight) return;
      var parsed = parseAmount(amt.input.value);
      if (!parsed.ok) { fb.error("split-error", parsed.error); return; }
      var people = participants();
      if (!people.length) { fb.error("split-error", "Add at least one handle."); return; }
      var body = { amount: parsed.value, participant_handles: people, note: note.value };
      inFlight = true;
      busy(submit, true, "Splitting…");
      api("POST", "/splits", { body: body, key: keys.key() }).then(function (res) {
        inFlight = false;
        busy(submit, false);
        if (res.kind === "ok") {
          var n = res.data.requests.length;
          fb.ok("split-success", n ? "Sent " + n + (n === 1 ? " request." : " requests.") : "Split recorded — there was nobody else to ask.");
          var ul = h("ul", { class: "list" });
          res.data.requests.forEach(function (r) {
            ul.appendChild(h("li", { class: "item" },
              h("div", null, h("div", { class: "who", text: "@" + r.payer_handle }), h("div", { class: "meta", text: "Pending" })),
              h("span", { class: "amt", text: fmtMoney(r.amount) })));
          });
          replace(result, n ? h("section", { class: "card" }, h("h2", { text: "Requests sent" }), ul) : null);
        } else if (res.kind === "uncertain") {
          fb.uncertain("split-uncertain", "We couldn't confirm this split.", " Press the button again without changing anything to retry safely.");
        } else {
          var code = res.data && res.data.error && res.data.error.code;
          fb.error("split-error", code === "not_found" ? "One of those handles doesn't belong to anyone." :
            code === "validation_failed" ? "Check the amount and handles — each person can only be listed once." : errorText(res));
        }
      });
    });

    replace(main(), h("div", null,
      pageHead("Split a bill", "You paid; everyone else gets a request for their share."),
      h("div", { class: "grid grid-2" },
        h("section", { class: "card", "aria-label": "Split form" }, form),
        h("div", { class: "stack" }, result,
          h("section", { class: "card" }, h("h2", { text: "How shares work" }),
            h("p", { class: "sub", style: "margin:6px 0 0", text: "The total is divided equally. If it doesn't divide exactly, the first people in your list pay one extra " + (session.me.minor_units ? "cent" : "unit") + " each, so shares always add up to the total." }))))));
  }

  // -- authorizations ---------------------------------------------------------

  function authorizationsScreen() {
    var fb = feedback();
    var body = h("div", null, loadingState("Loading holds…"));
    var capKeys = {};
    var seq = 0;

    function load() {
      var my = ++seq;
      return api("GET", "/authorizations?limit=200").then(function (res) {
        if (my !== seq) return;
        if (res.kind !== "ok") {
          replace(body, errorState("We couldn't load your holds.", load));
          return;
        }
        render(res.data.authorizations || []);
      });
    }

    function after(res, okText, uncertainTitle) {
      if (res.kind === "ok") fb.ok("authorization-success", okText(res.data));
      else if (res.kind === "uncertain") fb.uncertain("authorization-uncertain", uncertainTitle, " Try again — a retry will never move money twice.");
      else fb.error("authorization-error", errorText(res));
      load();
      refreshWallet();
    }

    function item(a) {
      var me = session.me;
      var outgoing = a.from_user_id === me.user_id;
      var open = a.status === "open";
      var remaining = a.remaining_amount !== undefined ? a.remaining_amount : a.amount - a.captured_amount;
      var actions = null;
      if (open && !outgoing) {
        var capInput = h("input", { type: "text", inputmode: "decimal", autocomplete: "off", testid: "authorization-capture-amount-" + a.authorization_id, value: toDecimalInput(remaining) });
        capInput.id = "cap-" + a.authorization_id;
        var keep = h("input", { type: "checkbox", id: "keep-" + a.authorization_id });
        var capBtn = h("button", { type: "button", class: "btn btn-primary btn-sm", testid: "authorization-capture-" + a.authorization_id }, "Collect");
        capBtn.addEventListener("click", function () {
          var parsed = parseAmount(capInput.value);
          if (!parsed.ok) { fb.error("authorization-error", parsed.error); return; }
          var bodyObj = { amount: parsed.value };
          if (keep.checked) bodyObj.final = false;
          var sig = a.authorization_id + "|" + JSON.stringify(bodyObj);
          if (!capKeys[sig]) capKeys[sig] = newKey();
          fb.clear();
          busy(capBtn, true, "Collecting…");
          api("POST", "/authorizations/" + encodeURIComponent(a.authorization_id) + "/capture", { body: bodyObj, key: capKeys[sig] }).then(function (res) {
            busy(capBtn, false);
            after(res, function (p) { return "Collected " + fmtMoney(p.amount) + " from @" + p.from_handle + "."; }, "We couldn't confirm this collection.");
          });
        });
        actions = h("div", { class: "actions" },
          h("div", { class: "field" }, h("label", { for: capInput.id, text: "Amount to collect" }), capInput),
          h("label", { class: "check", for: keep.id }, keep, "Keep the rest on hold"),
          capBtn);
      } else if (open && outgoing) {
        var voidBtn = h("button", { type: "button", class: "btn btn-danger btn-sm", testid: "authorization-void-" + a.authorization_id }, "Release hold");
        voidBtn.addEventListener("click", function () {
          fb.clear();
          busy(voidBtn, true, "Releasing…");
          api("POST", "/authorizations/" + encodeURIComponent(a.authorization_id) + "/void").then(function (res) {
            busy(voidBtn, false);
            after(res, function (x) { return "Released the hold for @" + x.to_handle + "."; }, "We couldn't confirm the release.");
          });
        });
        actions = h("div", { class: "actions" }, voidBtn);
      }
      var labels = { open: "On hold", captured: "Collected", voided: "Released", expired: "Expired" };
      var detail = [];
      if (a.captured_amount > 0) detail.push(h("span", null, "Collected so far ", h("strong", { text: fmtMoney(a.captured_amount) })));
      if (open && a.captured_amount > 0) detail.push(h("span", { text: "Still held " + fmtMoney(remaining) }));
      return h("li", { class: "item", testid: "authorization-item-" + a.authorization_id, "data-status": a.status },
        h("div", null,
          h("div", { class: "dir", text: outgoing ? "You're holding for" : "Held for you by" }),
          h("div", { class: "who", text: "@" + (outgoing ? a.to_handle : a.from_handle) }),
          h("div", { class: "note", text: a.note }),
          h("div", { class: "meta" },
            h("span", { class: "badge badge-" + a.visibility, text: a.visibility === "private" ? "Private" : "Public" }),
            h("span", { title: fmtTime(a.expires_at) }, open ? "Expires " : "Expiry ",
              h("span", { class: "expires", testid: "authorization-expires-" + a.authorization_id, text: a.expires_at }))),
          detail.length ? h("div", { class: "meta" }, detail) : null),
        h("div", { class: "side" },
          h("span", { class: "amt", testid: "authorization-amount-" + a.authorization_id, text: fmtMoney(a.amount) }),
          h("span", { class: "badge badge-" + a.status, text: labels[a.status] || a.status }),
          a.status === "captured"
            ? h("span", { class: "meta" }, "Collected ", h("span", { testid: "authorization-captured-" + a.authorization_id, text: fmtMoney(a.captured_amount) }))
            : null),
        actions);
    }

    function render(items) {
      if (!items.length) {
        replace(body, h("section", { class: "card" }, h("div", { class: "empty", testid: "empty-authorizations" },
          h("strong", { text: "No holds" }), "When you hold money for someone, or someone holds money for you, it shows up here.")));
        return;
      }
      var list = h("ul", { class: "list", testid: "authorization-list" });
      items.forEach(function (a) { list.appendChild(item(a)); });
      replace(body, h("section", { class: "card", "aria-label": "Holds" },
        h("div", { class: "section-title" }, h("h2", { text: "Your holds" }), h("span", { class: "count", text: items.length + (items.length === 1 ? " hold" : " holds") })),
        list));
    }

    replace(main(), h("div", null,
      pageHead("Holds", "Money reserved now and collected later."),
      h("div", { class: "grid grid-main" },
        h("div", { class: "stack" }, walletCard(false), authorizeForm(load)),
        h("div", { class: "stack" }, fb.el, body))));
    load();
    refreshWallet();
  }

  // -- auth screens -----------------------------------------------------------

  function authScreen(mode) {
    var isSignup = mode === "signup";
    var email = h("input", { type: "email", testid: isSignup ? "signup-email" : "login-email", autocomplete: "email", autocapitalize: "none", spellcheck: "false" });
    var password = h("input", { type: "password", testid: isSignup ? "signup-password" : "login-password", autocomplete: isSignup ? "new-password" : "current-password" });
    var display = isSignup ? h("input", { type: "text", testid: "signup-display-name", autocomplete: "name" }) : null;
    var submit = h("button", { type: "submit", class: "btn btn-primary", testid: isSignup ? "signup-submit" : "login-submit" }, isSignup ? "Create account" : "Log in");
    var errorSlot = h("div", { "aria-live": "assertive" });
    var form = h("form", { novalidate: true },
      field(isSignup ? "signup-email" : "login-email", "Email", email),
      field(isSignup ? "signup-password" : "login-password", "Password", password, isSignup ? "At least 8 characters." : null),
      isSignup ? field("signup-display-name", "Your name", display, "Shown to people you pay.") : null,
      errorSlot, submit);
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      clear(errorSlot);
      var body = isSignup
        ? { email: email.value.trim(), password: password.value, display_name: display.value.trim() }
        : { email: email.value.trim(), password: password.value };
      busy(submit, true, isSignup ? "Creating account…" : "Logging in…");
      api("POST", isSignup ? "/auth/signup" : "/auth/login", { body: body, noAuthRedirect: true }).then(function (res) {
        busy(submit, false);
        if (res.kind === "ok" && res.data && res.data.token) {
          setToken(res.data.token);
          window.location.assign("/");
          return;
        }
        var text;
        if (res.kind === "uncertain") text = "We couldn't reach Pocketful. Check your connection and try again.";
        else if (isSignup && res.data && res.data.error && res.data.error.code === "validation_failed")
          text = "Use a valid email address, a password of at least 8 characters, and your name.";
        else text = errorText(res);
        replace(errorSlot, h("div", { class: "msg msg-error", role: "alert", testid: "auth-error", text: text }));
      });
    });
    replace(main(), h("div", { class: "auth-wrap" },
      h("section", { class: "card" },
        h("h1", { text: isSignup ? "Create your wallet" : "Welcome back" }),
        h("p", { class: "sub", text: isSignup ? "Send, request and split money with people you know." : "Log in to see your balance and activity." }),
        form,
        h("p", { class: "auth-alt" }, isSignup ? "Already have an account? " : "New to Pocketful? ",
          h("a", { href: isSignup ? "/login" : "/signup", text: isSignup ? "Log in" : "Create an account" })))));
  }

  // ---------------------------------------------------------------------------
  // boot

  var SCREENS = {
    "/": homeScreen,
    "/requests": requestsScreen,
    "/split": splitScreen,
    "/authorizations": authorizationsScreen
  };

  function boot() {
    var path = window.location.pathname.replace(/\/+$/, "") || "/";
    var isAuthPage = path === "/login" || path === "/signup";
    function start() {
      renderTopbar();
      if (isAuthPage) { authScreen(path === "/signup" ? "signup" : "login"); return; }
      if (!session.me) { window.location.replace("/login"); return; }
      var screen = SCREENS[path];
      if (!screen) {
        replace(main(), errorState("This page doesn't exist.", null));
        return;
      }
      screen();
    }
    if (!session.token) { start(); return; }
    api("GET", "/me", { noAuthRedirect: true }).then(function (res) {
      if (res.kind === "ok") {
        session.me = res.data;
        start();
      } else if (res.kind === "error" && res.status === 401) {
        setToken(null);
        start();
      } else {
        renderTopbar();
        replace(main(), errorState("We couldn't reach Pocketful right now.", function () { window.location.reload(); }));
      }
    });
  }

  boot();
})();
