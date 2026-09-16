(function () {
  "use strict";

  function windowRow(day) {
    var weekday = day.dataset.day;
    var row = document.createElement("div");
    row.className = "schedule-window";
    row.dataset.windowRow = "";
    row.innerHTML =
      '<label>С <input type="time" name="day_' + weekday + '_start" required></label>' +
      '<label>До <input type="time" name="day_' + weekday + '_end" required></label>' +
      '<button class="btn btn-outline-secondary btn-sm" type="button" data-remove-window aria-label="Удалить интервал">Удалить</button>';
    return row;
  }

  function syncDay(day) {
    var closed = day.querySelector("[data-day-closed]");
    var controls = day.querySelectorAll("[data-windows] input, [data-add-window], [data-remove-window]");
    day.classList.toggle("is-closed", closed.checked);
    controls.forEach(function (control) {
      control.disabled = closed.checked;
      if (closed.checked && control.matches("input")) control.required = false;
      if (!closed.checked && control.matches("input")) control.required = true;
    });
  }

  document.querySelectorAll("[data-staff-schedule-form]").forEach(function (form) {
    form.querySelectorAll("[data-schedule-day]").forEach(function (day) {
      var closed = day.querySelector("[data-day-closed]");
      closed.addEventListener("change", function () { syncDay(day); });
      day.querySelector("[data-add-window]").addEventListener("click", function () {
        day.querySelector("[data-windows]").appendChild(windowRow(day));
        syncDay(day);
        var input = day.querySelector("[data-windows] [data-window-row]:last-child input");
        if (input) input.focus();
      });
      day.querySelector("[data-windows]").addEventListener("click", function (event) {
        var button = event.target.closest("[data-remove-window]");
        if (!button) return;
        var rows = day.querySelectorAll("[data-window-row]");
        if (rows.length === 1 && !closed.checked) {
          button.closest("[data-window-row]").querySelectorAll("input").forEach(function (input) { input.value = ""; });
          return;
        }
        button.closest("[data-window-row]").remove();
      });
      syncDay(day);
    });
  });
})();
