(function() {
  if (window.__obInit) return;
  window.__obInit = true;
  function init() {
    var modal = document.getElementById("onboarding-modal");
    if (!modal) return;
    document.body.classList.add("onboarding-active");
    var TOTAL = 6;
    var i18n = {};
    try {
      var raw = document.getElementById("ob-i18n-data");
      if (raw && raw.textContent) {
        i18n = JSON.parse(raw.textContent) || {};
      }
    } catch (_) {
      i18n = {};
    }
    function t(key, fallback) {
      var v = i18n[key];
      return typeof v === "string" && v.length ? v : fallback || key;
    }
    var steps = modal.querySelectorAll(".ob-step");
    var dots = modal.querySelectorAll(".ob-progress-dot");
    var indicator = modal.querySelector("#ob-step-indicator");
    var skipBtn = modal.querySelector("#ob-skip-btn");
    var backBtn = modal.querySelector("#ob-back-btn");
    var nextBtn = modal.querySelector("#ob-next-btn");
    var submitBtn = modal.querySelector("#ob-submit-btn");
    var footerError = modal.querySelector("#ob-footer-error");
    var nameInput = modal.querySelector("#ob-display-name");
    var nameError = modal.querySelector("#ob-name-error");
    var termsInput = modal.querySelector("#ob-terms");
    var current = 1;
    function setIndicator(n) {
      if (!indicator) return;
      var tpl = t("step.indicator", "Step {current} of {total}");
      indicator.textContent = tpl.replace("{current}", String(n)).replace("{total}", String(TOTAL));
    }
    function show(n) {
      if (n < 1) n = 1;
      if (n > TOTAL) n = TOTAL;
      current = n;
      for (var i = 0; i < steps.length; i++) {
        var stepNum = parseInt(steps[i].getAttribute("data-step"), 10);
        if (stepNum === n) steps[i].classList.add("is-active"); else steps[i].classList.remove("is-active");
      }
      for (var j = 0; j < dots.length; j++) {
        var d = parseInt(dots[j].getAttribute("data-step"), 10);
        dots[j].classList.remove("is-active", "is-done");
        if (d < n) dots[j].classList.add("is-done"); else if (d === n) dots[j].classList.add("is-active");
      }
      setIndicator(n);
      if (n === TOTAL) {
        if (skipBtn) skipBtn.hidden = true;
        if (nextBtn) nextBtn.hidden = true;
        if (submitBtn) submitBtn.hidden = false;
      } else {
        if (skipBtn) skipBtn.hidden = false;
        if (nextBtn) nextBtn.hidden = false;
        if (submitBtn) submitBtn.hidden = true;
      }
      if (backBtn) backBtn.hidden = n === 1;
      var body = modal.querySelector(".ob-body");
      if (body) body.scrollTop = 0;
      clearFooterError();
      if (n === TOTAL && nameInput) {
        try {
          setTimeout(function() {
            nameInput.focus();
          }, 60);
        } catch (_) {}
      }
    }
    function clearFooterError() {
      if (footerError) {
        footerError.classList.remove("is-visible");
        footerError.textContent = "";
      }
    }
    function showFooterError(msg) {
      if (!footerError) return;
      footerError.textContent = msg;
      footerError.classList.add("is-visible");
    }
    function clearNameError() {
      if (nameError) {
        nameError.classList.remove("is-visible");
        nameError.textContent = "";
      }
      if (nameInput) nameInput.classList.remove("is-invalid");
    }
    function showNameError(msg) {
      if (nameError) {
        nameError.textContent = msg;
        nameError.classList.add("is-visible");
      }
      if (nameInput) nameInput.classList.add("is-invalid");
    }
    function validateNameClient(value) {
      var v = (value || "").trim();
      if (!v) return t("validation.required", "Please fill out this field");
      if (v.length < 2) return t("validation.tooShort", "At least 2 characters required");
      if (v.length > 40) return t("validation.tooLong", "Maximum 40 characters");
      if (/^0x[a-fA-F0-9]{40}$/.test(v)) {
        return t("validation.walletStyle", "Username cannot be a raw wallet address");
      }
      var allowed = /^[\p{L}\p{N} ._'\-]+$/u;
      try {
        if (!allowed.test(v)) {
          return t("validation.invalidChars", "Only letters, numbers, space, dot, underscore, dash, apostrophe allowed");
        }
      } catch (_) {}
      return null;
    }
    if (skipBtn) {
      skipBtn.addEventListener("click", function() {
        show(TOTAL);
      });
    }
    if (backBtn) {
      backBtn.addEventListener("click", function() {
        show(current - 1);
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener("click", function() {
        show(current + 1);
      });
    }
    if (nameInput) {
      nameInput.addEventListener("input", clearNameError);
    }
    if (termsInput) {
      termsInput.addEventListener("change", function() {
        if (termsInput.checked) clearFooterError();
      });
    }
    var form = modal.querySelector("#ob-final-form");
    if (form) {
      form.addEventListener("submit", function(ev) {
        ev.preventDefault();
        ev.stopPropagation();
        clearFooterError();
        clearNameError();
        var nameVal = nameInput ? nameInput.value : "";
        var clientErr = validateNameClient(nameVal);
        if (clientErr) {
          showNameError(clientErr);
          if (nameInput) try {
            nameInput.focus();
          } catch (_) {}
          return;
        }
        if (termsInput && !termsInput.checked) {
          showFooterError(t("validation.terms", "Please accept the terms to continue"));
          return;
        }
        var body = new FormData;
        body.append("display_name", (nameVal || "").trim());
        body.append("terms_accepted", "1");
        var origLabel = submitBtn ? submitBtn.textContent : "";
        if (submitBtn) {
          submitBtn.disabled = true;
          submitBtn.textContent = t("btn.submitting", "Setting up…");
        }
        fetch("/api/user/onboard-submit", {
          method: "POST",
          body: body,
          credentials: "same-origin",
          headers: {
            "X-Requested-With": "XMLHttpRequest"
          }
        }).then(function(r) {
          return r.json().then(function(data) {
            return {
              ok: r.ok,
              status: r.status,
              data: data
            };
          });
        }).then(function(res) {
          if (res.ok && res.data && res.data.ok) {
            closeModal(true);
            return;
          }
          var errKey = res.data && res.data.error_key || "validation.network";
          var fallback = res.data && res.data.error || t("validation.network", "Network error");
          var msg = t(errKey, fallback);
          if (errKey === "validation.required" || errKey === "validation.tooShort" || errKey === "validation.tooLong" || errKey === "validation.invalidChars" || errKey === "validation.walletStyle" || errKey === "validation.reserved" || errKey === "validation.taken") {
            showNameError(msg);
            if (nameInput) try {
              nameInput.focus();
            } catch (_) {}
          } else {
            showFooterError(msg);
          }
        }).catch(function() {
          showFooterError(t("validation.network", "Network error"));
        }).finally(function() {
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = origLabel;
          }
        });
      });
    }
    function closeModal(reload) {
      try {
        if (modal && modal.parentNode) {
          modal.parentNode.removeChild(modal);
        }
        var host = document.getElementById("onboarding-host");
        if (host && host.parentNode) host.innerHTML = "";
      } catch (_) {}
      document.body.classList.remove("onboarding-active");
      if (reload) {
        try {
          window.location.reload();
        } catch (_) {}
      }
    }
    show(1);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();