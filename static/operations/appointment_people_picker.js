(function () {
  'use strict';
  document.querySelectorAll('[data-picker]').forEach(function (picker) {
    var search = picker.querySelector('.picker-search');
    var dropdown = picker.querySelector('.picker-dropdown');
    var results = picker.querySelector('.picker-results');
    var status = picker.querySelector('.picker-search-status');
    var selected = picker.querySelector('.picker-selected');
    var pagination = picker.querySelector('.picker-pagination');
    var previous = picker.querySelector('.picker-page-prev');
    var next = picker.querySelector('.picker-page-next');
    var retry = picker.querySelector('.picker-retry');
    var people = new Map();
    var page = 1;
    var active = -1;
    var timer;
    var request;
    var revision = 0;

    function selectionChanged() {
      picker.querySelector('[data-selected-count]').textContent = people.size;
      picker.querySelector('.picker-none').hidden = people.size > 0;
      selected.hidden = people.size === 0;
      picker.dispatchEvent(new Event('change', { bubbles: true }));
    }
    function add(person, notify) {
      var id = String(person.id);
      if (people.has(id)) return;
      var chip = document.createElement('div');
      chip.className = 'picker-selected-item';
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.name = picker.dataset.picker;
      input.value = id;
      input.checked = true;
      input.hidden = true;
      input.dataset.personLabel = person.label;
      var label = document.createElement('span');
      label.textContent = person.label;
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'picker-remove';
      remove.textContent = '×';
      remove.setAttribute('aria-label', 'Убрать: ' + person.label);
      remove.addEventListener('click', function () {
        people.delete(id);
        chip.remove();
        selectionChanged();
        search.focus();
      });
      chip.append(input, label, remove);
      selected.appendChild(chip);
      people.set(id, person);
      if (notify) selectionChanged();
    }
    var initial = Array.from(selected.querySelectorAll('input:checked')).map(function (input) {
      return { id: input.value, label: input.closest('label').textContent.trim() };
    });
    selected.replaceChildren();
    initial.forEach(function (person) { add(person, false); });
    selectionChanged();

    function cancelRequest() {
      clearTimeout(timer);
      revision += 1;
      if (request) request.abort();
    }
    function close() {
      cancelRequest();
      dropdown.hidden = true;
      search.setAttribute('aria-expanded', 'false');
      search.removeAttribute('aria-activedescendant');
      active = -1;
    }
    function highlight(index) {
      var options = Array.from(results.children);
      if (!options.length) return;
      active = (index + options.length) % options.length;
      options.forEach(function (option, i) { option.classList.toggle('is-active', i === active); });
      search.setAttribute('aria-activedescendant', options[active].id);
      options[active].scrollIntoView({ block: 'nearest' });
    }
    function choose(person) {
      add(person, true);
      close();
      search.value = '';
      page = 1;
      search.focus();
    }
    function render(data) {
      results.replaceChildren();
      data.results.forEach(function (person, index) {
        var option = document.createElement('li');
        option.id = search.id + '-option-' + index;
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', people.has(String(person.id)) ? 'true' : 'false');
        var name = document.createElement('span');
        name.textContent = person.label;
        option.appendChild(name);
        if (people.has(String(person.id))) {
          var mark = document.createElement('small');
          mark.textContent = 'Добавлен';
          option.appendChild(mark);
        }
        option.addEventListener('mousedown', function (event) { event.preventDefault(); });
        option.addEventListener('click', function () { choose(person); });
        results.appendChild(option);
      });
      status.textContent = data.results.length
        ? (data.has_more ? 'Уточните имя или перейдите к следующим результатам.' : 'Выберите человека из списка.')
        : 'Никого не найдено. Проверьте имя или фамилию.';
      pagination.hidden = page === 1 && !data.has_more;
      previous.disabled = page === 1;
      next.disabled = !data.has_more;
      picker.querySelector('.picker-page-label').textContent = 'Страница ' + page;
    }
    function load(delay) {
      cancelRequest();
      var current = revision;
      dropdown.hidden = false;
      search.setAttribute('aria-expanded', 'true');
      search.removeAttribute('aria-activedescendant');
      active = -1;
      results.replaceChildren();
      pagination.hidden = true;
      retry.hidden = true;
      status.textContent = 'Ищем…';
      results.setAttribute('aria-busy', 'true');
      timer = setTimeout(async function () {
        request = new AbortController();
        var url = new URL(picker.dataset.searchUrl, window.location.origin);
        url.searchParams.set('kind', picker.dataset.picker);
        url.searchParams.set('q', search.value.trim());
        url.searchParams.set('page', page);
        try {
          var response = await fetch(url, { signal: request.signal, headers: { Accept: 'application/json' } });
          if (!response.ok || response.redirected) throw new Error('Search unavailable');
          var data = await response.json();
          if (current !== revision) return;
          render(data);
        } catch (error) {
          if (current !== revision || error.name === 'AbortError') return;
          status.textContent = 'Поиск не загрузился. Выбранные люди сохранены в форме.';
          retry.hidden = false;
        } finally {
          if (current === revision) results.setAttribute('aria-busy', 'false');
        }
      }, delay);
    }
    search.addEventListener('focus', function () { if (dropdown.hidden) { page = 1; load(0); } });
    search.addEventListener('click', function () { if (dropdown.hidden) { page = 1; load(0); } });
    search.addEventListener('input', function () { page = 1; load(200); });
    search.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (dropdown.hidden) { load(0); return; }
        highlight(active + (event.key === 'ArrowDown' ? 1 : -1));
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        if (dropdown.hidden) { load(0); return; }
        if (results.children[active]) results.children[active].click();
      }
    });
    previous.addEventListener('click', function () { page -= 1; load(0); search.focus(); });
    next.addEventListener('click', function () { page += 1; load(0); search.focus(); });
    retry.addEventListener('click', function () { load(0); search.focus(); });
    picker.addEventListener('focusout', function (event) { if (!picker.contains(event.relatedTarget)) close(); });
    document.addEventListener('pointerdown', function (event) { if (!picker.contains(event.target)) close(); });
    picker.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !dropdown.hidden) { close(); search.focus(); close(); }
    });
  });
}());
