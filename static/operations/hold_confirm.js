(function () {
  function bindHoldConfirm(button) {
    var form = button.closest("form");
    var target = document.getElementById(button.dataset.targetId);
    var holdMs = Number(button.dataset.holdMs || 1200);
    var timer = null;

    button.style.setProperty("--hold-duration", holdMs + "ms");

    function start(event) {
      if (event.type === "keydown" && event.key !== " " && event.key !== "Enter") return;
      event.preventDefault();
      if (timer || button.disabled) return;
      button.classList.add("is-holding");
      timer = window.setTimeout(function () {
        timer = null;
        button.classList.remove("is-holding");
        button.classList.add("is-complete");
        if (target) target.value = "1";
        if (form) form.submit();
      }, holdMs);
    }

    function cancel() {
      if (!timer) return;
      window.clearTimeout(timer);
      timer = null;
      button.classList.remove("is-holding");
    }

    button.addEventListener("pointerdown", start);
    button.addEventListener("pointerup", cancel);
    button.addEventListener("pointerleave", cancel);
    button.addEventListener("pointercancel", cancel);
    button.addEventListener("keydown", start);
    button.addEventListener("keyup", cancel);
  }

  document.querySelectorAll("[data-hold-submit='true']").forEach(bindHoldConfirm);
}());
