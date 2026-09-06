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

  function getSessionToken() {
    try { return window.localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }
  function setSessionToken(token) {
    try { window.localStorage.setItem(STORAGE_KEY, token); } catch (e) { /* private browsing, etc - fine to lose it */ }
  }
  function hasConsented() {
    try { return window.localStorage.getItem(CONSENT_KEY) === "1"; } catch (e) { return false; }
  }
  function setConsented() {
    try { window.localStorage.setItem(CONSENT_KEY, "1"); } catch (e) { /* re-asked next visit - fine */ }
  }

  var host = document.createElement("div");
  host.id = "krova-widget-host";
  document.body.appendChild(host);
  var root = host.attachShadow({ mode: "open" });

  var style = document.createElement("style");
  style.textContent = [
    ":host, *{box-sizing:border-box;font-family:-apple-system,'Segoe UI',Inter,system-ui,sans-serif;}",
    ".bubble{position:fixed;bottom:20px;right:20px;width:56px;height:56px;border-radius:50%;",
    "background:" + accent + ";box-shadow:0 4px 16px rgba(0,0,0,.25);border:none;cursor:pointer;",
    "display:flex;align-items:center;justify-content:center;z-index:2147483000;}",
    ".bubble svg{width:26px;height:26px;fill:#14151F;}",
    ".panel{position:fixed;bottom:88px;right:20px;width:360px;max-width:calc(100vw - 32px);",
    "height:520px;max-height:calc(100vh - 120px);background:#0f1117;border-radius:16px;",
    "box-shadow:0 8px 32px rgba(0,0,0,.35);display:none;flex-direction:column;overflow:hidden;",
    "z-index:2147483000;border:1px solid rgba(255,255,255,.08);}",
    ".panel.open{display:flex;}",
    ".head{padding:14px 16px;background:" + accent + ";color:#14151F;font-weight:700;font-size:14px;}",
    ".messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;}",
    ".msg{max-width:85%;padding:8px 12px;border-radius:12px;font-size:13px;line-height:1.4;}",
    ".msg.user{align-self:flex-end;background:" + accent + ";color:#14151F;}",
    ".msg.bot{align-self:flex-start;background:rgba(255,255,255,.08);color:#f4f4f5;}",
    ".contact{padding:10px 12px;background:rgba(255,255,255,.04);border-top:1px solid rgba(255,255,255,.08);display:none;gap:6px;flex-direction:column;}",
    ".contact.open{display:flex;}",
    ".contact input{padding:8px 10px;border-radius:8px;border:1px solid rgba(255,255,255,.15);",
    "background:#0a0c11;color:#f4f4f5;font-size:12px;}",
    ".contact button{padding:8px;border-radius:8px;border:none;background:" + accent + ";color:#14151F;font-weight:700;font-size:12px;cursor:pointer;}",
    ".inputRow{display:flex;gap:6px;padding:10px;border-top:1px solid rgba(255,255,255,.08);}",
    ".inputRow input{flex:1;padding:9px 12px;border-radius:20px;border:1px solid rgba(255,255,255,.15);",
    "background:#0a0c11;color:#f4f4f5;font-size:13px;outline:none;}",
    ".inputRow button{padding:9px 14px;border-radius:20px;border:none;background:" + accent + ";",
    "color:#14151F;font-weight:700;font-size:13px;cursor:pointer;}",
    ".dim{color:#9a9aa5;font-size:11px;padding:6px 12px;}",
    ".consentScreen{flex:1;display:flex;flex-direction:column;justify-content:center;",
    "padding:20px;gap:14px;text-align:center;}",
    ".consentScreen p{color:#d0d0d6;font-size:12.5px;line-height:1.5;margin:0;}",
    ".consentScreen button{padding:10px;border-radius:10px;border:none;background:" + accent + ";",
    "color:#14151F;font-weight:700;font-size:13px;cursor:pointer;}",
  ].join("");
  root.appendChild(style);

  var bubble = document.createElement("button");
  bubble.className = "bubble";
  bubble.setAttribute("aria-label", "Open chat");
  bubble.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.03 2 11c0 2.4 1.05 4.57 2.77 6.17L4 22l5.06-2.53c.94.22 1.93.34 2.94.34 5.52 0 10-4.03 10-9s-4.48-9-10-9z"/></svg>';
  root.appendChild(bubble);

  var panel = document.createElement("div");
  panel.className = "panel";
  panel.innerHTML =
    '<div class="head">Chat with us</div>' +
    '<div class="consentScreen">' +
      '<p>This chat is answered by an AI assistant using this business\'s own information. ' +
      'By continuing, you agree to share your messages (and, if you choose to book something, ' +
      'your contact details) with them.</p>' +
      '<button class="consentBtn">Start Chat</button>' +
    '</div>' +
    '<div class="messages"></div>' +
    '<div class="contact">' +
      '<input type="text" class="cname" placeholder="Your name" />' +
      '<input type="tel" class="cphone" placeholder="Phone number" />' +
      '<button class="csubmit">Continue</button>' +
    '</div>' +
    '<div class="inputRow">' +
      '<input type="text" class="textInput" placeholder="Type a message..." />' +
      '<button class="sendBtn">Send</button>' +
    '</div>';
  root.appendChild(panel);

  var consentScreenEl = panel.querySelector(".consentScreen");
  var consentBtn = panel.querySelector(".consentBtn");
  var messagesEl = panel.querySelector(".messages");
  var contactEl = panel.querySelector(".contact");
  var textInput = panel.querySelector(".textInput");
  var sendBtn = panel.querySelector(".sendBtn");
  var csubmit = panel.querySelector(".csubmit");
  var cname = panel.querySelector(".cname");
  var cphone = panel.querySelector(".cphone");

  var pendingContact = null;
  var isOpen = false;

  // DPDP: the notice is shown, and the chat surface itself stays hidden,
  // until the visitor actively accepts it - never inferred from opening
  // the widget, never smuggled into the first message silently.
  function showConsentGate() {
    consentScreenEl.style.display = "flex";
    messagesEl.style.display = "none";
    textInput.parentElement.style.display = "none";
  }
  function showChat() {
    consentScreenEl.style.display = "none";
    messagesEl.style.display = "flex";
    textInput.parentElement.style.display = "flex";
  }

  consentBtn.addEventListener("click", function () {
    setConsented();
    showChat();
    addMessage("bot", "Hi! How can I help you today?");
  });

  bubble.addEventListener("click", function () {
    isOpen = !isOpen;
    panel.classList.toggle("open", isOpen);
    if (!isOpen) return;
    if (!hasConsented()) {
      showConsentGate();
    } else if (messagesEl.children.length === 0) {
      showChat();
      addMessage("bot", "Hi! How can I help you today?");
    }
  });

  function addMessage(role, text) {
    if (!text) return;
    var div = document.createElement("div");
    div.className = "msg " + (role === "user" ? "user" : "bot");
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setLoading(loading) {
    sendBtn.disabled = loading;
    textInput.disabled = loading;
  }

  function send(text, contact) {
    setLoading(true);
    return fetch(apiBase + "/api/v1/widget/" + encodeURIComponent(siteKey) + "/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_token: getSessionToken(),
        text: text,
        consent: hasConsented(),
        contact: contact || undefined,
      }),
    })
      .then(function (res) {
        if (res.status === 429) {
          addMessage("bot", "We're getting a lot of messages right now - please try again in a minute.");
          return null;
        }
        if (!res.ok) throw new Error("request failed");
        return res.json();
      })
      .then(function (data) {
        if (!data) return data;
        setSessionToken(data.session_token);
        // Should not normally happen (the consent gate blocks this client-
        // side already), but the backend is the real enforcement - if it
        // ever disagrees, defer to it rather than show a broken reply.
        if (data.needs_consent) {
          showConsentGate();
          return data;
        }
        if (data.reply_text) addMessage("bot", data.reply_text);
        if (data.needs_contact) {
          contactEl.classList.add("open");
        } else {
          contactEl.classList.remove("open");
        }
        return data;
      })
      .catch(function () {
        addMessage("bot", "Sorry, something went wrong. Please try again.");
      })
      .finally(function () {
        setLoading(false);
      });
  }

  sendBtn.addEventListener("click", function () {
    var text = textInput.value.trim();
    if (!text) return;
    addMessage("user", text);
    textInput.value = "";
    send(text, pendingContact);
    pendingContact = null;
  });
  textInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") sendBtn.click();
  });

  csubmit.addEventListener("click", function () {
    var name = cname.value.trim();
    var phone = cphone.value.trim();
    if (!phone) return;
    pendingContact = { name: name || undefined, phone: phone };
    contactEl.classList.remove("open");
    addMessage("user", "(shared contact info)");
    // Re-send the visitor's last request now that contact info is known,
    // so the booking they originally asked for actually completes -
    // matches the backend's own needs_contact contract: the same message
    // retried once identity resolves, not a second unrelated request.
    var lastUserMsg = "";
    var nodes = messagesEl.querySelectorAll(".msg.user");
    if (nodes.length > 1) lastUserMsg = nodes[nodes.length - 2].textContent;
    if (lastUserMsg) send(lastUserMsg, pendingContact);
    pendingContact = null;
  });
})();
"""


@router.get("/widget.js")
async def widget_js() -> Response:
    return Response(content=_WIDGET_JS, media_type="application/javascript")
