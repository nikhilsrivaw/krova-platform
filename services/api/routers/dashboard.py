"""
The ledger screen.

What a business opens Krova to see: what has been promised, by whom, and what
is late. Their accounts will tell them weeks from now. The conversations
already know.

Three decisions shape this page.

Overdue first, not newest first. A message list sorts by recency; a ledger
sorts by what needs attention. The oldest unpaid promise is the most urgent
thing on the screen, and it should never be three scrolls down.

Every figure opens its evidence. Click any promise and the actual messages
appear - who said it, when, on which channel. For a product that tells people
things about their money, showing the working is the reason they believe the
number.

Uncertain extractions are quarantined. Anything the model was unsure about
sits in its own section, counts toward no total, and waits for a yes or no.
Presenting a guess as a fact once would make every other number suspect.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dashboard"])


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ledger &mdash; Krova</title>
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
    padding:18px 28px; border-bottom:1px solid var(--line);
  }
  .mark {
    width:28px; height:28px; border-radius:7px; flex:none;
    background:linear-gradient(135deg,var(--teal),var(--teal-deep));
    display:grid; place-items:center; font-weight:700; font-size:15px; color:#0B0B0F;
  }
  .wordmark { font-weight:600; letter-spacing:.14em; font-size:14px; }
  nav { margin-left:24px; display:flex; gap:18px; }
  nav a { color:var(--dim); text-decoration:none; font-size:14px; }
  nav a.on, nav a:hover { color:var(--text); }
  header .who { margin-left:auto; font-size:13px; color:var(--dim); }
  header .who a { color:var(--teal); text-decoration:none; margin-left:10px; }

  main { max-width:940px; margin:0 auto; padding:36px 24px 96px; }
  h1 { font-size:24px; margin:0 0 4px; font-weight:600; }
  .lede { color:var(--dim); margin:0 0 26px; font-size:14px; }

  .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:26px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:18px 20px; }
  .stat .k {
    font:600 10px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
    letter-spacing:.16em; text-transform:uppercase; color:var(--dim); margin-bottom:10px;
  }
  .stat .v { font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }
  .stat .s { font-size:12.5px; color:var(--dim); margin-top:4px; }
  .stat.alert { border-color:rgba(248,113,113,.35); }
  .stat.alert .v { color:var(--red); }

  .section-head { display:flex; align-items:baseline; gap:12px; margin:30px 0 12px; }
  .section-head h2 { font-size:15px; font-weight:600; margin:0; }
  .section-head .count { font-size:13px; color:var(--dim); }
  .filters { margin-left:auto; display:flex; gap:6px; }
  .filters button {
    font:500 12.5px/1 inherit; padding:6px 11px; border-radius:999px; cursor:pointer;
    background:transparent; border:1px solid var(--line-lit); color:var(--dim);
  }
  .filters button.on { background:var(--card); border-color:var(--teal-deep); color:var(--teal); }

  .row {
    background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:15px 18px; margin-bottom:9px; cursor:pointer; transition:border-color .12s;
  }
  .row:hover { border-color:var(--line-lit); }
  .row.open { border-color:var(--teal-deep); }
  .row .top { display:flex; align-items:baseline; gap:12px; }
  .row .desc { font-weight:500; flex:1; min-width:0; }
  .row .amt { font-variant-numeric:tabular-nums; font-weight:600; white-space:nowrap; }
  .row .sub { font-size:12.5px; color:var(--dim); margin-top:5px; display:flex; gap:10px; flex-wrap:wrap; }
  .tag {
    font:600 10px/1 ui-monospace,monospace; letter-spacing:.06em; text-transform:uppercase;
    padding:4px 7px; border-radius:4px; white-space:nowrap;
  }
  .tag.owed  { background:rgba(94,234,212,.12); color:var(--teal); }
  .tag.owing { background:rgba(251,191,36,.12); color:var(--amber); }
  .tag.late  { background:rgba(248,113,113,.14); color:var(--red); }
  .tag.maybe { background:#1A1A21; color:var(--dim); }

  .evidence { margin-top:14px; padding-top:14px; border-top:1px solid var(--line); }
  .evidence .eyebrow {
    font:600 10px/1 ui-monospace,monospace; letter-spacing:.16em;
    text-transform:uppercase; color:var(--teal); margin-bottom:12px;
  }
  .ev { display:flex; gap:10px; margin-bottom:10px; font-size:13.5px; }
  .ev .chan {
    font:600 9px/1 ui-monospace,monospace; letter-spacing:.06em; text-transform:uppercase;
    padding:4px 6px; border-radius:4px; background:#0E0E13; color:var(--dim);
    border:1px solid var(--line-lit); height:fit-content; white-space:nowrap;
  }
  .ev .txt { flex:1; min-width:0; }
  .ev .when { font-size:11.5px; color:var(--dim); margin-top:2px; }
  .actions { display:flex; gap:8px; margin-top:14px; flex-wrap:wrap; }
  .actions button {
    font:500 13px/1 inherit; padding:8px 13px; border-radius:7px; cursor:pointer;
    background:transparent; border:1px solid var(--line-lit); color:var(--dim);
  }
  .actions button:hover { color:var(--text); background:#1A1A21; }
  .actions button.primary { background:var(--teal); color:#08211D; border-color:var(--teal); font-weight:600; }

  .empty { color:var(--dim); font-size:14px; padding:26px 0; }
  .msg { padding:12px 14px; border-radius:8px; font-size:14px; margin-bottom:18px; }
  .msg.err { background:rgba(248,113,113,.1); border:1px solid rgba(248,113,113,.3); color:#FCA5A5; }

  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:24px; }
  label { display:block; font-size:13px; color:var(--dim); margin-bottom:6px; }
  input {
    width:100%; padding:11px 13px; margin-bottom:14px; background:#0E0E13;
    border:1px solid var(--line-lit); border-radius:8px; color:var(--text);
    font-size:15px; font-family:inherit;
  }
  input:focus { outline:none; border-color:var(--teal-deep); }
  button.signin { font:inherit; font-weight:600; cursor:pointer; padding:12px 20px;
    border-radius:8px; border:0; background:var(--teal); color:#08211D; }
  [hidden] { display:none !important; }
</style>
</head>
<body>

<header>
  <span class="mark">K</span>
  <span class="wordmark">KROVA</span>
  <nav id="nav" hidden>
    <a href="/ledger" class="on">Ledger</a>
    <a href="/onboarding">Channels</a>
  </nav>
  <span class="who" id="who" hidden><span id="who-email"></span><a href="#" id="signout">Sign out</a></span>
</header>

<main>
  <div id="alert" class="msg err" hidden></div>

  <section id="step-auth">
    <h1>Sign in</h1>
    <p class="lede">Your ledger is private to your business.</p>
    <div class="card">
      <label for="email">Work email</label>
      <input id="email" type="email" autocomplete="username" placeholder="you@company.com">
      <label for="password">Password</label>
      <input id="password" type="password" autocomplete="current-password" placeholder="Your password">
      <button class="signin" id="btn-signin">Sign in</button>
    </div>
  </section>

  <section id="step-ledger" hidden>
    <h1>Your ledger</h1>
    <p class="lede">Promises found in your conversations. Every figure opens the messages it came from.</p>

    <div class="stats">
      <div class="stat"><div class="k">Owed to you</div><div class="v" id="s-owed">&mdash;</div>
        <div class="s" id="s-owed-sub">&nbsp;</div></div>
      <div class="stat" id="s-late-card"><div class="k">Overdue</div><div class="v" id="s-late">&mdash;</div>
        <div class="s" id="s-late-sub">&nbsp;</div></div>
      <div class="stat"><div class="k">You promised</div><div class="v" id="s-owing">&mdash;</div>
        <div class="s" id="s-owing-sub">&nbsp;</div></div>
      <div class="stat"><div class="k">Needs your review</div><div class="v" id="s-maybe">&mdash;</div>
        <div class="s">Not counted in any total</div></div>
    </div>

    <div class="section-head">
      <h2>Promises</h2><span class="count" id="count"></span>
      <span class="filters">
        <button data-f="all" class="on">All</button>
        <button data-f="overdue">Overdue</button>
        <button data-f="they_owe">Owed to you</button>
        <button data-f="we_owe">You promised</button>
        <button data-f="unconfirmed">Unconfirmed</button>
      </span>
    </div>

    <div id="rows"></div>
    <div class="empty" id="empty" hidden>
      Nothing yet. Krova reads your conversations as they arrive &mdash; promises
      will appear here as they are made.
    </div>
  </section>
</main>

<script>
let token = null;
let filter = "all";
let openRow = null;

const $ = (id) => document.getElementById(id);
const rupees = (p) => p === null || p === undefined ? null : "\\u20b9" + Number(p / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });

function say(text) {
  const el = $("alert");
  if (!text) { el.hidden = true; return; }
  el.textContent = text; el.hidden = false;
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

async function signIn() {
  const btn = $("btn-signin");
  const email = $("email").value.trim(), password = $("password").value;
  if (!email || !password) { say("Enter your email and password."); return; }
  btn.disabled = true; say("");
  try {
    const d = await api("/api/v1/auth/login", {
      method: "POST", body: JSON.stringify({ email: email, password: password }) });
    token = d.access_token;
    $("who-email").textContent = d.email;
    $("who").hidden = false; $("nav").hidden = false;
    $("step-auth").hidden = true; $("step-ledger").hidden = false;
    await refresh();
  } catch (e) { say(e.message); } finally { btn.disabled = false; }
}

async function refresh() {
  const [summary, items] = await Promise.all([
    api("/api/v1/ledger/summary"),
    api("/api/v1/ledger/commitments?limit=200"),
  ]);

  $("s-owed").textContent = rupees(summary.owed_to_us_paise);
  $("s-owed-sub").textContent = summary.open_count + " open promise" + (summary.open_count === 1 ? "" : "s");
  $("s-late").textContent = rupees(summary.overdue_paise);
  $("s-late-sub").textContent = summary.overdue_count + " past due";
  $("s-late-card").className = "stat" + (summary.overdue_count > 0 ? " alert" : "");
  $("s-owing").textContent = rupees(summary.owed_by_us_paise);
  $("s-owing-sub").textContent = "what you owe others";
  $("s-maybe").textContent = summary.unconfirmed_count;

  render(items);
}

function visible(items) {
  if (filter === "all") return items.filter(function (c) { return c.status !== "unconfirmed"; });
  if (filter === "overdue") return items.filter(function (c) { return c.overdue; });
  if (filter === "unconfirmed") return items.filter(function (c) { return c.status === "unconfirmed"; });
  return items.filter(function (c) { return c.direction === filter && c.status !== "unconfirmed"; });
}

function render(items) {
  const list = visible(items);
  $("count").textContent = list.length ? list.length + " shown" : "";
  const box = $("rows");
  box.innerHTML = "";
  $("empty").hidden = list.length > 0;

  for (const c of list) {
    const row = document.createElement("div");
    row.className = "row";

    const top = document.createElement("div");
    top.className = "top";
    const desc = document.createElement("span");
    desc.className = "desc"; desc.textContent = c.description;
    const amt = document.createElement("span");
    amt.className = "amt"; amt.textContent = c.amount_display || "";
    top.appendChild(desc); top.appendChild(amt);

    const sub = document.createElement("div");
    sub.className = "sub";
    const tag = document.createElement("span");
    if (c.status === "unconfirmed") { tag.className = "tag maybe"; tag.textContent = "Unconfirmed"; }
    else if (c.overdue) { tag.className = "tag late"; tag.textContent = "Overdue"; }
    else if (c.direction === "they_owe") { tag.className = "tag owed"; tag.textContent = "Owed to you"; }
    else { tag.className = "tag owing"; tag.textContent = "You promised"; }
    sub.appendChild(tag);

    const who = document.createElement("span");
    who.textContent = c.customer_name || "Unknown contact";
    sub.appendChild(who);

    if (c.due_at) {
      const due = document.createElement("span");
      const d = new Date(c.due_at);
      due.textContent = "due " + d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
      sub.appendChild(due);
    }

    row.appendChild(top); row.appendChild(sub);
    row.addEventListener("click", function () { toggle(row, c); });
    box.appendChild(row);
  }
}

async function toggle(row, c) {
  if (openRow && openRow !== row) {
    const old = openRow.querySelector(".evidence");
    if (old) old.remove();
    openRow.classList.remove("open");
  }
  const existing = row.querySelector(".evidence");
  if (existing) { existing.remove(); row.classList.remove("open"); openRow = null; return; }

  row.classList.add("open"); openRow = row;
  const panel = document.createElement("div");
  panel.className = "evidence";
  panel.textContent = "Loading the messages\\u2026";
  row.appendChild(panel);

  try {
    const d = await api("/api/v1/ledger/commitments/" + c.id);
    panel.innerHTML = "";
    const eyebrow = document.createElement("div");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "Read from " + d.evidence.length + " message"
      + (d.evidence.length === 1 ? "" : "s");
    panel.appendChild(eyebrow);

    for (const e of d.evidence) {
      const ev = document.createElement("div"); ev.className = "ev";
      const ch = document.createElement("span"); ch.className = "chan"; ch.textContent = e.channel;
      const tx = document.createElement("div"); tx.className = "txt";
      const body = document.createElement("div"); body.textContent = e.text || "(no text)";
      const when = document.createElement("div"); when.className = "when";
      when.textContent = (e.direction === "inbound" ? "They wrote" : "You wrote") + " \\u00b7 "
        + new Date(e.occurred_at).toLocaleString("en-IN",
            { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      tx.appendChild(body); tx.appendChild(when);
      ev.appendChild(ch); ev.appendChild(tx);
      panel.appendChild(ev);
    }

    const actions = document.createElement("div"); actions.className = "actions";
    if (c.status === "unconfirmed") {
      actions.appendChild(button("Yes, this is real", "primary", async function () {
        await api("/api/v1/ledger/commitments/" + c.id + "/confirm", { method: "POST" });
        await refresh();
      }));
      actions.appendChild(button("Not a promise", "", async function () {
        await api("/api/v1/ledger/commitments/" + c.id + "/resolve",
          { method: "POST", body: JSON.stringify({ outcome: "cancelled" }) });
        await refresh();
      }));
    } else {
      actions.appendChild(button("Mark done", "primary", async function () {
        await api("/api/v1/ledger/commitments/" + c.id + "/resolve",
          { method: "POST", body: JSON.stringify({ outcome: "met" }) });
        await refresh();
      }));
      actions.appendChild(button("Didn't happen", "", async function () {
        await api("/api/v1/ledger/commitments/" + c.id + "/resolve",
          { method: "POST", body: JSON.stringify({ outcome: "missed" }) });
        await refresh();
      }));
    }
    panel.appendChild(actions);
  } catch (e) {
    panel.textContent = e.message;
  }
}

function button(label, cls, handler) {
  const b = document.createElement("button");
  b.textContent = label; if (cls) b.className = cls;
  b.addEventListener("click", async function (ev) {
    ev.stopPropagation();
    b.disabled = true;
    try { await handler(); } catch (err) { say(err.message); b.disabled = false; }
  });
  return b;
}

for (const b of document.querySelectorAll(".filters button")) {
  b.addEventListener("click", async function () {
    for (const o of document.querySelectorAll(".filters button")) o.classList.remove("on");
    b.classList.add("on"); filter = b.dataset.f; openRow = null;
    await refresh();
  });
}

$("btn-signin").addEventListener("click", signIn);
$("password").addEventListener("keydown", function (e) { if (e.key === "Enter") signIn(); });
$("signout").addEventListener("click", function (e) {
  e.preventDefault(); token = null;
  $("who").hidden = true; $("nav").hidden = true;
  $("step-ledger").hidden = true; $("step-auth").hidden = false;
});
</script>

</body>
</html>
"""


@router.get("/ledger", response_class=HTMLResponse, include_in_schema=False)
async def ledger_page() -> HTMLResponse:
    """The ledger screen."""
    return HTMLResponse(_PAGE)
