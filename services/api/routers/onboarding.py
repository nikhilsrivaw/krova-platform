"""
The screen a business uses to connect its channels.

Served by the API rather than a separate frontend, deliberately. Meta's
Embedded Signup requires the page that opens the dialog to sit on an
allowlisted HTTPS origin, and the code exchange must happen server-side. Same
origin means one domain in Meta's settings, no CORS, and no possibility of the
authorisation code taking a detour through another host.
"""

import json

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from shared.config.settings import settings

router = APIRouter(tags=["onboarding"])


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect your channels &mdash; Krova</title>
<style>
  :root {
    --bg:#0B0B0F; --card:#131318; --line:#24242D; --line-lit:#35353F;
    --text:#F4F4F5; --dim:#9A9AA5; --teal:#5EEAD4; --teal-deep:#00A387;
    --red:#F87171; --amber:#FBBF24;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--text);
    font:15px/1.6 -apple-system,"Segoe UI",Inter,system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  header {
    display:flex; align-items:center; gap:10px;
    padding:20px 28px; border-bottom:1px solid var(--line);
  }
  .mark {
    width:28px; height:28px; border-radius:7px; flex:none;
    background:linear-gradient(135deg,var(--teal),var(--teal-deep));
    display:grid; place-items:center; font-weight:700; font-size:15px; color:#0B0B0F;
  }
  .wordmark { font-weight:600; letter-spacing:.14em; font-size:14px; }
  header .who { margin-left:auto; font-size:13px; color:var(--dim); }
  header .who a { color:var(--teal); text-decoration:none; margin-left:10px; }
  main { max-width:760px; margin:0 auto; padding:44px 24px 96px; }
  h1 { font-size:26px; line-height:1.25; margin:0 0 10px; font-weight:600; }
  .lede { color:var(--dim); margin:0 0 28px; }
  .card {
    background:var(--card); border:1px solid var(--line);
    border-radius:12px; padding:24px; margin-bottom:18px;
  }
  label { display:block; font-size:13px; color:var(--dim); margin-bottom:6px; }
  input {
    width:100%; padding:11px 13px; margin-bottom:14px; background:#0E0E13;
    border:1px solid var(--line-lit); border-radius:8px; color:var(--text);
    font-size:15px; font-family:inherit;
  }
  input:focus { outline:none; border-color:var(--teal-deep); }
  button {
    font:inherit; font-weight:600; cursor:pointer; padding:12px 20px;
    border-radius:8px; border:0; background:var(--teal); color:#08211D;
  }
  button:hover:not(:disabled) { background:#7BF0DC; }
  button:disabled { opacity:.5; cursor:default; }
  button.ghost { background:transparent; color:var(--dim); border:1px solid var(--line-lit); }
  button.ghost:hover:not(:disabled) { background:#1A1A21; color:var(--text); }
  .eyebrow {
    font:600 10px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
    letter-spacing:.18em; text-transform:uppercase; color:var(--teal); margin-bottom:14px;
  }
  .chan { display:flex; align-items:center; gap:14px; }
  .chan .icon {
    width:38px; height:38px; border-radius:9px; flex:none; display:grid;
    place-items:center; font-size:18px; background:#0E0E13; border:1px solid var(--line-lit);
  }
  .chan .body { flex:1; min-width:0; }
  .chan .name { font-weight:600; }
  .chan .meta { font-size:13px; color:var(--dim); }
  .pill {
    font:600 11px/1 ui-monospace,monospace; letter-spacing:.06em;
    padding:5px 9px; border-radius:999px; white-space:nowrap;
  }
  .pill.on  { background:rgba(94,234,212,.12); color:var(--teal); }
  .pill.off { background:#1A1A21; color:var(--dim); }
  .pill.warn{ background:rgba(251,191,36,.12); color:var(--amber); }
  dl { display:grid; grid-template-columns:minmax(150px,auto) 1fr; gap:11px 20px; margin:0; }
  dt { color:var(--dim); font-size:13px; }
  dd { margin:0; font:13px ui-monospace,SFMono-Regular,Menlo,monospace; word-break:break-all; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th {
    text-align:left; color:var(--dim); font-weight:500; padding:0 10px 9px 0;
    border-bottom:1px solid var(--line); font-size:11px;
    letter-spacing:.08em; text-transform:uppercase;
  }
  td { padding:9px 10px 9px 0; border-bottom:1px solid var(--line);
       font:12.5px ui-monospace,SFMono-Regular,Menlo,monospace; }
  tr:last-child td { border-bottom:0; }
  .ok { color:var(--teal); } .bad { color:var(--red); } .none { color:var(--dim); }
  .msg { padding:12px 14px; border-radius:8px; font-size:14px; margin-bottom:18px; }
  .msg.err  { background:rgba(248,113,113,.1); border:1px solid rgba(248,113,113,.3); color:#FCA5A5; }
  .msg.info { background:rgba(94,234,212,.08); border:1px solid rgba(94,234,212,.25); color:var(--teal); }
  .note { color:var(--dim); font-size:13px; margin-top:16px; }
  .scroll { overflow-x:auto; }
  [hidden] { display:none !important; }
  a { color:var(--teal); }
</style>
</head>
<body>

<header>
  <span class="mark">K</span>
  <span class="wordmark">KROVA</span>
  <span class="who" id="who" hidden><span id="who-email"></span><a href="#" id="signout">Sign out</a></span>
</header>

<main>
  <div id="alert" class="msg" hidden></div>

  <!-- sign in -->
  <section id="step-auth">
    <h1>Sign in</h1>
    <p class="lede">Channels connect to your business, so we need to know who you are.</p>
    <div class="card">
      <label for="email">Work email</label>
      <input id="email" type="email" autocomplete="username" placeholder="you@company.com">
      <label for="password">Password</label>
      <input id="password" type="password" autocomplete="current-password" placeholder="Your password">
      <button id="btn-signin">Sign in</button>
    </div>
  </section>

  <!-- channels -->
  <section id="step-channels" hidden>
    <h1>Connect your channels</h1>
    <p class="lede">
      Krova reads and replies through accounts you already own. Nothing is
      shared, and you can disconnect any channel here at any time.
    </p>

    <div class="card">
      <div class="chan">
        <span class="icon">&#128172;</span>
        <span class="body">
          <span class="name">WhatsApp Business</span><br>
          <span class="meta" id="wa-meta">Not connected</span>
        </span>
        <span class="pill off" id="wa-pill">Not connected</span>
      </div>
      <p style="margin:18px 0 0;color:var(--dim);font-size:14px" id="wa-blurb">
        Meta opens its own window. You sign in with Facebook, choose your
        WhatsApp Business Account and the number to use. Krova never sees your
        Facebook password.
      </p>
      <div style="margin-top:18px;display:flex;gap:10px;flex-wrap:wrap">
        <button id="btn-wa">Connect WhatsApp</button>
        <button class="ghost" id="btn-wa-off" hidden>Disconnect</button>
      </div>
    </div>

    <div class="card">
      <div class="chan">
        <span class="icon">&#9993;</span>
        <span class="body">
          <span class="name">Email</span><br>
          <span class="meta" id="gm-meta">Not connected</span>
        </span>
        <span class="pill off" id="gm-pill">Not connected</span>
      </div>
      <p style="margin:18px 0 0;color:var(--dim);font-size:14px">
        Unlike WhatsApp, email has history. Connecting a mailbox lets Krova
        read the last 90 days and show you what was promised before you ever
        signed up.
      </p>
      <div style="margin-top:18px">
        <button class="ghost" id="btn-gm">Connect Gmail</button>
      </div>
    </div>

    <!-- what just happened -->
    <div class="card" id="calls-card" hidden>
      <div class="eyebrow">What Krova did to connect this account</div>
      <div class="scroll">
        <table>
          <thead><tr><th>Method</th><th>Endpoint</th><th>Permission</th><th>Result</th></tr></thead>
          <tbody id="calls"></tbody>
        </table>
      </div>
      <p class="note" id="calls-note"></p>
    </div>

    <div class="card" id="detail-card" hidden>
      <div class="eyebrow">Connected account</div>
      <dl>
        <dt>Business account</dt><dd id="d-waba-name">&mdash;</dd>
        <dt>WABA ID</dt><dd id="d-waba-id">&mdash;</dd>
        <dt>Phone number</dt><dd id="d-phone">&mdash;</dd>
        <dt>Display name</dt><dd id="d-verified">&mdash;</dd>
        <dt>Quality rating</dt><dd id="d-quality">&mdash;</dd>
        <dt>Receiving messages</dt><dd id="d-subscribed">&mdash;</dd>
      </dl>
    </div>
  </section>
</main>

<script>
const CFG = __CONFIG__;
let token = null;

const $ = (id) => document.getElementById(id);

function say(text, kind) {
  const el = $("alert");
  if (!text) { el.hidden = true; return; }
  el.className = "msg " + (kind || "info");
  el.textContent = text;
  el.hidden = false;
}

async function api(path, options) {
  const opts = options || {};
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, Object.assign({}, opts, { headers: headers }));
  const body = res.status === 204 ? {} : await res.json().catch(function () { return {}; });
  if (!res.ok) throw new Error(body.detail || ("Request failed (" + res.status + ")"));
  return body;
}

// ── sign in ─────────────────────────────────────────────────────────────────
async function signIn() {
  const btn = $("btn-signin");
  const email = $("email").value.trim();
  const password = $("password").value;
  if (!email || !password) { say("Enter your email and password.", "err"); return; }

  btn.disabled = true;
  say("");
  try {
    const data = await api("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email: email, password: password }),
    });
    token = data.access_token;
    $("who-email").textContent = data.email;
    $("who").hidden = false;
    $("step-auth").hidden = true;
    $("step-channels").hidden = false;
    await loadChannels();
  } catch (e) {
    say(e.message, "err");
  } finally {
    btn.disabled = false;
  }
}

// ── current state ───────────────────────────────────────────────────────────
async function loadChannels() {
  let channels = [];
  try { channels = await api("/api/v1/channels/"); } catch (e) { return; }

  const wa = channels.find(function (c) { return c.channel === "whatsapp" && c.status === "active"; });
  const gm = channels.find(function (c) { return c.channel === "email" && c.status === "active"; });

  if (wa) {
    $("wa-meta").textContent = (wa.handle || "") + (wa.display_name ? " \\u00b7 " + wa.display_name : "");
    const healthy = wa.webhook_subscribed && wa.number_registered;
    $("wa-pill").textContent = healthy ? "Connected" : "Needs attention";
    $("wa-pill").className = "pill " + (healthy ? "on" : "warn");
    $("btn-wa").textContent = "Reconnect";
    $("btn-wa-off").hidden = false;
    $("d-waba-name").textContent = wa.display_name || "\\u2014";
    $("d-waba-id").textContent = wa.waba_id || "\\u2014";
    $("d-phone").textContent = wa.handle || "\\u2014";
    $("d-verified").textContent = wa.verified_name || "\\u2014";
    $("d-quality").textContent = wa.quality_rating || "\\u2014";
    $("d-subscribed").textContent = wa.webhook_subscribed ? "Yes" : "No \\u2014 messages will not arrive";
    $("detail-card").hidden = false;
  } else {
    $("wa-pill").textContent = "Not connected";
    $("wa-pill").className = "pill off";
    $("btn-wa").textContent = "Connect WhatsApp";
    $("btn-wa-off").hidden = true;
  }

  if (gm) {
    $("gm-meta").textContent = gm.handle || "";
    $("gm-pill").textContent = "Connected";
    $("gm-pill").className = "pill on";
    $("btn-gm").textContent = "Reconnect Gmail";
  }
}

// ── Meta SDK ────────────────────────────────────────────────────────────────
window.fbAsyncInit = function () {
  FB.init({ appId: CFG.app_id, cookie: true, xfbml: false, version: CFG.graph_version });
};

// Meta reports progress back to the opener while its dialog runs.
window.addEventListener("message", function (event) {
  let host;
  try { host = new URL(event.origin).hostname; } catch (e) { return; }
  if (!/(^|\\.)facebook\\.com$/.test(host)) return;
  let payload;
  try { payload = JSON.parse(event.data); } catch (e) { return; }
  if (payload.type !== "WA_EMBEDDED_SIGNUP") return;
  if (payload.event === "CANCEL" || payload.event === "ERROR") {
    const detail = (payload.data && payload.data.error_message) || payload.event;
    say("Signup was not completed: " + detail, "err");
  }
});

function connectWhatsApp() {
  say("");
  if (typeof FB === "undefined") {
    say("Meta's SDK did not load. Check this domain is allowlisted in the app's Client OAuth settings.", "err");
    return;
  }
  FB.login(function (response) {
    const code = response && response.authResponse && response.authResponse.code;
    if (!code) { say("Signup was cancelled before access was granted.", "err"); return; }
    finishWhatsApp(code);
  }, {
    config_id: CFG.config_id,
    response_type: "code",
    override_default_response_type: true,
    extras: { setup: {}, sessionInfoVersion: "3" },
  });
}

async function finishWhatsApp(code) {
  $("btn-wa").disabled = true;
  say("Completing the connection\\u2026", "info");
  try {
    const data = await api("/api/v1/channels/whatsapp/embedded-signup", {
      method: "POST",
      body: JSON.stringify({ code: code }),
    });
    renderConnection(data);
    say("");
    await loadChannels();
  } catch (e) {
    say(e.message, "err");
  } finally {
    $("btn-wa").disabled = false;
  }
}

function renderConnection(d) {
  $("d-waba-name").textContent = d.waba_name || "\\u2014";
  $("d-waba-id").textContent = d.waba_id || "\\u2014";
  $("d-phone").textContent = d.display_phone_number || "\\u2014";
  $("d-verified").textContent = d.verified_name || "\\u2014";
  $("d-quality").textContent = d.quality_rating || "\\u2014";
  $("d-subscribed").textContent = d.webhook_subscribed
    ? "Yes" : "No \\u2014 messages will not arrive";
  $("detail-card").hidden = false;

  const body = $("calls");
  body.innerHTML = "";
  const calls = d.graph_calls || [];
  for (const c of calls) {
    const tr = document.createElement("tr");
    const hasPerm = c.permission && c.permission !== "-";
    const cells = [
      [c.method, ""],
      [c.path, ""],
      [hasPerm ? c.permission : "\\u2014", hasPerm ? "ok" : "none"],
      [String(c.status), c.status < 300 ? "ok" : "bad"],
    ];
    for (const pair of cells) {
      const td = document.createElement("td");
      td.textContent = pair[0];
      if (pair[1]) td.className = pair[1];
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  if (calls.length) {
    $("calls-note").textContent =
      "Krova subscribes to your account's messages and registers the number so it "
      + "can send. Both steps fail silently if skipped, so their result is recorded "
      + "rather than assumed.";
    $("calls-card").hidden = false;
  }
}

async function disconnectWhatsApp() {
  if (!window.confirm("Disconnect WhatsApp? Krova will stop receiving and sending on this number.")) return;
  $("btn-wa-off").disabled = true;
  try {
    await api("/api/v1/channels/whatsapp", { method: "DELETE" });
    $("detail-card").hidden = true;
    $("calls-card").hidden = true;
    say("WhatsApp disconnected. Krova no longer has access to that account.", "info");
    await loadChannels();
  } catch (e) {
    say(e.message, "err");
  } finally {
    $("btn-wa-off").disabled = false;
  }
}

async function connectGmail() {
  say("");
  try {
    const data = await api("/api/v1/channels/gmail/connect");
    window.location.href = data.authorize_url;
  } catch (e) {
    say(e.message, "err");
  }
}

$("btn-signin").addEventListener("click", signIn);
$("password").addEventListener("keydown", function (e) { if (e.key === "Enter") signIn(); });
$("btn-wa").addEventListener("click", connectWhatsApp);
$("btn-wa-off").addEventListener("click", disconnectWhatsApp);
$("btn-gm").addEventListener("click", connectGmail);
$("signout").addEventListener("click", function (e) {
  e.preventDefault();
  token = null;
  $("who").hidden = true;
  $("step-channels").hidden = true;
  $("step-auth").hidden = false;
  say("");
});
</script>
<script async defer crossorigin="anonymous" src="https://connect.facebook.net/en_US/sdk.js"></script>

</body>
</html>
"""


@router.get("/onboarding", response_class=HTMLResponse, include_in_schema=False)
async def onboarding_page() -> HTMLResponse:
    """The channel connection screen."""
    config = {
        "app_id": settings.meta_app_id,
        "config_id": settings.meta_config_id,
        "graph_version": settings.meta_api_version,
    }
    return HTMLResponse(_PAGE.replace("__CONFIG__", json.dumps(config)))
