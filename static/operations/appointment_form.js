(function () {
  'use strict';
  var form = document.getElementById('appointment-editor-form');
  if (!form) return;
  function field(name) { return form.elements.namedItem(name); }
  function value(name) { var element = field(name); return element ? element.value : ''; }
  function choice(name, fallback) {
    var select = field(name);
    return select && select.value && select.selectedOptions.length ? select.selectedOptions[0].textContent.trim() : fallback;
  }
  function selected(name) {
    return Array.from(form.querySelectorAll('input[name="' + name + '"]:checked')).map(function (input) {
      return input.dataset.personLabel || input.closest('label').textContent.trim();
    });
  }
  function setSummary(index, main, detail) {
    var row = document.querySelector('[data-summary="' + index + '"]');
    if (row) { row.querySelector('strong').textContent = main; row.querySelector('small').textContent = detail; }
  }
  var statuses = {
    proposed: 'Время предложено. Решение по расписанию можно принять в карточке занятия.',
    confirmed: 'Занятие включено в согласованное расписание. Проведение отмечается отдельно.',
    reserved: 'Время занято предварительной бронью.',
    draft: 'Запись для подготовки. Черновик не блокирует время участников и кабинета.',
    completed: 'Занятие уже проведено. Изменение факта проведения выполняется отдельно.',
    no_show: 'По занятию отмечена неявка. Изменение факта посещения выполняется отдельно.'
  };
  function update() {
    var people = selected('participants');
    var staff = selected('staff_members');
    var group = people.length > 1 || staff.length > 1;
    var type = field('session_type');
    if (group && type.value !== 'group') {
      type.value = 'group';
      type.dispatchEvent(new Event('change', { bubbles: true }));
    }
    document.getElementById('appointment-composition-note').hidden = !group;
    function brief(names, empty) {
      return names.length ? names.slice(0, 2).join(', ') + (names.length > 2 ? ' и ещё ' + (names.length - 2) : '') : empty;
    }
    var names = brief(people, 'Получатель не выбран') + ' · ' + brief(staff, 'Специалист не выбран');
    setSummary(0, choice('session_type', 'Индивидуальное'), names);
    var date = value('date').split('-').reverse().join('.');
    var start = value('time');
    var duration = Number(value('duration_minutes'));
    var interval = start || 'Время не выбрано';
    if (start && duration > 0) {
      var parts = start.split(':').map(Number);
      var end = parts[0] * 60 + parts[1] + duration;
      interval += '–' + String(Math.floor(end / 60) % 24).padStart(2, '0') + ':' + String(end % 60).padStart(2, '0');
      if (end >= 1440) interval += ' (следующий день)';
    }
    setSummary(1, (date || 'Дата не выбрана') + ' · ' + interval, duration ? duration + ' минут' : 'Укажите длительность');
    setSummary(2, choice('service', 'Услуга не выбрана'), choice('room', 'Кабинет пока не выбран'));
    var status = choice('status', 'Статус не выбран');
    var explanation = statuses[value('status')] || 'Проверьте текущий статус занятия.';
    setSummary(3, status, '');
    var statusHelp = document.getElementById('id_status_helptext');
    if (statusHelp) statusHelp.textContent = explanation + ' Проведение занятия и списание оплаты отмечаются отдельно.';
  }
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  form.querySelectorAll('.appointment-help').forEach(function (help) {
    var button = help.querySelector('button');
    var content = help.querySelector('[role="tooltip"]');
    var pinned = false;
    var timer;
    button.setAttribute('aria-describedby', content.id);
    function show() { clearTimeout(timer); content.hidden = false; button.setAttribute('aria-expanded', 'true'); }
    function hide() { clearTimeout(timer); pinned = false; content.hidden = true; button.setAttribute('aria-expanded', 'false'); }
    help.addEventListener('pointerenter', show);
    help.addEventListener('pointerleave', function () {
      if (!pinned && document.activeElement !== button) timer = setTimeout(hide, 150);
    });
    button.addEventListener('focus', show);
    button.addEventListener('blur', hide);
    button.addEventListener('click', function () { if (pinned) hide(); else { pinned = true; show(); } });
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') hide(); });
    document.addEventListener('pointerdown', function (event) { if (!help.contains(event.target)) hide(); });
  });
  update();
}());
