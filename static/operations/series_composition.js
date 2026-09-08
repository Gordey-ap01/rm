(() => {
  const form = document.querySelector('[data-series-composition-form]');
  if (!form) return;

  const totalInput = form.querySelector('[name="staff-TOTAL_FORMS"]');
  const rows = form.querySelector('[data-staff-rows]');
  const empty = form.querySelector('[data-staff-empty-form]');
  const addButton = form.querySelector('[data-add-staff]');
  const applyButton = form.querySelector('[data-apply-composition]');
  const maxForms = 100;
  let previewIsCurrent = Boolean(applyButton);

  const setRowState = (row) => {
    const deletion = row.querySelector('input[name$="-DELETE"]');
    const status = row.querySelector('[data-row-status]');
    const removed = Boolean(deletion && deletion.checked);
    row.classList.toggle('is-removed', removed);
    if (status) status.textContent = removed ? 'Будет удалена при сохранении' : (status.dataset.newRow === 'true' ? 'Новая строка' : 'В составе');
  };

  const setRowNumber = (row, number) => {
    row.querySelectorAll('[data-row-number]').forEach((element) => {
      element.textContent = String(number);
    });
  };

  const markPreviewStale = () => {
    if (!previewIsCurrent || !applyButton) return;
    previewIsCurrent = false;
    applyButton.hidden = true;
    const note = document.createElement('p');
    note.className = 'small text-warning-emphasis mb-0';
    note.dataset.previewStale = 'true';
    note.textContent = 'Форма изменена. Посмотрите изменения заново перед сохранением.';
    applyButton.parentElement.append(note);
  };

  form.addEventListener('change', (event) => {
    const row = event.target.closest('[data-staff-row]');
    if (row) setRowState(row);
    markPreviewStale();
  });
  form.addEventListener('input', markPreviewStale);

  addButton?.addEventListener('click', () => {
    const index = Number(totalInput?.value || 0);
    if (!Number.isInteger(index) || index >= maxForms || !empty || !rows || !totalInput) return;
    const fragment = empty.content.cloneNode(true);
    fragment.querySelectorAll('[name], [id], label[for]').forEach((element) => {
      for (const attribute of ['name', 'id', 'for']) {
        if (element.hasAttribute(attribute)) element.setAttribute(attribute, element.getAttribute(attribute).replace(/__prefix__/g, String(index)));
      }
    });
    const row = fragment.querySelector('[data-staff-row]');
    row?.querySelector('[data-row-status]')?.setAttribute('data-new-row', 'true');
    setRowNumber(row, index + 1);
    rows.append(fragment);
    totalInput.value = String(index + 1);
    setRowState(rows.lastElementChild);
    markPreviewStale();
    rows.lastElementChild?.querySelector('select, input, textarea')?.focus();
    if (index + 1 >= maxForms) addButton.hidden = true;
  });

  form.querySelectorAll('[data-staff-row]').forEach((row, index) => {
    setRowNumber(row, index + 1);
    setRowState(row);
  });
})();
