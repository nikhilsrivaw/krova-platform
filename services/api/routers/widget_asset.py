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
      start: "Start chat", type: "Write a message...", name: "Your name", phone: "Phone number",
      go: "Continue", team: "Team", busy: "We're getting a lot of messages right now - please try again in a minute.",
      failed: "Sorry, something went wrong. Please try again.", withPerson: "Sent to our team - they will reply here.",
      contactFirst: "Share your name and number to start - we'll reply right here.",
      contactNeeded: "Share your name and number so we can help you.", badPhone: "Please enter a valid phone number.",
      shared: "Contact details shared", close: "Close chat", open: "Open chat", send: "Send",
      status: "Replies in seconds", powered: "Powered by KROVA", welcome: "Hi there"
    },
    hi: {
      notice: "इस चैट में जवाब एक AI असिस्टेंट देता है, जो इस बिज़नेस की अपनी जानकारी से बात करता है। आगे बढ़कर आप अपने मैसेज (और बुकिंग करने पर अपना संपर्क) उनके साथ साझा करने के लिए सहमत होते हैं।",
      start: "चैट शुरू करें", type: "अपना मैसेज लिखें...", name: "आपका नाम", phone: "फ़ोन नंबर",
      go: "आगे बढ़ें", team: "टीम", busy: "अभी बहुत मैसेज आ रहे हैं - एक मिनट बाद फिर कोशिश करें।",
      failed: "माफ़ कीजिए, कुछ गड़बड़ हो गई। फिर से कोशिश करें।", withPerson: "आपका मैसेज हमारी टीम को भेज दिया गया है - वे यहीं जवाब देंगे।",
      contactFirst: "शुरू करने के लिए अपना नाम और नंबर बताइए - हम यहीं जवाब देंगे।",
      contactNeeded: "अपना नाम और नंबर बताइए, ताकि हम आपकी मदद कर सकें।", badPhone: "कृपया सही फ़ोन नंबर डालें।",
      shared: "संपर्क साझा किया", close: "चैट बंद करें", open: "चैट खोलें", send: "भेजें",
      status: "तुरंत जवाब", powered: "Powered by KROVA", welcome: "नमस्ते"
    }
  };

  // Dark or light text on the business's colour, whichever reads better.
  function inkOn(hex) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return "#ffffff";
    var n = parseInt(m[1], 16);
    var c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(function (v) {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    var lum = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    return lum > 0.4 ? "#111318" : "#ffffff";
  }

  // The business's colour, darker by `amount` (0-1) - the header's gradient end.
  function shade(hex, amount) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return hex;
    var n = parseInt(m[1], 16);
    var parts = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map(function (v) {
      return ("0" + Math.round(v * (1 - amount)).toString(16)).slice(-2);
    });
    return "#" + parts.join("");
  }

  var ICON_CHAT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3C6.98 3 3 6.58 3 11c0 2.2 1 4.2 2.62 5.65L5 21l4.3-2.15c.86.2 1.76.3 2.7.3 5.02 0 9-3.58 9-8.15S17.02 3 12 3z"/></svg>';
  var ICON_CLOSE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19l5.6-5.6 5.6 5.6 1.4-1.4-5.6-5.6L19 6.4 17.6 5 12 10.6z"/></svg>';
  var ICON_DOWN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7.4 8.6 12 13.2l4.6-4.6L18 10l-6 6-6-6z"/></svg>';
  var ICON_SEND = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.4 20.4 21 12 3.4 3.6 3.4 10l12.6 2-12.6 2z"/></svg>';
  var ICON_WA = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12.04 2C6.58 2 2.13 6.45 2.13 11.91c0 1.75.46 3.45 1.32 4.95L2.05 22l5.25-1.38c1.45.79 3.08 1.21 4.74 1.21 5.46 0 9.91-4.45 9.91-9.91S17.5 2 12.04 2zm5.8 14.03c-.25.69-1.44 1.32-1.98 1.37-.51.05-.99.24-3.33-.69-2.81-1.11-4.6-3.98-4.74-4.17-.14-.18-1.13-1.5-1.13-2.87 0-1.36.72-2.03.97-2.31.25-.28.55-.35.74-.35l.53.01c.17.01.4-.06.62.48.25.6.83 2.06.9 2.21.07.15.12.32.02.51-.1.19-.15.31-.29.48-.15.17-.31.38-.44.51-.15.15-.3.31-.13.6.17.29.76 1.25 1.63 2.03 1.12 1 2.07 1.31 2.36 1.46.29.15.46.12.63-.07.17-.19.73-.85.92-1.14.19-.29.39-.24.65-.14.27.1 1.7.8 1.99.95.29.15.48.22.55.34.07.12.07.7-.18 1.39z"/></svg>';

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
  var ink = inkOn(accent);
  var t = WORDS[look.language] || WORDS.en;
  var title = look.title || "Chat with us";
  var greeting = look.greeting || "Hi! How can I help you today?";
  var side = look.position === "left" ? "left" : "right";
  var light = look.theme !== "dark";
  var contactFirst = look.ask_contact === "before_chat";
  var starters = look.starters || [];
  var initial = (title.replace(/[^A-Za-z0-9ऀ-ॿ]/g, "").charAt(0) || "K").toUpperCase();
  var logo = look.logo_url || null;
  var statusText = look.status_text || t.status;
  var lift = typeof look.offset_bottom === "number" ? look.offset_bottom : 22;
  var mobileMode = look.mobile_mode || "full";
  var hidePaths = look.hide_paths || [];
  var accentDeep = shade(accent, 0.22);
  var phoneQuery = window.matchMedia ? window.matchMedia("(max-width: 480px)") : null;
  function isPhone() { return !!(phoneQuery && phoneQuery.matches); }

  var P = light
    ? { bg: "#ffffff", fg: "#15171c", sub: "#6b7080", soft: "#f2f3f6", line: "#e6e8ee", field: "#f6f7f9", shadow: "0 18px 50px rgba(17,19,24,.18), 0 2px 8px rgba(17,19,24,.08)" }
    : { bg: "#15171d", fg: "#f2f3f5", sub: "#9aa0ad", soft: "#22252d", line: "#2b2f38", field: "#1c1f26", shadow: "0 18px 50px rgba(0,0,0,.55), 0 2px 8px rgba(0,0,0,.3)" };

  var host = document.createElement("div");
  host.id = "krova-widget-host";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  var style = document.createElement("style");
  style.textContent = [
    ":host{all:initial;}",
    "*{box-sizing:border-box;margin:0;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans','Noto Sans Devanagari',sans-serif;}",
    "svg{width:100%;height:100%;fill:currentColor;display:block;}",
    "button{font:inherit;}",
    ".launch{position:fixed;bottom:" + lift + "px;" + side + ":22px;display:flex;align-items:center;gap:12px;z-index:2147483000;",
    "flex-direction:" + (side === "left" ? "row-reverse" : "row") + ";}",
    ".label{background:" + P.bg + ";color:" + P.fg + ";padding:10px 14px;border-radius:14px;font-size:13.5px;font-weight:600;",
    "box-shadow:" + P.shadow + ";cursor:pointer;border:1px solid " + P.line + ";white-space:nowrap;transition:transform .2s;}",
    ".label:hover{transform:translateY(-1px);}",
    ".bubble{position:relative;width:60px;height:60px;border-radius:50%;background:" + accent + ";color:" + ink + ";",
    "box-shadow:0 10px 28px rgba(0,0,0,.22);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;",
    "transition:transform .2s ease;padding:0;}",
    ".bubble:hover{transform:scale(1.06);}",
    ".bubble .ic{width:28px;height:28px;position:absolute;transition:opacity .2s,transform .25s;}",
    ".bubble .ic.x{opacity:0;transform:rotate(-90deg) scale(.6);width:24px;height:24px;}",
    ".bubble.open .ic.c{opacity:0;transform:rotate(90deg) scale(.6);}",
    ".bubble.open .ic.x{opacity:1;transform:none;}",
    ".dot{position:absolute;top:1px;right:1px;width:14px;height:14px;border-radius:50%;background:#ef4444;border:2px solid #fff;display:none;}",
    ".dot.on{display:block;}",
    ".teaser{position:fixed;bottom:" + (lift + 74) + "px;" + side + ":22px;max-width:270px;background:" + P.bg + ";color:" + P.fg + ";",
    "padding:14px 34px 14px 16px;border-radius:16px;font-size:14px;line-height:1.45;box-shadow:" + P.shadow + ";",
    "border:1px solid " + P.line + ";z-index:2147483000;cursor:pointer;opacity:0;transform:translateY(8px);pointer-events:none;transition:opacity .25s,transform .25s;}",
    ".teaser.on{opacity:1;transform:none;pointer-events:auto;}",
    ".teaser .x{position:absolute;top:8px;right:8px;width:20px;height:20px;padding:3px;background:none;border:none;color:" + P.sub + ";cursor:pointer;border-radius:50%;}",
    ".teaser .x:hover{background:" + P.soft + ";}",
    ".panel{position:fixed;bottom:" + (lift + 74) + "px;" + side + ":22px;width:384px;max-width:calc(100vw - 32px);",
    "height:620px;max-height:calc(100vh - " + (lift + 102) + "px);max-height:calc(100dvh - " + (lift + 102) + "px);background:" + P.bg + ";color:" + P.fg + ";border-radius:20px;",
    "box-shadow:" + P.shadow + ";display:flex;flex-direction:column;overflow:hidden;z-index:2147483001;",
    "border:1px solid " + P.line + ";opacity:0;transform:translateY(16px) scale(.98);transform-origin:bottom " + side + ";",
    "pointer-events:none;visibility:hidden;transition:opacity .22s ease,transform .22s ease,visibility 0s .22s;}",
    ".panel.open{opacity:1;transform:none;pointer-events:auto;visibility:visible;transition:opacity .22s ease,transform .22s ease;}",
    // Phones: the chat takes the whole screen (sized to what is visible above the keyboard - see fitPhone), keeps
    // clear of the notch and the home bar, and text fields are 16px so iPhone does not zoom the page on tap.
    "@media (max-width:480px){.panel{top:0;left:0;right:0;bottom:auto;width:100vw;max-width:100vw;height:100vh;height:100dvh;",
    "max-height:none;border-radius:0;border:none;transform:translateY(24px);}",
    ".panel.open{transform:none;}",
    ".panel.open~.launch,.panel.open~.teaser{display:none;}",
    ".panel .head{padding-top:calc(14px + env(safe-area-inset-top));}",
    ".panel .composer{padding-bottom:calc(6px + env(safe-area-inset-bottom));}",
    ".panel .bar input,.panel .form input{font-size:16px;}",
    ".launch{bottom:calc(" + lift + "px + env(safe-area-inset-bottom));" + side + ":16px;}",
    ".teaser{bottom:calc(" + (lift + 74) + "px + env(safe-area-inset-bottom));" + side + ":16px;max-width:calc(100vw - 32px);}",
    (mobileMode === "button" ? ".label,.teaser{display:none !important;}" : "") + "}",
    ".head{padding:16px 16px 16px 18px;background:linear-gradient(135deg," + accent + " 0%," + accentDeep + " 100%);color:" + ink + ";",
    "display:flex;align-items:center;gap:12px;position:relative;}",
    ".avatar{width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.22);display:flex;align-items:center;justify-content:center;",
    "font-weight:700;font-size:17px;flex-shrink:0;border:1.5px solid rgba(255,255,255,.35);overflow:hidden;}",
    ".avatar img,.mini img{width:100%;height:100%;object-fit:cover;background:#fff;display:block;}",
    ".who{flex:1;min-width:0;}",
    ".who .t{font-weight:700;font-size:15.5px;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
    ".who .s{font-size:12px;opacity:.85;margin-top:3px;display:flex;align-items:center;gap:6px;}",
    ".who .s i{width:7px;height:7px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 2px rgba(255,255,255,.35);display:inline-block;}",
    ".hbtn{width:34px;height:34px;border-radius:50%;border:none;background:rgba(255,255,255,.16);color:inherit;cursor:pointer;",
    "padding:7px;display:flex;align-items:center;justify-content:center;text-decoration:none;flex-shrink:0;transition:background .15s;}",
    ".hbtn:hover{background:rgba(255,255,255,.28);}",
    ".body{flex:1;overflow-y:auto;padding:18px 16px 8px;display:flex;flex-direction:column;gap:10px;scrollbar-width:thin;}",
    ".row{display:flex;align-items:flex-end;gap:8px;max-width:100%;animation:in .2s ease;}",
    ".row.user{justify-content:flex-end;}",
    "@keyframes in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}",
    ".mini{width:26px;height:26px;border-radius:50%;background:" + accent + ";color:" + ink + ";font-size:11px;font-weight:700;",
    "display:flex;align-items:center;justify-content:center;flex-shrink:0;overflow:hidden;}",
    ".row.grouped .mini{visibility:hidden;}",
    ".row.cont{margin-top:-6px;}",
    ".bubble.pulse::after{content:'';position:absolute;inset:0;border-radius:50%;border:2px solid " + accent + ";animation:ring 1.6s ease-out 2;}",
    "@keyframes ring{from{transform:scale(1);opacity:.7}to{transform:scale(1.55);opacity:0}}",
    ".msg{max-width:78%;padding:10px 14px;border-radius:18px;font-size:14px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word;}",
    ".msg a{color:inherit;text-decoration:underline;text-underline-offset:2px;overflow-wrap:anywhere;}",
    ".row.user .msg{background:" + accent + ";color:" + ink + ";border-bottom-right-radius:6px;}",
    ".row.bot .msg,.row.team .msg{background:" + P.soft + ";color:" + P.fg + ";border-bottom-left-radius:6px;}",
    ".row.team .msg{box-shadow:inset 3px 0 0 " + accent + ";}",
    ".msg b{display:block;font-size:11px;font-weight:700;color:" + P.sub + ";margin-bottom:3px;letter-spacing:.02em;}",
    ".note{align-self:center;font-size:12px;color:" + P.sub + ";background:" + P.soft + ";padding:5px 12px;border-radius:12px;text-align:center;}",
    ".typing{display:flex;gap:4px;padding:14px 16px;}",
    ".typing span{width:7px;height:7px;border-radius:50%;background:" + P.sub + ";animation:kb 1.1s infinite ease-in-out;}",
    ".typing span:nth-child(2){animation-delay:.15s}.typing span:nth-child(3){animation-delay:.3s}",
    "@keyframes kb{0%,80%,100%{opacity:.35;transform:translateY(0)}40%{opacity:1;transform:translateY(-3px)}}",
    ".chips{display:flex;flex-direction:column;align-items:flex-end;gap:8px;padding:2px 16px 10px;}",
    ".chips button{border:1.5px solid " + accent + ";background:" + P.bg + ";color:" + P.fg + ";border-radius:18px;padding:9px 14px;",
    "font-size:13.5px;cursor:pointer;text-align:left;max-width:85%;transition:background .15s,color .15s;}",
    ".chips button:hover{background:" + accent + ";color:" + ink + ";}",
    ".form{margin:4px 0 6px 34px;background:" + P.bg + ";border:1px solid " + P.line + ";border-radius:16px;padding:14px;display:none;",
    "flex-direction:column;gap:9px;box-shadow:0 4px 14px rgba(0,0,0,.06);}",
    ".form.open{display:flex;}",
    ".form p{font-size:13px;color:" + P.sub + ";line-height:1.4;}",
    ".form p.err{color:#dc2626;}",
    ".form input{width:100%;padding:11px 12px;border-radius:10px;border:1px solid " + P.line + ";background:" + P.field + ";color:" + P.fg + ";font-size:14px;outline:none;}",
    ".form input:focus{border-color:" + accent + ";box-shadow:0 0 0 3px " + accent + "33;}",
    ".form button{padding:11px;border-radius:10px;border:none;background:" + accent + ";color:" + ink + ";font-weight:700;font-size:14px;cursor:pointer;}",
    ".form button:disabled{opacity:.6;}",
    ".composer{padding:10px 12px 6px;border-top:1px solid " + P.line + ";}",
    ".bar{display:flex;align-items:center;gap:6px;background:" + P.field + ";border:1px solid " + P.line + ";border-radius:24px;padding:5px 5px 5px 16px;transition:border-color .15s,box-shadow .15s;}",
    ".bar:focus-within{border-color:" + accent + ";box-shadow:0 0 0 3px " + accent + "26;}",
    ".bar input{flex:1;min-width:0;border:none;background:transparent;color:" + P.fg + ";font-size:14.5px;outline:none;padding:7px 0;}",
    ".bar input::placeholder{color:" + P.sub + ";}",
    ".send{width:38px;height:38px;border-radius:50%;border:none;background:" + accent + ";color:" + ink + ";cursor:pointer;padding:9px;flex-shrink:0;transition:opacity .15s,transform .15s;}",
    ".send:disabled{opacity:.35;cursor:default;}",
    ".send:not(:disabled):hover{transform:scale(1.05);}",
    ".foot{font-size:11px;color:" + P.sub + ";text-align:center;padding:6px 0 10px;opacity:.8;}",
    ".consent{flex:1;display:flex;flex-direction:column;justify-content:flex-end;padding:22px 20px;gap:16px;}",
    ".consent .hello{font-size:22px;font-weight:700;line-height:1.25;}",
    ".consent .card{background:" + P.soft + ";border-radius:16px;padding:16px;font-size:13px;line-height:1.55;color:" + P.sub + ";}",
    ".consent button{padding:14px;border-radius:14px;border:none;background:" + accent + ";color:" + ink + ";font-weight:700;font-size:15px;cursor:pointer;",
    "box-shadow:0 6px 18px " + accent + "55;transition:transform .15s;}",
    ".consent button:hover{transform:translateY(-1px);}",
  ].join("");
  root.appendChild(style);

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }
  // The business's logo when it has one, else the first letter of its name.
  function face(cls) {
    var box = el("div", cls);
    if (logo) {
      var img = document.createElement("img");
      img.src = logo; img.alt = ""; img.referrerPolicy = "no-referrer";
      img.onerror = function () { box.innerHTML = ""; box.textContent = initial; };
      box.appendChild(img);
    } else {
      box.textContent = initial;
    }
    return box;
  }
  function iconButton(cls, svg, labelText) {
    var b = el("button", cls);
    b.type = "button";
    b.innerHTML = svg;
    b.setAttribute("aria-label", labelText);
    return b;
  }

  // Panel
  var panel = el("div", "panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", title);
  var head = el("div", "head");
  head.appendChild(face("avatar"));
  var who = el("div", "who");
  who.appendChild(el("div", "t", title));
  var status = el("div", "s");
  status.appendChild(el("i", ""));
  status.appendChild(document.createTextNode(statusText));
  who.appendChild(status);
  head.appendChild(who);
  if (look.whatsapp) {
    var wa = el("a", "hbtn");
    wa.innerHTML = ICON_WA;
    wa.href = "https://wa.me/" + look.whatsapp + "?text=" + encodeURIComponent("Hi! I came from your website.");
    wa.target = "_blank";
    wa.rel = "noopener";
    wa.setAttribute("aria-label", "WhatsApp");
    wa.title = "WhatsApp";
    head.appendChild(wa);
  }
  var closeBtn = iconButton("hbtn", ICON_DOWN, t.close);
  head.appendChild(closeBtn);
  panel.appendChild(head);

  var consentEl = el("div", "consent");
  consentEl.appendChild(el("div", "hello", t.welcome + " 👋"));
  consentEl.appendChild(el("div", "card", t.notice));
  var consentBtn = el("button", "", t.start);
  consentBtn.type = "button";
  consentEl.appendChild(consentBtn);
  panel.appendChild(consentEl);

  var bodyEl = el("div", "body");
  bodyEl.setAttribute("aria-live", "polite");
  var typingRow = el("div", "row bot typing-row");
  typingRow.appendChild(face("mini"));
  var typingBubble = el("div", "msg typing");
  typingBubble.innerHTML = "<span></span><span></span><span></span>";
  typingRow.appendChild(typingBubble);
  var formEl = el("div", "form");
  var formNote = el("p", "", "");
  var cname = el("input", "");
  cname.type = "text"; cname.placeholder = t.name; cname.autocomplete = "name";
  var cphone = el("input", "");
  cphone.type = "tel"; cphone.placeholder = t.phone; cphone.autocomplete = "tel"; cphone.inputMode = "tel";
  var csubmit = el("button", "", t.go);
  csubmit.type = "button";
  formEl.appendChild(formNote);
  formEl.appendChild(cname);
  formEl.appendChild(cphone);
  formEl.appendChild(csubmit);
  panel.appendChild(bodyEl);

  var chipsEl = el("div", "chips");
  panel.appendChild(chipsEl);

  var composer = el("div", "composer");
  var bar = el("div", "bar");
  var textInput = el("input", "");
  textInput.type = "text"; textInput.placeholder = t.type; textInput.setAttribute("aria-label", t.type);
  var sendBtn = iconButton("send", ICON_SEND, t.send);
  sendBtn.disabled = true;
  bar.appendChild(textInput);
  bar.appendChild(sendBtn);
  composer.appendChild(bar);
  composer.appendChild(el("div", "foot", t.powered));
  panel.appendChild(composer);
  root.appendChild(panel);

  // Launcher and teaser
  var launch = el("div", "launch");
  if (look.launcher_text) {
    var label = el("div", "label", look.launcher_text);
    label.addEventListener("click", function () { toggle(true); });
    launch.appendChild(label);
  }
  var bubble = el("button", "bubble");
  bubble.type = "button";
  bubble.setAttribute("aria-label", t.open);
  var icChat = el("span", "ic c"); icChat.innerHTML = ICON_CHAT;
  var icX = el("span", "ic x"); icX.innerHTML = ICON_CLOSE;
  var dot = el("span", "dot");
  bubble.appendChild(icChat);
  bubble.appendChild(icX);
  bubble.appendChild(dot);
  launch.appendChild(bubble);
  root.appendChild(launch);

  var teaser = el("div", "teaser");
  teaser.appendChild(el("span", "", look.teaser || ""));
  var teaserX = iconButton("x", ICON_CLOSE, t.close);
  teaser.appendChild(teaserX);
  root.appendChild(teaser);

  var isOpen = false;
  var started = false;
  var userSaid = 0;
  var lastAsked = "";
  var toldWithPerson = false;
  var shownReplies = {};
  var busy = false;

  function teamReady() { return getItem(TEAM_KEY) === "1"; }
  function scrollDown() { bodyEl.scrollTop = bodyEl.scrollHeight; }

  // Links in a reply (a payment link, a map) open in a new tab; everything else stays plain text.
  function fill(node, text) {
    String(text).split(/(https?:\/\/[^\s<>"]+)/g).forEach(function (part) {
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

  function place(node) {
    // New lines go above the typing dots and the contact form, which always sit at the bottom.
    var anchor = typingRow.parentNode === bodyEl ? typingRow : (formEl.parentNode === bodyEl ? formEl : null);
    bodyEl.insertBefore(node, anchor);
    scrollDown();
  }
  function addMessage(role, text) {
    if (!text) return;
    var kind = role === "user" ? "user" : role === "team" ? "team" : "bot";
    var row = el("div", "row " + kind);
    if (kind !== "user") row.appendChild(face("mini"));
    // Back-to-back messages from the same side sit together, with one avatar on the last of them.
    var rows = bodyEl.querySelectorAll(".row:not(.typing-row)");
    var prev = rows.length ? rows[rows.length - 1] : null;
    var next = prev ? prev.nextSibling : null;
    if (prev && prev.className.split(" ")[1] === kind && (!next || next === typingRow || next === formEl)) {
      prev.classList.add("grouped");
      row.classList.add("cont");
    }
    var msg = el("div", "msg");
    if (kind === "team") msg.appendChild(el("b", "", t.team));
    fill(msg, text);
    row.appendChild(msg);
    place(row);
  }
  function addNote(text) { place(el("div", "note", text)); }
  function setTyping(on) {
    if (on) { bodyEl.appendChild(typingRow); scrollDown(); }
    else if (typingRow.parentNode === bodyEl) bodyEl.removeChild(typingRow);
  }

  function renderChips() {
    chipsEl.innerHTML = "";
    if (userSaid > 0 || !starters.length || formEl.classList.contains("open") || !hasConsented()) return;
    starters.forEach(function (q) {
      var b = el("button", "", q);
      b.type = "button";
      b.addEventListener("click", function () { submit(q); });
      chipsEl.appendChild(b);
    });
  }

  function showContact(note, isError) {
    formNote.textContent = note || t.contactNeeded;
    formNote.className = isError ? "err" : "";
    if (formEl.parentNode !== bodyEl) bodyEl.appendChild(formEl);
    formEl.classList.add("open");
    if (contactFirst && !teamReady()) composer.style.display = "none";
    renderChips();
    scrollDown();
    cname.focus();
  }
  function hideContact() {
    formEl.classList.remove("open");
    if (formEl.parentNode === bodyEl) bodyEl.removeChild(formEl);
    composer.style.display = "block";
    renderChips();
  }

  function showConsentGate() {
    consentEl.style.display = "flex";
    bodyEl.style.display = "none";
    composer.style.display = "none";
    chipsEl.style.display = "none";
  }
  function showChat() {
    consentEl.style.display = "none";
    bodyEl.style.display = "flex";
    composer.style.display = "block";
    chipsEl.style.display = "flex";
    if (!started) {
      started = true;
      if (!bodyEl.querySelector(".row")) addMessage("bot", greeting);
      if (contactFirst && !teamReady()) showContact(t.contactFirst);
    }
    renderChips();
  }

  // Phones: size the chat to the part of the screen above the keyboard, and stop the page behind it scrolling.
  var savedOverflow = null;
  function fitPhone() {
    if (!isOpen || !isPhone() || !window.visualViewport) { panel.style.height = ""; panel.style.top = ""; return; }
    panel.style.height = window.visualViewport.height + "px";
    panel.style.top = window.visualViewport.offsetTop + "px";
    scrollDown();
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitPhone);
    window.visualViewport.addEventListener("scroll", fitPhone);
  }
  function lockPage(lock) {
    var html = document.documentElement;
    if (lock && isPhone()) {
      if (savedOverflow === null) savedOverflow = html.style.overflow;
      html.style.overflow = "hidden";
    } else if (savedOverflow !== null) {
      html.style.overflow = savedOverflow;
      savedOverflow = null;
    }
  }

  function toggle(open) {
    isOpen = open;
    lockPage(isOpen);
    fitPhone();
    panel.classList.toggle("open", isOpen);
    bubble.classList.toggle("open", isOpen);
    bubble.setAttribute("aria-label", isOpen ? t.close : t.open);
    teaser.classList.remove("on");
    if (!isOpen) return;
    dot.classList.remove("on");
    if (!hasConsented()) showConsentGate(); else showChat();
    scrollDown();
    poll();
    if (composer.style.display !== "none" && window.innerWidth > 480) setTimeout(function () { textInput.focus(); }, 50);
  }
  bubble.addEventListener("click", function () { toggle(!isOpen); });
  closeBtn.addEventListener("click", function () { toggle(false); });
  consentBtn.addEventListener("click", function () { setConsented(); showChat(); textInput.focus(); });
  teaser.addEventListener("click", function (e) {
    if (teaserX.contains(e.target)) { teaser.classList.remove("on"); return; }
    toggle(true);
  });
  root.addEventListener("keydown", function (e) { if (e.key === "Escape" && isOpen) toggle(false); });

  function post(path, payload) {
    return fetch(widgetBase + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  function setLoading(loading) {
    busy = loading;
    csubmit.disabled = loading;
    sendBtn.disabled = loading || !textInput.value.trim();
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
        setTyping(false);
        if (data.reply_text) addMessage("bot", data.reply_text);
        if (data.with_person && !toldWithPerson) { toldWithPerson = true; addNote(t.withPerson); }
        if (data.needs_contact) showContact(""); else hideContact();
        return data;
      })
      .catch(function () { setTyping(false); addMessage("bot", t.failed); })
      .finally(function () { setLoading(false); });
  }

  function submit(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    userSaid += 1;
    lastAsked = text;
    addMessage("user", text);
    renderChips();
    send(text);
  }

  textInput.addEventListener("input", function () { sendBtn.disabled = busy || !textInput.value.trim(); });
  sendBtn.addEventListener("click", function () {
    var text = textInput.value;
    textInput.value = "";
    sendBtn.disabled = true;
    submit(text);
  });
  textInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendBtn.click(); }
  });
  cphone.addEventListener("keydown", function (e) { if (e.key === "Enter") csubmit.click(); });

  csubmit.addEventListener("click", function () {
    var name = cname.value.trim();
    var phone = cphone.value.trim();
    if (phone.replace(/\D/g, "").length < 8) { showContact(t.badPhone, true); cphone.focus(); return; }
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
          hideContact();
          addNote(t.shared);
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
        if (!bodyEl.querySelector(".row")) addMessage("bot", greeting);
        data.messages.forEach(function (m) {
          if (m.role === "user") { userSaid += 1; lastAsked = m.text; }
          if (m.id) shownReplies[m.id] = true;
          if (m.role === "team" && m.at) setItem(AFTER_KEY, m.at);
          addMessage(m.role, m.text);
        });
        started = true;
        renderChips();
      })
      .catch(function () { /* starts fresh */ })
      .finally(poll);
  }

  // Where the chat shows: not on pages the business listed, and not on phones when it chose so. Checked again as
  // the visitor moves around a site that changes pages without reloading.
  function allowedHere() {
    var path = window.location.pathname || "/";
    for (var i = 0; i < hidePaths.length; i++) {
      if (path === hidePaths[i] || path.indexOf(hidePaths[i].replace(/\/$/, "") + "/") === 0) return false;
    }
    return !(mobileMode === "hidden" && isPhone());
  }
  var lastPath = null;
  function applyPlace() {
    var ok = allowedHere();
    host.style.display = ok ? "" : "none";
    if (!ok && isOpen) toggle(false);
  }
  applyPlace();
  setInterval(function () {
    if (window.location.pathname !== lastPath) { lastPath = window.location.pathname; applyPlace(); }
  }, 1000);
  if (phoneQuery && phoneQuery.addEventListener) phoneQuery.addEventListener("change", applyPlace);

  // The teaser: once a day, a few seconds after the page opens, for a visitor who has not chatted yet.
  if (look.teaser && !token) {
    var today = new Date().toISOString().slice(0, 10);
    if (getItem(TEASER_KEY) !== today) {
      setTimeout(function () {
        if (isOpen) return;
        if (!allowedHere() || (mobileMode === "button" && isPhone())) return;
        setItem(TEASER_KEY, today);
        teaser.classList.add("on");
        bubble.classList.add("pulse");
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
