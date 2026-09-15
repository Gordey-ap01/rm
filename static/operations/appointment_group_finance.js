(function () {
  'use strict';
  var form = document.getElementById('appointment-editor-form');
  var section = document.getElementById('appointment-finance-section');
  if (!form || !section) return;
  var program = form.elements.namedItem('program_block');
  var account = form.elements.namedItem('billing_account');
  var fields = document.getElementById('appointment-finance-fields');
  var reset = document.getElementById('group-finance-reset');
  var originals = [section.dataset.originalProgram, section.dataset.originalAccount];
  var hasOriginals = originals.some(Boolean);
  var errors = fields.querySelectorAll('.form-error');
  var errorsDismissed = false;
  var wasGroup = false;
  function isGroup() {
    return form.elements.namedItem('session_type').value === 'group'
      || form.querySelectorAll('input[name="participants"]:checked').length > 1
      || form.querySelectorAll('input[name="staff_members"]:checked').length > 1;
  }
  function changed() { return program.value !== originals[0] || account.value !== originals[1]; }
  function canRestore() {
    return [program, account].every(function (select, index) {
      return !originals[index] || Array.from(select.options).some(function (option) { return option.value === originals[index]; });
    });
  }
  function update() {
    var group = isGroup();
    var modified = changed();
    var ownerRemoved = hasOriginals && section.dataset.originalChild
      && !Array.from(form.querySelectorAll('input[name="participants"]:checked')).some(function (input) { return input.value === section.dataset.originalChild; });
    document.getElementById('individual-finance-help').hidden = group;
    document.getElementById('group-finance-help').hidden = !group;
    document.getElementById('group-finance-existing').hidden = !hasOriginals;
    document.getElementById('group-finance-changes').hidden = !modified;
    document.getElementById('group-finance-owner-warning').hidden = !ownerRemoved;
    fields.hidden = group && !modified && !ownerRemoved && (!errors.length || errorsDismissed);
    reset.disabled = !canRestore();
    reset.textContent = hasOriginals ? 'Вернуть сохранённые значения' : 'Настроить после сохранения';
    document.getElementById('group-finance-warning').textContent = canRestore()
      ? (hasOriginals
        ? 'В общих полях появились новые значения. Верните сохранённые значения и измените привязки отдельно в карточке участника.'
        : 'В общих полях уже выбрана программа или счёт. Они не распределяются на всю группу. Кнопка ниже очистит только эти два поля, чтобы настроить каждого участника после сохранения.')
      : 'Сохранённые значения недоступны для текущего получателя или услуги. Проверьте состав и услугу; автоматическое восстановление этих полей недоступно.';
    if (group && (!wasGroup || modified || ownerRemoved)) section.open = true;
    wasGroup = group;
  }
  reset.addEventListener('click', function () {
    if (!canRestore()) return;
    program.value = originals[0];
    account.value = originals[1];
    errorsDismissed = true;
    errors.forEach(function (error) { error.hidden = true; });
    [program, account].forEach(function (select) { select.dispatchEvent(new Event('change', { bubbles: true })); });
    update();
    section.querySelector('summary').focus();
  });
  form.addEventListener('change', update);
  form.addEventListener('submit', function (event) {
    if (isGroup() && changed()) {
      event.preventDefault();
      section.open = true;
      reset.focus();
    }
  });
  update();
}());
