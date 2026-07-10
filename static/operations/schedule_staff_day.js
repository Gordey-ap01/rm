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
    laneMode: "staff",
    staff: [],
    rooms: [],
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
  var staffLaneMode = document.getElementById("staffLaneMode");
  var roomLaneMode = document.getElementById("roomLaneMode");
  var meta = document.getElementById("staffDayMeta");
  var statusSummary = document.getElementById("staffDayStatusSummary");
  var staffFilter = document.getElementById("staffFilter");
  var serviceFilter = document.getElementById("serviceFilter");
  var roomFilter = document.getElementById("roomFilter");
  var statusFilter = document.getElementById("statusFilter");
  var statusOrder = ["proposed", "reserved", "confirmed", "completed", "rescheduled", "cancelled", "no_show", "draft"];

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

  function createUrl(column, minutes) {
    var url = new URL(root.dataset.createUrl || "/appointments/new/", window.location.origin);
    url.searchParams.set("date", state.date);
    url.searchParams.set("time", formatMinutes(minutes));
    var staffId = selectedStaffId();
    var serviceId = selectedServiceId();
    var roomId = selectedRoomId();
    if (state.laneMode === "room") {
      url.searchParams.set("room_id", column.id);
      if (staffId) url.searchParams.set("staff_id", staffId);
    } else {
      url.searchParams.set("staff_id", column.id);
      if (roomId) url.searchParams.set("room_id", roomId);
    }
    if (serviceId) url.searchParams.set("service_id", serviceId);
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

  function selectedRoomId() {
    return roomFilter && roomFilter.value ? String(roomFilter.value) : "";
  }

  function selectedStatus() {
    return statusFilter && statusFilter.value ? String(statusFilter.value) : "";
  }

  function staffMatches(props, staffId) {
    var ids = props.staffIds || (props.staffId ? [props.staffId] : []);
    return ids.map(String).indexOf(String(staffId)) !== -1;
  }

  function hasStaff(props) {
    var ids = props.staffIds || (props.staffId ? [props.staffId] : []);
    return ids.length > 0;
  }

  function roomMatches(props, roomId) {
    return String(props.roomId || "") === String(roomId);
  }

  function filteredColumns() {
    if (state.laneMode === "room") {
      var selectedRoom = selectedRoomId();
      if (!selectedRoom) return state.rooms;
      return state.rooms.filter(function (item) {
        return String(item.id) === selectedRoom;
      });
    }
    var selected = selectedStaffId();
    if (!selected) return state.staff;
    return state.staff.filter(function (item) {
      return String(item.id) === selected;
    });
  }

  function filteredAppointments() {
    var staffId = selectedStaffId();
    var serviceId = selectedServiceId();
    var roomId = selectedRoomId();
    var status = selectedStatus();
    return state.appointments
      .filter(function (item) {
        var props = item.extendedProps || {};
        if (String(item.start).slice(0, 10) !== state.date) return false;
        if (state.laneMode === "staff" && !hasStaff(props)) return false;
        if (state.laneMode === "room" && !props.roomId) return false;
        if (staffId && !staffMatches(props, staffId)) return false;
        if (serviceId && String(props.serviceId) !== serviceId) return false;
        if (roomId && String(props.roomId) !== roomId) return false;
        if (status && String(props.status) !== status) return false;
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
    var columnLabel = state.laneMode === "room" ? "каб." : "спец.";
    meta.innerHTML =
      "<span>" + escapeHtml(dateLabel) + "</span>" +
      "<span>" + columns.length + " " + columnLabel + "</span>" +
      "<span>" + appointments.length + " зан.</span>";
  }

  function renderStatusSummary(appointments) {
    if (!statusSummary) return;
    if (!appointments.length) {
      statusSummary.innerHTML = '<span class="staff-day-status-empty">Нет занятий по фильтрам</span>';
      return;
    }
    var counts = {};
    appointments.forEach(function (item) {
      var status = (item.extendedProps && item.extendedProps.status) || "confirmed";
      counts[status] = (counts[status] || 0) + 1;
    });
    var statuses = statusOrder.filter(function (status) {
      return counts[status];
    });
    statusSummary.innerHTML = statuses
      .map(function (status) {
        var def = statusDefs[status] || { label: status, color: "#64748b" };
        return (
          '<span class="staff-day-status-chip" style="--status-color:' +
          escapeHtml(def.color) +
          '">' +
          escapeHtml(def.label) +
          " " +
          counts[status] +
          "</span>"
        );
      })
      .join("");
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

  function renderSlotHits(column) {
    var html = "";
    for (var minute = START_HOUR * 60; minute < END_HOUR * 60; minute += STEP_MINUTES) {
      var top = (minute - START_HOUR * 60) * MINUTE_HEIGHT;
      html +=
        '<a class="staff-day-slot-hit" href="' +
        escapeHtml(createUrl(column, minute)) +
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
    var group = props.participantCount > 1 ? '<span class="staff-day-badge">группа ' + escapeHtml(props.participantCount) + "</span>" : "";
    var multiStaff = props.staffCount > 1 ? '<span class="staff-day-badge">' + escapeHtml(props.staffCount) + " спец.</span>" : "";
    var program = props.programBlock ? '<span class="staff-day-badge">каскад</span>' : "";
    var compactClass = duration <= STEP_MINUTES ? " is-compact" : "";
    var groupTitle = item.title ? String(item.title).split(" / ")[0] : "";
    var childLabel = props.participantCount > 1 ? groupTitle || "Группа" : props.child || item.title || "Занятие";
    var title = [
      formatMinutes(startMinutes) + "-" + formatMinutes(endMinutes),
      props.child,
      props.staff,
      props.service,
      props.room,
      props.programBlock,
    ]
      .filter(Boolean)
      .join("\n");

    return (
      '<a class="staff-day-card' +
      compactClass +
      '" data-status="' +
      escapeHtml(status) +
      '" href="' +
      escapeHtml(detailUrl(item.id)) +
      '" title="' +
      escapeHtml(title) +
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
      escapeHtml(childLabel) +
      "</strong>" +
      "<small>" +
      escapeHtml([props.service, props.room].filter(Boolean).join(" · ")) +
      "</small>" +
      '<span class="staff-day-card-badges"><span class="staff-day-badge">' +
      escapeHtml(statusDef.label) +
      "</span>" +
      group +
      multiStaff +
      program +
      account +
      "</span>" +
      "</a>"
    );
  }

  function columnName(column) {
    return column.full_name || column.name || "";
  }

  function columnColor(column) {
    return column.color || (state.laneMode === "room" ? "#64748b" : "#00a443");
  }

  function columnLabel() {
    return state.laneMode === "room" ? "Кабинет" : "Специалист";
  }

  function appointmentInColumn(item, column) {
    var props = item.extendedProps || {};
    if (state.laneMode === "room") return roomMatches(props, column.id);
    return staffMatches(props, column.id);
  }

  function renderLane(column, appointments) {
    var columnAppointments = appointments.filter(function (item) {
      return appointmentInColumn(item, column);
    });
    return (
      '<div class="staff-day-lane" data-column-id="' +
      escapeHtml(column.id) +
      '">' +
      renderSlotHits(column) +
      renderNowLine() +
      columnAppointments.map(renderAppointmentCard).join("") +
      "</div>"
    );
  }

  function render() {
    var columns = filteredColumns();
    var appointments = filteredAppointments();
    setMeta(columns, appointments);
    renderStatusSummary(appointments);

    if (!columns.length) {
      root.innerHTML = '<div class="staff-day-empty">Нет колонок для выбранного фильтра.</div>';
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
    columns.forEach(function (column) {
      html +=
        '<div class="staff-day-column-head" style="--staff-color:' +
        escapeHtml(columnColor(column)) +
        '">' +
        "<strong>" +
        escapeHtml(columnName(column)) +
        "</strong><span>" +
        columnLabel() +
        "</span></div>";
    });

    html += renderTimeColumn();
    columns.forEach(function (column) {
      html += renderLane(column, appointments);
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
    if (window.rmScheduleRefreshCreateLink) window.rmScheduleRefreshCreateLink();
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

  function setLaneMode(mode) {
    state.laneMode = mode === "room" ? "room" : "staff";
    if (staffLaneMode) staffLaneMode.classList.toggle("is-active", state.laneMode === "staff");
    if (roomLaneMode) roomLaneMode.classList.toggle("is-active", state.laneMode === "room");
    window.localStorage.setItem("rmScheduleLaneMode", state.laneMode);
    render();
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

  function loadRooms() {
    return fetch("/api/rooms/")
      .then(function (response) {
        if (!response.ok) throw new Error("rooms api " + response.status);
        return response.json();
      })
      .then(function (items) {
        state.rooms = items || [];
      })
      .catch(function () {
        state.rooms = [];
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
  if (staffLaneMode) {
    staffLaneMode.addEventListener("click", function () {
      setLaneMode("staff");
    });
  }
  if (roomLaneMode) {
    roomLaneMode.addEventListener("click", function () {
      setLaneMode("room");
    });
  }
  if (staffFilter) {
    staffFilter.addEventListener("change", render);
  }
  if (serviceFilter) {
    serviceFilter.addEventListener("change", render);
  }
  if (roomFilter) {
    roomFilter.addEventListener("change", render);
  }
  if (statusFilter) {
    statusFilter.addEventListener("change", render);
  }

  var params = new URLSearchParams(window.location.search);
  if (params.get("date")) {
    state.date = params.get("date");
    if (dateInput) dateInput.value = state.date;
  }

  var savedLaneMode = window.localStorage.getItem("rmScheduleLaneMode");
  if (savedLaneMode === "room") {
    state.laneMode = "room";
    if (staffLaneMode) staffLaneMode.classList.remove("is-active");
    if (roomLaneMode) roomLaneMode.classList.add("is-active");
  }

  Promise.all([loadStaff(), loadRooms(), loadAppointments()]).then(function () {
    var savedMode = window.localStorage.getItem("rmScheduleMode");
    setMode(savedMode || "staff");
  });
})();
