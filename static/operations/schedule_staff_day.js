(function () {
  var root = document.getElementById("staffDaySchedule");
  if (!root) return;

  var START_HOUR = 8;
  var END_HOUR = 20;
  var MINUTE_HEIGHT = 1.55;
  var STEP_MINUTES = 30;
  var timelineMinutes = (END_HOUR - START_HOUR) * 60;
  var timelineHeight = timelineMinutes * MINUTE_HEIGHT;
  var halfHourHeight = STEP_MINUTES * MINUTE_HEIGHT;

  var state = {
    date: root.dataset.initialDay || todayIso(),
    staff: [],
    appointments: [],
    loading: false,
  };

  var statusDefs = {
    draft: { label: "Черновик", color: "#9ca3af" },
    proposed: { label: "Предложено", color: "#ea580c" },
    confirmed: { label: "Согласовано", color: "#16a34a" },
    reserved: { label: "Бронь", color: "#a855f7" },
    completed: { label: "Проведено", color: "#6b7280" },
    cancelled: { label: "Отменено", color: "#ef4444" },
    no_show: { label: "Неявка", color: "#dc2626" },
    rescheduled: { label: "Перенесено", color: "#f59e0b" },
  };

  var calendarSection = document.getElementById("calendar-section");
  var staffDaySection = document.getElementById("staff-day-section");
  var staffDayMode = document.getElementById("staffDayMode");
  var calendarMode = document.getElementById("calendarMode");
  var dateInput = document.getElementById("staffDayDate");
  var prevBtn = document.getElementById("staffDayPrev");
  var nextBtn = document.getElementById("staffDayNext");
  var todayBtn = document.getElementById("staffDayToday");
  var meta = document.getElementById("staffDayMeta");
  var staffFilter = document.getElementById("staffFilter");
  var serviceFilter = document.getElementById("serviceFilter");

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function todayIso() {
    var d = new Date();
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function dateFromIso(value) {
    var parts = value.split("-").map(Number);
    return new Date(parts[0], parts[1] - 1, parts[2]);
  }

  function shiftDay(value, delta) {
    var d = dateFromIso(value);
    d.setDate(d.getDate() + delta);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function dayRange(dateIso) {
    var start = dateFromIso(dateIso);
    var end = new Date(start);
    end.setDate(end.getDate() + 1);
    return {
      start: start.toISOString(),
      end: end.toISOString(),
    };
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function readTimeMinutes(iso) {
    var match = String(iso).match(/T(\d{2}):(\d{2})/);
    if (!match) return START_HOUR * 60;
    return Number(match[1]) * 60 + Number(match[2]);
  }

  function formatMinutes(total) {
    var hh = Math.floor(total / 60);
    var mm = total % 60;
    return pad(hh) + ":" + pad(mm);
  }

  function appointmentDurationMinutes(item) {
    var start = new Date(item.start);
    var end = new Date(item.end);
    var duration = Math.round((end - start) / 60000);
    return Math.max(duration || STEP_MINUTES, 15);
  }

  function createUrl(staffId, minutes) {
    var url = new URL(root.dataset.createUrl || "/appointments/new/", window.location.origin);
    url.searchParams.set("date", state.date);
    url.searchParams.set("time", formatMinutes(minutes));
    url.searchParams.set("staff_id", staffId);
    return url.pathname + url.search;
  }

  function detailUrl(id) {
    return "/appointments/" + id + "/";
  }

  function selectedStaffId() {
    return staffFilter && staffFilter.value ? String(staffFilter.value) : "";
  }

  function selectedServiceId() {
    return serviceFilter && serviceFilter.value ? String(serviceFilter.value) : "";
  }

  function filteredStaff() {
    var selected = selectedStaffId();
    if (!selected) return state.staff;
    return state.staff.filter(function (item) {
      return String(item.id) === selected;
    });
  }

  function filteredAppointments() {
    var staffId = selectedStaffId();
    var serviceId = selectedServiceId();
    return state.appointments
      .filter(function (item) {
        var props = item.extendedProps || {};
        if (String(item.start).slice(0, 10) !== state.date) return false;
        if (staffId && String(props.staffId) !== staffId) return false;
        if (serviceId && String(props.serviceId) !== serviceId) return false;
        return true;
      })
      .sort(function (a, b) {
        return String(a.start).localeCompare(String(b.start));
      });
  }

  function setMeta(columns, appointments) {
    var dateLabel = dateFromIso(state.date).toLocaleDateString("ru-RU", {
      weekday: "long",
      day: "2-digit",
      month: "long",
      year: "numeric",
    });
    meta.innerHTML =
      "<span>" + escapeHtml(dateLabel) + "</span>" +
      "<span>" + columns.length + " спец.</span>" +
      "<span>" + appointments.length + " зан.</span>";
  }

  function renderTimeColumn() {
    var html = '<div class="staff-day-time-column">';
    for (var minute = START_HOUR * 60; minute <= END_HOUR * 60; minute += STEP_MINUTES) {
      var top = (minute - START_HOUR * 60) * MINUTE_HEIGHT;
      html += '<div class="staff-day-tick" style="top:' + top + 'px"><span>' + formatMinutes(minute) + "</span></div>";
    }
    html += "</div>";
    return html;
  }

  function renderSlotHits(staffId) {
    var html = "";
    for (var minute = START_HOUR * 60; minute < END_HOUR * 60; minute += STEP_MINUTES) {
      var top = (minute - START_HOUR * 60) * MINUTE_HEIGHT;
      html +=
        '<a class="staff-day-slot-hit" href="' +
        escapeHtml(createUrl(staffId, minute)) +
        '" style="top:' +
        top +
        "px;height:" +
        halfHourHeight +
        'px" aria-label="Создать занятие на ' +
        escapeHtml(formatMinutes(minute)) +
        '"></a>';
    }
    return html;
  }

  function renderNowLine() {
    if (state.date !== todayIso()) return "";
    var now = new Date();
    var minute = now.getHours() * 60 + now.getMinutes();
    if (minute < START_HOUR * 60 || minute > END_HOUR * 60) return "";
    var top = (minute - START_HOUR * 60) * MINUTE_HEIGHT;
    return '<div class="staff-day-now" style="top:' + top + 'px"></div>';
  }

  function renderAppointmentCard(item) {
    var props = item.extendedProps || {};
    var startMinutes = readTimeMinutes(item.start);
    var endMinutes = readTimeMinutes(item.end);
    var duration = appointmentDurationMinutes(item);
    var top = Math.max((startMinutes - START_HOUR * 60) * MINUTE_HEIGHT, 0);
    var height = Math.max(duration * MINUTE_HEIGHT - 4, 30);
    var status = props.status || "confirmed";
    var statusDef = statusDefs[status] || { label: status, color: "#64748b" };
    var staffColor = props.staffColor || "#3b82f6";
    var sequence = props.sequenceNumber ? "№" + props.sequenceNumber : "";
    var account = props.billingAccountId ? '<span class="staff-day-badge">счёт</span>' : "";
    var compactClass = duration <= STEP_MINUTES ? " is-compact" : "";

    return (
      '<a class="staff-day-card' +
      compactClass +
      '" data-status="' +
      escapeHtml(status) +
      '" href="' +
      escapeHtml(detailUrl(item.id)) +
      '" style="top:' +
      top +
      "px;height:" +
      height +
      "px;--staff-color:" +
      escapeHtml(staffColor) +
      ";--status-color:" +
      escapeHtml(statusDef.color) +
      '">' +
      '<span class="staff-day-card-time">' +
      escapeHtml(formatMinutes(startMinutes) + "-" + formatMinutes(endMinutes)) +
      (sequence ? "<em>" + escapeHtml(sequence) + "</em>" : "") +
      "</span>" +
      "<strong>" +
      escapeHtml(props.child || item.title || "Занятие") +
      "</strong>" +
      "<small>" +
      escapeHtml([props.service, props.room].filter(Boolean).join(" · ")) +
      "</small>" +
      '<span class="staff-day-card-badges"><span class="staff-day-badge">' +
      escapeHtml(statusDef.label) +
      "</span>" +
      account +
      "</span>" +
      "</a>"
    );
  }

  function renderLane(staff, appointments) {
    var staffAppointments = appointments.filter(function (item) {
      return String((item.extendedProps || {}).staffId) === String(staff.id);
    });
    return (
      '<div class="staff-day-lane" data-staff-id="' +
      escapeHtml(staff.id) +
      '">' +
      renderSlotHits(staff.id) +
      renderNowLine() +
      staffAppointments.map(renderAppointmentCard).join("") +
      "</div>"
    );
  }

  function render() {
    var columns = filteredStaff();
    var appointments = filteredAppointments();
    setMeta(columns, appointments);

    if (!columns.length) {
      root.innerHTML = '<div class="staff-day-empty">Нет активных специалистов для выбранного фильтра.</div>';
      return;
    }

    var gridTemplate = "72px repeat(" + columns.length + ", minmax(220px, 1fr))";
    var html =
      '<div class="staff-day-grid" style="grid-template-columns:' +
      gridTemplate +
      ";--timeline-height:" +
      timelineHeight +
      "px;--half-hour-height:" +
      halfHourHeight +
      'px">';

    html += '<div class="staff-day-corner">Время</div>';
    columns.forEach(function (staff) {
      html +=
        '<div class="staff-day-column-head" style="--staff-color:' +
        escapeHtml(staff.color || "#00a443") +
        '">' +
        "<strong>" +
        escapeHtml(staff.full_name) +
        "</strong><span>Специалист</span></div>";
    });

    html += renderTimeColumn();
    columns.forEach(function (staff) {
      html += renderLane(staff, appointments);
    });

    html += "</div>";
    root.innerHTML = html;
  }

  function loadAppointments() {
    state.loading = true;
    root.innerHTML = '<div class="staff-day-empty">Загрузка расписания...</div>';
    var range = dayRange(state.date);
    return fetch("/api/appointments/?start=" + encodeURIComponent(range.start) + "&end=" + encodeURIComponent(range.end))
      .then(function (response) {
        if (!response.ok) throw new Error("appointments api " + response.status);
        return response.json();
      })
      .then(function (items) {
        state.appointments = items || [];
        state.loading = false;
        render();
      })
      .catch(function () {
        state.loading = false;
        root.innerHTML = '<div class="staff-day-empty">Не удалось загрузить расписание. Обновите страницу.</div>';
      });
  }

  function setDate(value) {
    state.date = value;
    if (dateInput) dateInput.value = value;
    var params = new URLSearchParams(window.location.search);
    params.set("date", value);
    var nextUrl = window.location.pathname + "?" + params.toString();
    window.history.replaceState({}, "", nextUrl);
    if (window.rmScheduleCalendar && typeof window.rmScheduleCalendar.gotoDate === "function") {
      window.rmScheduleCalendar.gotoDate(value + "T00:00:00");
    }
    loadAppointments();
  }

  function setMode(mode) {
    var staffMode = mode === "staff";
    if (calendarSection) calendarSection.classList.toggle("d-none", staffMode);
    if (staffDaySection) staffDaySection.classList.toggle("d-none", !staffMode);
    if (staffDayMode) staffDayMode.classList.toggle("is-active", staffMode);
    if (calendarMode) calendarMode.classList.toggle("is-active", !staffMode);
    window.localStorage.setItem("rmScheduleMode", mode);
    if (staffMode) {
      render();
    } else if (window.rmScheduleCalendar && typeof window.rmScheduleCalendar.updateSize === "function") {
      window.setTimeout(function () {
        window.rmScheduleCalendar.updateSize();
      }, 0);
    }
  }

  function loadStaff() {
    return fetch("/api/staff/")
      .then(function (response) {
        if (!response.ok) throw new Error("staff api " + response.status);
        return response.json();
      })
      .then(function (items) {
        state.staff = items || [];
      });
  }

  if (staffDayMode) {
    staffDayMode.addEventListener("click", function () {
      setMode("staff");
    });
  }
  if (calendarMode) {
    calendarMode.addEventListener("click", function () {
      setMode("calendar");
    });
  }
  if (dateInput) {
    dateInput.addEventListener("change", function () {
      if (dateInput.value) setDate(dateInput.value);
    });
  }
  if (prevBtn) {
    prevBtn.addEventListener("click", function () {
      setDate(shiftDay(state.date, -1));
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", function () {
      setDate(shiftDay(state.date, 1));
    });
  }
  if (todayBtn) {
    todayBtn.addEventListener("click", function () {
      setDate(todayIso());
    });
  }
  if (staffFilter) {
    staffFilter.addEventListener("change", render);
  }
  if (serviceFilter) {
    serviceFilter.addEventListener("change", render);
  }

  var params = new URLSearchParams(window.location.search);
  if (params.get("date")) {
    state.date = params.get("date");
    if (dateInput) dateInput.value = state.date;
  }

  Promise.all([loadStaff(), loadAppointments()]).then(function () {
    var savedMode = window.localStorage.getItem("rmScheduleMode");
    setMode(savedMode || "staff");
  });
})();
