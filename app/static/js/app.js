/* Screenwise — progressive enhancements for recruiter and candidate pages. Everything works without JS. */
(function () {
  "use strict";
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // Auto-submit filter selects
  $$("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { if (el.form) el.form.submit(); });
  });

  // Confirmation prompts
  $$("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
    });
  });

  // Prevent double submits (button disabled after the browser has captured its value)
  $$("[data-once]").forEach(function (btn) {
    var form = btn.form;
    if (!form) return;
    form.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      window.setTimeout(function () {
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        btn.dataset.label = btn.textContent;
        btn.textContent = "Working…";
      }, 0);
    });
  });
  window.addEventListener("pageshow", function () {
    $$("[data-once][aria-busy]").forEach(function (btn) {
      btn.disabled = false; btn.removeAttribute("aria-busy");
      if (btn.dataset.label) btn.textContent = btn.dataset.label;
    });
  });

  // Copy to clipboard
  $$("[data-copy]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var target = $(btn.getAttribute("data-copy"));
      if (!target) return;
      var text = target.textContent.trim();
      var done = function () { var old = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.textContent = old; }, 1600); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { window.prompt("Copy this:", text); });
      } else { window.prompt("Copy this:", text); }
    });
  });

  $$("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });
  $$("[data-back]").forEach(function (a) {
    a.addEventListener("click", function (e) { if (window.history.length > 1) { e.preventDefault(); window.history.back(); } });
  });

  // Close the account menu when clicking elsewhere
  document.addEventListener("click", function (e) {
    $$("details.usermenu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });

  // Job form: add criterion rows from <template>
  var addBtn = $("[data-add-criterion]");
  if (addBtn) {
    addBtn.addEventListener("click", function () {
      var list = $("#criteria-list");
      var tpl = $("#criterion-template");
      var i = parseInt(list.getAttribute("data-next-index") || "0", 10);
      var html = tpl.innerHTML.replace(/__i__/g, String(i));
      var wrap = document.createElement("div");
      wrap.innerHTML = html.trim();
      var node = wrap.firstElementChild;
      list.appendChild(node);
      list.setAttribute("data-next-index", String(i + 1));
      var input = node.querySelector("input[type=text], input:not([type])");
      if (input) input.focus();
    });
  }

  // Invitation preview follows the form
  var frame = $("#email-preview");
  if (frame) {
    var timer = null;
    var refresh = function () {
      var params = new URLSearchParams();
      $$("[data-preview-param]").forEach(function (el) { if (el.value) params.set(el.getAttribute("data-preview-param"), el.value); });
      frame.src = frame.getAttribute("data-base") + "?" + params.toString();
    };
    $$("[data-preview-param]").forEach(function (el) {
      el.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(refresh, 500); });
      el.addEventListener("change", refresh);
    });
  }

  // Candidate application: file picker feedback with client-side checks (server re-validates)
  var drop = $("[data-dropzone]");
  if (drop) {
    var input = drop.querySelector("input[type=file]");
    var ok = $("[data-file-ok]");
    var err = $("[data-file-error]");
    var maxMb = parseFloat((drop.textContent.match(/up to ([\d.]+) MB/) || [0, "4"])[1]);
    var show = function (file) {
      err.hidden = true;
      if (!file) { ok.classList.remove("show"); drop.hidden = false; return; }
      var name = file.name.toLowerCase();
      if (!/\.(pdf|docx)$/.test(name)) { err.textContent = "Only PDF and DOCX files are accepted."; err.hidden = false; input.value = ""; return; }
      if (file.size > maxMb * 1024 * 1024) { err.textContent = "This file is larger than " + maxMb + " MB. Please upload a smaller file."; err.hidden = false; input.value = ""; return; }
      $("[data-file-name]").textContent = file.name;
      $("[data-file-size]").textContent = Math.max(1, Math.round(file.size / 1024)) + " KB · ready to upload";
      ok.classList.add("show");
      drop.hidden = true;
    };
    input.addEventListener("change", function () { show(input.files && input.files[0]); });
    ["dragenter", "dragover"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("drag"); }); });
    ["dragleave", "drop"].forEach(function (ev) { drop.addEventListener(ev, function () { drop.classList.remove("drag"); }); });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      if (e.dataTransfer && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(input.files[0]); }
    });
    var replace = $("[data-file-replace]");
    if (replace) replace.addEventListener("click", function () { input.value = ""; show(null); input.click(); });
  }

  // Interview welcome: enable Start once acknowledged
  var ack = $("[data-ack]");
  if (ack) {
    var start = $("[data-start]");
    var hint = $("[data-ack-hint]");
    var sync = function () { start.setAttribute("aria-disabled", ack.checked ? "false" : "true"); hint.hidden = ack.checked; };
    ack.addEventListener("change", sync);
    start.addEventListener("click", function (e) { if (!ack.checked) { e.preventDefault(); ack.focus(); hint.hidden = false; } });
    sync();
  }
})();
