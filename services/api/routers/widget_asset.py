"""
The embeddable widget's own JS bundle - served directly by the API at a
short, memorable path (/widget.js, not under /api/v1), the same reasoning
onboarding.py's own docstring gives for serving its page the same way:
"a short path is what gets pasted into their settings," here a clinic's
own <script> tag.

A single, dependency-free vanilla-JS file rather than a bundled React
widget - this codebase has no frontend build pipeline for a standalone
embeddable asset (krova-web's own build only ever produces the dashboard
app), and every other "the API server serves a raw asset" precedent here
(onboarding.py) is a hand-written string for the same reason: one file,
no build step, works on any third-party page with zero assumptions about
what else that page is running. Shadow DOM keeps the widget's own styles
from colliding with (or being overridden by) the host site's CSS - the
same isolation Intercom/Chatbase-shaped widgets rely on.

No CORS handling needed here specifically - a plain <script src> load is
not subject to CORS the way the widget's own fetch() calls to
/api/v1/widget/{site_key}/message are (see widget_cors.py for those).
"""

from fastapi import APIRouter, Response

router = APIRouter(tags=["widget-asset"])


_WIDGET_JS = r"""
(function () {
  "use strict";
  var script = document.currentScript;
  var siteKey = script.getAttribute("data-site-key");
  var apiBase = (script.getAttribute("data-api-base") || "https://api.krova.space").replace(/\/$/, "");
  var accent = script.getAttribute("data-accent") || "#d4a24c";

  if (!siteKey) {
    console.error("Krova widget: data-site-key attribute is required on the script tag");
    return;
  }

  var STORAGE_KEY = "krova_widget_session_" + siteKey;
  var CONSENT_KEY = "krova_widget_consent_" + siteKey;
  var TEAM_KEY = "krova_widget_team_" + siteKey;
  var AFTER_KEY = "krova_widget_after_" + siteKey;
  var TEASER_KEY = "krova_widget_teaser_" + siteKey;
  var widgetBase = apiBase + "/api/v1/widget/" + encodeURIComponent(siteKey);

  function getItem(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function setItem(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private browsing - fine to lose it */ }
  }
  function getSessionToken() { return getItem(STORAGE_KEY); }
  function setSessionToken(token) { if (token) setItem(STORAGE_KEY, token); }
  function hasConsented() { return getItem(CONSENT_KEY) === "1"; }
  function setConsented() { setItem(CONSENT_KEY, "1"); }

  var WORDS = {
    en: {
      notice: "This chat is answered by an AI assistant using this business's own information. By continuing, you agree to share your messages (and, if you choose to book something, your contact details) with them.",
      start: "Start chat", type: "Type a message...", send: "Send", name: "Your name", phone: "Phone number",
      go: "Continue", team: "Team", busy: "We're getting a lot of messages right now - please try again in a minute.",
      failed: "Sorry, something went wrong. Please try again.", withPerson: "Sent to our team - they will reply here.",
      contactFirst: "Please share your name and phone number to start.", badPhone: "Please enter a valid phone number.",
      shared: "Shared my contact details", whatsapp: "WhatsApp", close: "Close chat", open: "Open chat",
      powered: "Powered by KROVA"
    },
    hi: {
      notice: "इस चैट में जवाब एक AI असिस्टेंट देता है, जो इस बिज़नेस की अपनी जानकारी से बात करता है। आगे बढ़कर आप अपने मैसेज (और बुकिंग करने पर अपना संपर्क) उनके साथ साझा करने के लिए सहमत होते हैं।",
      start: "चैट शुरू करें", type: "अपना मैसेज लिखें...", send: "भेजें", name: "आपका नाम", phone: "फ़ोन नंबर",
      go: "आगे बढ़ें", team: "टीम", busy: "अभी बहुत मैसेज आ रहे हैं - एक मिनट बाद फिर कोशिश करें।",
      failed: "माफ़ कीजिए, कुछ गड़बड़ हो गई। फिर से कोशिश करें।", withPerson: "आपका मैसेज हमारी टीम को भेज दिया गया है - वे यहीं जवाब देंगे।",
      contactFirst: "शुरू करने के लिए अपना नाम और फ़ोन नंबर बताइए।", badPhone: "कृपया सही फ़ोन नंबर डालें।",
      shared: "अपना संपर्क साझा किया", whatsapp: "WhatsApp", close: "चैट बंद करें", open: "चैट खोलें",
      powered: "Powered by KROVA"
    }
  };

  // How the widget looks and behaves comes from the business's Website chat page. A 404 means it is switched off.
  fetch(widgetBase + "/config")
    .then(function (res) { return res.ok ? res.json() : null; })
    .then(function (look) { if (look) start(look); })
    .catch(function () { /* the site keeps working without the chat */ });

  function start(look) {
  if (!document.body) {
    document.addEventListener("DOMContentLoaded", function () { start(look); });
    return;
  }
  if (document.getElementById("krova-widget-host")) return;  // already on this page
  if (look.accent) accent = look.accent;
  var t = WORDS[look.language] || WORDS.en;
  var title = look.title || "Chat with us";
  var greeting = look.greeting || "Hi! How can I help you today?";
  var side = look.position === "left" ? "left" : "right";
  var light = look.theme === "light";
  var contactFirst = look.ask_contact === "before_chat";
  var starters = look.starters || [];

  var bg = light ? "#ffffff" : "#0f1117";
  var fg = light ? "#14151f" : "#f4f4f5";
  var soft = light ? "rgba(0,0,0,.06)" : "rgba(255,255,255,.08)";
  var line = light ? "rgba(0,0,0,.1)" : "rgba(255,255,255,.08)";
  var field = light ? "#f4f4f6" : "#0a0c11";
  var dim = light ? "#6b6b76" : "#9a9aa5";

  var host = document.createElement("div");
  host.id = "krova-widget-host";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  var style = document.createElement("style");
  style.textContent = [
    ":host, *{box-sizing:border-box;font-family:-apple-system,'Segoe UI',Inter,system-ui,sans-serif;}",
    ".launch{position:fixed;bottom:20px;" + side + ":20px;display:flex;align-items:center;gap:10px;z-index:2147483000;",
    "flex-direction:" + (side === "left" ? "row-reverse" : "row") + ";}",
    ".label{background:" + bg + ";color:" + fg + ";padding:8px 12px;border-radius:18px;font-size:13px;font-weight:600;",
    "box-shadow:0 4px 16px rgba(0,0,0,.2);cursor:pointer;border:1px solid " + line + ";}",
    ".bubble{position:relative;width:56px;height:56px;border-radius:50%;background:" + accent + ";",
    "box-shadow:0 4px 16px rgba(0,0,0,.25);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;}",
    ".bubble svg{width:26px;height:26px;fill:#14151F;}",
    ".dot{position:absolute;top:2px;right:2px;width:12px;height:12px;border-radius:50%;background:#ef4444;display:none;}",
    ".dot.on{display:block;}",
    ".teaser{position:fixed;bottom:88px;" + side + ":20px;max-width:260px;background:" + bg + ";color:" + fg + ";",
    "padding:12px 30px 12px 14px;border-radius:14px;font-size:13px;line-height:1.4;box-shadow:0 6px 24px rgba(0,0,0,.25);",
    "border:1px solid " + line + ";z-index:2147483000;display:none;cursor:pointer;}",
    ".teaser.on{display:block;}",
    ".teaser .x{position:absolute;top:4px;right:6px;background:none;border:none;color:" + dim + ";font-size:16px;cursor:pointer;}",
    ".panel{position:fixed;bottom:88px;" + side + ":20px;width:370px;max-width:calc(100vw - 32px);",
    "height:560px;max-height:calc(100vh - 120px);background:" + bg + ";color:" + fg + ";border-radius:16px;",
    "box-shadow:0 8px 32px rgba(0,0,0,.35);display:none;flex-direction:column;overflow:hidden;",
    "z-index:2147483001;border:1px solid " + line + ";}",
    ".panel.open{display:flex;}",
    "@media (max-width:480px){.panel{top:0;left:0;right:0;bottom:0;width:100vw;max-width:100vw;height:100%;max-height:100%;border-radius:0;}}",
    ".head{padding:12px 14px;background:" + accent + ";color:#14151F;font-weight:700;font-size:14px;display:flex;align-items:center;gap:8px;}",
    ".head .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
    ".head a,.head button{background:rgba(0,0,0,.12);color:#14151F;border:none;border-radius:14px;padding:5px 10px;",
    "font-size:12px;font-weight:700;cursor:pointer;text-decoration:none;}",
    ".messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;}",
    ".msg{max-width:85%;padding:8px 12px;border-radius:12px;font-size:13px;line-height:1.45;white-space:pre-wrap;word-wrap:break-word;}",
    ".msg a{color:inherit;text-decoration:underline;}",
    ".msg.user{align-self:flex-end;background:" + accent + ";color:#14151F;}",
    ".msg.bot{align-self:flex-start;background:" + soft + ";color:" + fg + ";}",
    ".msg.team{align-self:flex-start;background:" + soft + ";color:" + fg + ";border-left:3px solid " + accent + ";}",
    ".msg.team b{display:block;font-size:10px;opacity:.7;margin-bottom:2px;}",
    ".typing{align-self:flex-start;background:" + soft + ";border-radius:12px;padding:10px 12px;display:none;gap:4px;}",
    ".typing.on{display:flex;}",
    ".typing span{width:6px;height:6px;border-radius:50%;background:" + dim + ";animation:kb 1s infinite ease-in-out;}",
    ".typing span:nth-child(2){animation-delay:.15s}.typing span:nth-child(3){animation-delay:.3s}",
    "@keyframes kb{0%,80%,100%{opacity:.3;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}",
    ".chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 8px;}",
    ".chips button{border:1px solid " + accent + ";background:transparent;color:" + fg + ";border-radius:16px;padding:6px 10px;font-size:12px;cursor:pointer;text-align:left;}",
    ".contact{padding:10px 12px;background:" + soft + ";border-top:1px solid " + line + ";display:none;gap:6px;flex-direction:column;}",
    ".contact.open{display:flex;}",
    ".contact p{margin:0;font-size:12px;color:" + dim + ";}",
    ".contact .err{color:#ef4444;}",
    ".contact input{padding:9px 10px;border-radius:8px;border:1px solid " + line + ";background:" + field + ";color:" + fg + ";font-size:13px;}",
    ".contact button{padding:9px;border-radius:8px;border:none;background:" + accent + ";color:#14151F;font-weight:700;font-size:13px;cursor:pointer;}",
    ".inputRow{display:flex;gap:6px;padding:10px;border-top:1px solid " + line + ";}",
    ".inputRow input{flex:1;min-width:0;padding:10px 12px;border-radius:20px;border:1px solid " + line + ";",
    "background:" + field + ";color:" + fg + ";font-size:14px;outline:none;}",
    ".inputRow button{padding:9px 14px;border-radius:20px;border:none;background:" + accent + ";",
    "color:#14151F;font-weight:700;font-size:13px;cursor:pointer;}",
    ".inputRow button:disabled{opacity:.5;}",
    ".dim{color:" + dim + ";font-size:11px;padding:2px 4px;align-self:center;}",
    ".foot{font-size:10px;color:" + dim + ";text-align:center;padding:4px 0 8px;}",
    ".consentScreen{flex:1;display:flex;flex-direction:column;justify-content:center;padding:20px;gap:14px;text-align:center;}",
    ".consentScreen p{color:" + fg + ";opacity:.85;font-size:12.5px;line-height:1.5;margin:0;}",
    ".consentScreen button{padding:11px;border-radius:10px;border:none;background:" + accent + ";",
    "color:#14151F;font-weight:700;font-size:13px;cursor:pointer;}",
  ].join("");
  root.appendChild(style);

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  var launch = el("div", "launch");
  var bubble = el("button", "bubble");
  bubble.setAttribute("aria-label", t.open);
  bubble.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.03 2 11c0 2.4 1.05 4.57 2.77 6.17L4 22l5.06-2.53c.94.22 1.93.34 2.94.34 5.52 0 10-4.03 10-9s-4.48-9-10-9z"/></svg>';
  var dot = el("span", "dot");
  bubble.appendChild(dot);
  if (look.launcher_text) {
    var label = el("div", "label", look.launcher_text);
    label.addEventListener("click", function () { toggle(true); });
    launch.appendChild(label);
  }
  launch.appendChild(bubble);
  root.appendChild(launch);

  var teaser = el("div", "teaser");
  var teaserX = el("button", "x", "×");
  teaserX.setAttribute("aria-label", t.close);
  teaser.appendChild(el("span", "", look.teaser || ""));
  teaser.appendChild(teaserX);
  root.appendChild(teaser);

  var panel = el("div", "panel");
  var head = el("div", "head");
  head.appendChild(el("span", "t", title));
  if (look.whatsapp) {
    var wa = el("a", "", t.whatsapp);
    wa.href = "https://wa.me/" + look.whatsapp + "?text=" + encodeURIComponent("Hi! I came from your website.");
    wa.target = "_blank";
    wa.rel = "noopener";
    head.appendChild(wa);
  }
  var closeBtn = el("button", "", "×");
  closeBtn.setAttribute("aria-label", t.close);
  head.appendChild(closeBtn);
  panel.appendChild(head);

  var consentScreenEl = el("div", "consentScreen");
  consentScreenEl.appendChild(el("p", "", t.notice));
  var consentBtn = el("button", "", t.start);
  consentScreenEl.appendChild(consentBtn);
  panel.appendChild(consentScreenEl);

  var messagesEl = el("div", "messages");
  var typingEl = el("div", "typing");
  typingEl.innerHTML = "<span></span><span></span><span></span>";
  panel.appendChild(messagesEl);
  var chipsEl = el("div", "chips");
  panel.appendChild(chipsEl);

  var contactEl = el("div", "contact");
  var contactNote = el("p", "", "");
  var cname = el("input", "");
  cname.type = "text"; cname.placeholder = t.name; cname.autocomplete = "name";
  var cphone = el("input", "");
  cphone.type = "tel"; cphone.placeholder = t.phone; cphone.autocomplete = "tel";
  var csubmit = el("button", "", t.go);
  contactEl.appendChild(contactNote);
  contactEl.appendChild(cname);
  contactEl.appendChild(cphone);
  contactEl.appendChild(csubmit);
  panel.appendChild(contactEl);

  var inputRow = el("div", "inputRow");
  var textInput = el("input", "");
  textInput.type = "text"; textInput.placeholder = t.type;
  var sendBtn = el("button", "", t.send);
  inputRow.appendChild(textInput);
  inputRow.appendChild(sendBtn);
  panel.appendChild(inputRow);
  panel.appendChild(el("div", "foot", t.powered));
  root.appendChild(panel);

  var isOpen = false;
  var started = false;
  var userSaid = 0;
  var lastAsked = "";
  var toldWithPerson = false;
  var shownReplies = {};

  function teamReady() { return getItem(TEAM_KEY) === "1"; }

  // Links in a reply (a payment link, a map) open in a new tab; everything else stays plain text.
  function fill(node, text) {
    var parts = String(text).split(/(https?:\/\/[^\s<>"]+)/g);
    parts.forEach(function (part) {
      if (/^https?:\/\//.test(part)) {
        var a = document.createElement("a");
        a.href = part; a.target = "_blank"; a.rel = "noopener nofollow";
        a.textContent = part;
        node.appendChild(a);
      } else if (part) {
        node.appendChild(document.createTextNode(part));
      }
    });
  }

  function addMessage(role, text) {
    if (!text) return;
    var div = el("div", "msg " + (role === "user" ? "user" : role === "team" ? "team" : "bot"));
    if (role === "team") div.appendChild(el("b", "", t.team));
    fill(div, text);
    messagesEl.insertBefore(div, typingEl.parentNode === messagesEl ? typingEl : null);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  function addNote(text) {
    var node = el("div", "dim", text);
    messagesEl.appendChild(node);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
  function setTyping(on) {
    if (on) messagesEl.appendChild(typingEl);
    typingEl.classList.toggle("on", on);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderChips() {
    chipsEl.innerHTML = "";
    if (userSaid > 0 || !starters.length || contactEl.classList.contains("open")) return;
    starters.forEach(function (q) {
      var b = el("button", "", q);
      b.addEventListener("click", function () { submit(q); });
      chipsEl.appendChild(b);
    });
  }

  function showContact(note, isError) {
    contactNote.textContent = note || "";
    contactNote.className = isError ? "err" : "";
    contactEl.classList.add("open");
    if (contactFirst && !teamReady()) inputRow.style.display = "none";
    renderChips();
    cname.focus();
  }
  function hideContact() {
    contactEl.classList.remove("open");
    inputRow.style.display = "flex";
    renderChips();
  }

  function showConsentGate() {
    consentScreenEl.style.display = "flex";
    messagesEl.style.display = "none";
    inputRow.style.display = "none";
    chipsEl.style.display = "none";
  }
  function showChat() {
    consentScreenEl.style.display = "none";
    messagesEl.style.display = "flex";
    inputRow.style.display = "flex";
    chipsEl.style.display = "flex";
    if (!started) {
      started = true;
      if (!messagesEl.querySelector(".msg")) addMessage("bot", greeting);
      if (contactFirst && !teamReady()) showContact(t.contactFirst);
      renderChips();
    }
  }

  function toggle(open) {
    isOpen = open;
    panel.classList.toggle("open", isOpen);
    teaser.classList.remove("on");
    if (!isOpen) return;
    dot.classList.remove("on");
    if (!hasConsented()) showConsentGate(); else showChat();
    messagesEl.scrollTop = messagesEl.scrollHeight;
    poll();
    if (inputRow.style.display !== "none") textInput.focus();
  }
  bubble.addEventListener("click", function () { toggle(!isOpen); });
  closeBtn.addEventListener("click", function () { toggle(false); });
  consentBtn.addEventListener("click", function () { setConsented(); showChat(); });
  teaser.addEventListener("click", function (e) {
    if (e.target === teaserX) { teaser.classList.remove("on"); return; }
    toggle(true);
  });

  function post(path, payload) {
    return fetch(widgetBase + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function setLoading(loading) {
    sendBtn.disabled = loading;
    csubmit.disabled = loading;
    setTyping(loading);
  }

  function send(text, contact) {
    setLoading(true);
    return post("/message", {
      session_token: getSessionToken(), text: text, consent: hasConsented(),
      contact: contact || undefined, page_url: window.location.href,
      // The same question again, now with the visitor's details - not a new message.
      retry: !!contact,
    })
      .then(function (res) {
        if (res.status === 429) { addMessage("bot", t.busy); return null; }
        if (res.status === 422) { showContact(t.badPhone, true); return null; }
        if (!res.ok) throw new Error("request failed");
        return res.json();
      })
      .then(function (data) {
        if (!data) return data;
        setSessionToken(data.session_token);
        if (data.needs_consent) { showConsentGate(); return data; }
        if (data.team_can_reply) setItem(TEAM_KEY, "1");
        if (data.reply_text) addMessage("bot", data.reply_text);
        if (data.with_person && !toldWithPerson) { toldWithPerson = true; addNote(t.withPerson); }
        if (data.needs_contact) showContact(""); else hideContact();
        return data;
      })
      .catch(function () { addMessage("bot", t.failed); })
      .finally(function () { setLoading(false); });
  }

  function submit(text) {
    text = (text || "").trim();
    if (!text) return;
    userSaid += 1;
    lastAsked = text;
    addMessage("user", text);
    renderChips();
    send(text);
  }

  sendBtn.addEventListener("click", function () {
    var text = textInput.value;
    textInput.value = "";
    submit(text);
  });
  textInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendBtn.click();
  });

  csubmit.addEventListener("click", function () {
    var name = cname.value.trim();
    var phone = cphone.value.trim();
    if (phone.replace(/\D/g, "").length < 8) { showContact(t.badPhone, true); return; }
    var contact = { name: name || undefined, phone: phone };
    if (!lastAsked) {
      // Asked first, before any question: save the details, then the visitor types.
      setLoading(true);
      post("/contact", { session_token: getSessionToken(), consent: hasConsented(), contact: contact, page_url: window.location.href })
        .then(function (res) {
          if (res.status === 422) { showContact(t.badPhone, true); return null; }
          if (!res.ok) throw new Error("request failed");
          return res.json();
        })
        .then(function (data) {
          if (!data) return;
          setSessionToken(data.session_token);
          if (data.team_can_reply) setItem(TEAM_KEY, "1");
          addNote(t.shared);
          hideContact();
          textInput.focus();
        })
        .catch(function () { addMessage("bot", t.failed); })
        .finally(function () { setLoading(false); });
      return;
    }
    // Asked because a person or a booking needs them: send the last question again with the details.
    hideContact();
    addNote(t.shared);
    send(lastAsked, contact);
  });

  // A person's replies from the business's Inbox. Asked for only once the visitor has left a number (before
  // that nobody can reply), every few seconds while the chat is open and now and then while it is closed.
  var polling = false;
  function poll() {
    var token = getSessionToken();
    if (polling || !token || !teamReady()) return;
    polling = true;
    var url = widgetBase + "/updates?session_token=" + encodeURIComponent(token);
    var after = getItem(AFTER_KEY);
    if (after) url += "&after=" + encodeURIComponent(after);
    fetch(url)
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (!data || !data.replies) return;
        data.replies.forEach(function (r) {
          if (shownReplies[r.id]) return;
          shownReplies[r.id] = true;
          setItem(AFTER_KEY, r.at);
          if (!hasConsented()) return;
          addMessage("team", r.text);
          if (!isOpen) dot.classList.add("on");
        });
      })
      .catch(function () { /* try again on the next tick */ })
      .finally(function () { polling = false; });
  }
  var ticks = 0;
  setInterval(function () {
    ticks += 1;
    if (isOpen || ticks % 6 === 0) poll();
  }, 5000);

  // The chat so far - it carries on across pages of the site and after a reload.
  var token = getSessionToken();
  if (token && hasConsented()) {
    fetch(widgetBase + "/history?session_token=" + encodeURIComponent(token))
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (data) {
        if (!data || !data.messages || !data.messages.length) return;
        if (data.team_can_reply) setItem(TEAM_KEY, "1");
        if (!messagesEl.querySelector(".msg")) addMessage("bot", greeting);
        data.messages.forEach(function (m) {
          if (m.role === "user") { userSaid += 1; lastAsked = m.text; }
          if (m.id) shownReplies[m.id] = true;
          if (m.role === "team" && m.at) setItem(AFTER_KEY, m.at);
          addMessage(m.role, m.text);
        });
        renderChips();
      })
      .catch(function () { /* starts fresh */ })
      .finally(poll);
  }

  // The teaser: once a day, a few seconds after the page opens, for a visitor who has not chatted yet.
  if (look.teaser && !token) {
    var today = new Date().toISOString().slice(0, 10);
    if (getItem(TEASER_KEY) !== today) {
      setTimeout(function () {
        if (isOpen) return;
        setItem(TEASER_KEY, today);
        teaser.classList.add("on");
      }, 6000);
    }
  }
  }
})();
"""


@router.get("/widget.js")
async def widget_js() -> Response:
    # The charset matters: without it a browser reads the Hindi words and the close mark as Latin-1 on many sites.
    return Response(content=_WIDGET_JS, media_type="application/javascript; charset=utf-8")
