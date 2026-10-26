/* Screenwise — candidate chat. Works without JS (plain form posts); this adds instant updates,
   draft autosave, idempotent retries after network errors, and recovery after refresh. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var initial;
  try { initial = JSON.parse($("#initial-state").textContent); } catch (e) { return; }

  var state = initial;
  var log = $("[data-log]");
  var form = $("[data-composer]");
  var textarea = $("#answer");
  var countEl = $("[data-count]");
  var errorEl = $("[data-error]");
  var saveEl = $("[data-save-status]");
  var offline = $("[data-offline]");
  var finish = $("[data-finish]");
  var endEarly = $("[data-end-early]");
  var csrf = $("[data-csrf]").value;
  var submitBtn = $("[data-submit]");
  var clarifyBtn = $("[data-clarify]");
  var busy = false;
  var retryTimer = null;
  var retryDelay = 2000;

  var storage = (function () {
    try { var k = "__sw"; localStorage.setItem(k, "1"); localStorage.removeItem(k); return localStorage; } catch (e) { return null; }
  })();
  var draftKey = function () { return "sw-draft:" + state.ref + ":" + state.progress.answered; };
  var pendingKey = "sw-pending:" + state.ref;

  function uid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "id-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  var aiIcon = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4h10v6H7l-3 3v-3H3z" stroke="currentColor" fill="none" stroke-width="1.4" stroke-linejoin="round"/></svg>';

  function bubble(msg) {
    if (msg.role === "candidate") {
      var me = el("div", "msg msg-me" + (msg.kind === "clarification_request" ? " msg-clar" : "") + (msg.pending ? " pending" : ""));
      me.appendChild(el("div", "meta", msg.meta));
      me.appendChild(el("div", "bubble", msg.text));
      return me;
    }
    var ai = el("div", "msg" + (msg.kind === "clarification" ? " msg-clar" : ""));
    var av = el("div", "avatar-ai"); av.setAttribute("aria-hidden", "true"); av.innerHTML = aiIcon;
    var body = el("div");
    body.appendChild(el("div", "meta", msg.meta));
    body.appendChild(el("div", "bubble", msg.text));
    ai.appendChild(av); ai.appendChild(body);
    return ai;
  }

  function typing() {
    var wrap = el("div", "msg"); wrap.setAttribute("data-typing", "");
    var av = el("div", "avatar-ai"); av.innerHTML = aiIcon;
    var t = el("div", "typing"); t.setAttribute("aria-label", "Interviewer is responding");
    t.appendChild(el("span")); t.appendChild(el("span")); t.appendChild(el("span"));
    wrap.appendChild(av); wrap.appendChild(t);
    return wrap;
  }

  function render(newState) {
    var prevLen = state.messages.length;
    state = newState;
    // Append only new messages so screen readers announce just those.
    var pend = log.querySelectorAll(".pending, [data-typing]");
    for (var i = 0; i < pend.length; i++) pend[i].remove();
    var existing = log.children.length;
    if (existing > state.messages.length || existing !== prevLen) {
      log.innerHTML = "";
      existing = 0;
    }
    for (var j = existing; j < state.messages.length; j++) log.appendChild(bubble(state.messages[j]));

    var p = state.progress;
    $("[data-progress-label]").textContent = state.all_answered ? "All " + p.total + " questions answered" : "Question " + p.current + " of " + p.total;
    $("[data-time-label]").textContent = state.all_answered ? "Ready to submit" : "About " + state.minutes_left + " min left";
    var bar = $("[data-progress]"); bar.max = p.total; bar.value = p.answered; bar.textContent = p.answered + " of " + p.total;

    form.hidden = state.all_answered;
    finish.hidden = !state.all_answered;
    if (endEarly) endEarly.hidden = state.all_answered;
    if (state.completed) window.location.href = "/interview/done";
  }

  function setSave(text, tone) {
    saveEl.textContent = text;
    saveEl.className = "pill chip-" + (tone || "green");
  }

  function showError(msg) {
    errorEl.textContent = msg || "";
    errorEl.hidden = !msg;
    textarea.classList.toggle("is-invalid", !!msg);
    if (msg) textarea.setAttribute("aria-invalid", "true"); else textarea.removeAttribute("aria-invalid");
  }

  function updateCount() { countEl.textContent = String(textarea.value.length); }

  function saveDraft() {
    if (!storage) return;
    try {
      if (textarea.value) storage.setItem(draftKey(), textarea.value); else storage.removeItem(draftKey());
      setSave(textarea.value ? "Draft saved on this device" : "All changes saved");
    } catch (e) { /* storage full or blocked: ignore */ }
  }

  function restoreDraft() {
    if (!storage || !textarea) return;
    var d = storage.getItem(draftKey());
    if (d && !textarea.value) { textarea.value = d; setSave("Draft restored"); }
    updateCount();
  }

  function clearDrafts() {
    if (!storage) return;
    try {
      for (var i = storage.length - 1; i >= 0; i--) {
        var k = storage.key(i);
        if (k && k.indexOf("sw-draft:" + state.ref + ":") === 0) storage.removeItem(k);
      }
    } catch (e) { /* ignore */ }
  }

  function setBusy(b) {
    busy = b;
    submitBtn.disabled = b; clarifyBtn.disabled = b;
    submitBtn.setAttribute("aria-busy", b ? "true" : "false");
  }

  function savePending(p) { if (storage) try { p ? storage.setItem(pendingKey, JSON.stringify(p)) : storage.removeItem(pendingKey); } catch (e) {} }
  function loadPending() { if (!storage) return null; try { return JSON.parse(storage.getItem(pendingKey) || "null"); } catch (e) { return null; } }

  function send(p) {
    setBusy(true);
    savePending(p);
    return fetch(p.url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify({ text: p.text, client_msg_id: p.id, prompt_seq: p.prompt })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) { return { res: res, body: body }; });
    }).then(function (r) {
      setBusy(false);
      offline.hidden = true;
      retryDelay = 2000;
      if (r.res.status === 409 && r.body.state === "submitted") { savePending(null); window.location.href = "/interview/done"; return; }
      if (r.res.status === 410 || r.res.status === 403) { savePending(null); window.location.reload(); return; }
      if (r.body && r.body.messages) { render(r.body); var ps = document.querySelector("[data-prompt-seq]"); if (ps) ps.value = state.prompt_seq || ""; }
      if (!r.res.ok) {
        savePending(null);
        // Put the text back so nothing is lost, and explain.
        if (p.kind === "answer" && !textarea.value) textarea.value = p.text;
        updateCount();
        showError((r.body && r.body.error) || "Something went wrong. Your answer is still here; please try again.");
        return;
      }
      savePending(null);
      if (p.kind === "answer") { clearDrafts(); textarea.value = ""; updateCount(); setSave("Answer submitted"); }
      showError("");
      if (!state.all_answered) textarea.focus();
    }).catch(function () {
      // Network failure: keep the pending submission and retry with the same id (server de-duplicates).
      setBusy(false);
      offline.hidden = false;
      setSave("Saved on this device", "amber");
      clearTimeout(retryTimer);
      retryTimer = setTimeout(function () { retry(); }, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    });
  }

  function retry() {
    var p = loadPending();
    if (p && !busy) send(p);
  }

  function submit(kind) {
    if (busy) return;
    var text = textarea.value.trim();
    if (kind === "answer" && !text) { showError("Please type an answer before submitting, or use “Ask for clarification”."); textarea.focus(); return; }
    showError("");
    var p = { id: uid(), kind: kind, text: kind === "answer" ? text : "", prompt: state.prompt_seq, url: kind === "answer" ? "/interview/answer" : "/interview/clarify" };
    var shown = kind === "answer"
      ? { role: "candidate", kind: "answer", meta: "You · sending…", text: text, pending: true }
      : { role: "candidate", kind: "clarification_request", meta: "You · asked for clarification", text: "Could you clarify this question?", pending: true };
    log.appendChild(bubble(shown));
    log.appendChild(typing());
    if (kind === "answer") { textarea.value = ""; updateCount(); }
    send(p).then(function () {
      if (kind === "answer" && errorEl.hidden === false && !textarea.value) textarea.value = text;
    });
  }

  if (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var isClarify = e.submitter && e.submitter.hasAttribute("data-clarify");
      submit(isClarify ? "clarify" : "answer");
    });
    textarea.addEventListener("input", function () { updateCount(); showError(""); saveDraft(); });
    textarea.addEventListener("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); submit("answer"); }
    });
  }

  $("[data-retry]").addEventListener("click", retry);
  window.addEventListener("online", retry);
  window.addEventListener("offline", function () { offline.hidden = false; });

  restoreDraft();
  if (loadPending()) retry();
  if (textarea && !state.all_answered && window.matchMedia("(min-width: 720px)").matches) textarea.focus();
})();
