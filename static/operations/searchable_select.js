(function () {
  var SELECTOR = "select:not([multiple]):not([data-searchable='off']):not(.rm-native-select)";
  var MAX_VISIBLE_OPTIONS = 80;
  var idCounter = 0;

  function normalize(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/ё/g, "е")
      .trim();
  }

  function optionLabel(option) {
    return (option && option.textContent ? option.textContent : "").replace(/\s+/g, " ").trim();
  }

  function isBlankLabel(label) {
    return /^[-\u2014\u2013\s]+$/.test(label);
  }

  function displayLabel(option) {
    var label = optionLabel(option);
    if (!option || (option.value === "" && isBlankLabel(label))) {
      return "Не выбрано";
    }
    return label;
  }

  function selectedLabel(select) {
    var option = select.options[select.selectedIndex];
    if (!option) return "";
    var label = optionLabel(option);
    if (option.value === "" && isBlankLabel(label)) return "";
    return label;
  }

  function allOptions(select) {
    return Array.from(select.options).map(function (option, index) {
      return {
        index: index,
        value: option.value,
        label: displayLabel(option),
        disabled: option.disabled,
        selected: option.selected,
      };
    });
  }

  function dispatchSelectChange(select) {
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function enhanceSelect(select) {
    if (select.dataset.rmSearchableReady === "true") return;
    if (select.closest("[data-searchable-scope='off']")) return;

    select.dataset.rmSearchableReady = "true";
    select.dataset.rmRequired = select.required ? "true" : "false";
    if (select.required) {
      select.required = false;
    }

    var wrapper = document.createElement("div");
    wrapper.className = "rm-combobox";
    wrapper.dataset.selectId = select.id || "";
    if (select.classList.contains("form-select-sm")) {
      wrapper.classList.add("rm-combobox-sm");
    }

    var input = document.createElement("input");
    input.type = "text";
    input.className = "rm-combobox-input";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.value = selectedLabel(select);
    input.placeholder = select.dataset.searchPlaceholder || "Начните вводить...";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-required", select.dataset.rmRequired);
    if (select.id) {
      input.id = select.id + "__search";
    } else {
      input.id = "rm-combobox-" + ++idCounter;
    }

    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "rm-combobox-toggle";
    toggle.setAttribute("aria-label", "Открыть список");
    toggle.innerHTML = '<i class="bi bi-chevron-down" aria-hidden="true"></i>';

    var list = document.createElement("div");
    list.className = "rm-combobox-list";
    list.id = input.id + "__list";
    list.setAttribute("role", "listbox");
    input.setAttribute("aria-controls", list.id);

    var empty = document.createElement("div");
    empty.className = "rm-combobox-empty";
    empty.textContent = "Ничего не найдено";

    wrapper.appendChild(input);
    wrapper.appendChild(toggle);
    wrapper.appendChild(list);
    select.classList.add("rm-native-select");
    select.insertAdjacentElement("afterend", wrapper);

    var isOpen = false;
    var activeIndex = -1;
    var renderedOptions = [];
    var observer = new MutationObserver(function () {
      syncFromSelect();
      render(input.value, true);
    });

    function setDisabledState() {
      var disabled = select.disabled;
      input.disabled = disabled;
      toggle.disabled = disabled;
      wrapper.classList.toggle("is-disabled", disabled);
    }

    function syncFromSelect() {
      input.value = selectedLabel(select);
      wrapper.classList.toggle("is-invalid", false);
      setDisabledState();
    }

    function setOpen(nextOpen) {
      if (select.disabled) return;
      isOpen = nextOpen;
      wrapper.classList.toggle("is-open", isOpen);
      input.setAttribute("aria-expanded", String(isOpen));
      if (!isOpen) {
        activeIndex = -1;
        input.removeAttribute("aria-activedescendant");
      }
    }

    function matches(option, query) {
      if (!query) return true;
      return normalize(option.label).includes(query) || normalize(option.value).includes(query);
    }

    function render(queryText, keepOpen) {
      var query = normalize(queryText);
      var options = allOptions(select).filter(function (option) {
        return matches(option, query);
      });
      var clipped = options.slice(0, MAX_VISIBLE_OPTIONS);
      renderedOptions = clipped;
      list.innerHTML = "";

      if (!clipped.length) {
        list.appendChild(empty);
        activeIndex = -1;
        input.removeAttribute("aria-activedescendant");
      } else {
        clipped.forEach(function (option, index) {
          var button = document.createElement("button");
          button.type = "button";
          button.className = "rm-combobox-option";
          button.setAttribute("role", "option");
          button.id = input.id + "__option_" + index;
          button.dataset.index = String(index);
          button.disabled = option.disabled;
          button.setAttribute("aria-selected", String(option.value === select.value));
          button.innerHTML =
            '<span class="rm-combobox-option-label"></span>' +
            (option.value === select.value ? '<i class="bi bi-check2" aria-hidden="true"></i>' : "");
          button.querySelector(".rm-combobox-option-label").textContent = option.label || "Без названия";
          button.addEventListener("mousedown", function (event) {
            event.preventDefault();
          });
          button.addEventListener("click", function () {
            choose(index);
          });
          list.appendChild(button);
        });

        if (options.length > clipped.length) {
          var more = document.createElement("div");
          more.className = "rm-combobox-more";
          more.textContent = "Еще " + (options.length - clipped.length) + " вариантов. Уточните поиск.";
          list.appendChild(more);
        }

        if (activeIndex < 0 || activeIndex >= clipped.length) {
          activeIndex = 0;
        }
        markActive();
      }

      if (keepOpen) {
        setOpen(true);
      }
    }

    function markActive() {
      Array.from(list.querySelectorAll(".rm-combobox-option")).forEach(function (node, index) {
        var active = index === activeIndex;
        node.classList.toggle("is-active", active);
        if (active) {
          input.setAttribute("aria-activedescendant", node.id);
          node.scrollIntoView({ block: "nearest" });
        }
      });
    }

    function choose(index) {
      var option = renderedOptions[index];
      if (!option || option.disabled) return;
      select.value = option.value;
      input.value = option.label;
      wrapper.classList.remove("is-invalid");
      dispatchSelectChange(select);
      setOpen(false);
      input.focus();
    }

    function move(delta) {
      if (!renderedOptions.length) return;
      var next = activeIndex;
      for (var step = 0; step < renderedOptions.length; step += 1) {
        next = (next + delta + renderedOptions.length) % renderedOptions.length;
        if (!renderedOptions[next].disabled) {
          activeIndex = next;
          markActive();
          return;
        }
      }
    }

    input.addEventListener("focus", function () {
      render(input.value, true);
      input.select();
    });

    input.addEventListener("input", function () {
      wrapper.classList.remove("is-invalid");
      activeIndex = 0;
      render(input.value, true);
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!isOpen) render(input.value, true);
        move(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (!isOpen) render(input.value, true);
        move(-1);
      } else if (event.key === "Enter") {
        if (isOpen) {
          event.preventDefault();
          choose(activeIndex);
        }
      } else if (event.key === "Escape") {
        event.preventDefault();
        syncFromSelect();
        setOpen(false);
      }
    });

    input.addEventListener("blur", function () {
      window.setTimeout(function () {
        if (!wrapper.contains(document.activeElement)) {
          syncFromSelect();
          setOpen(false);
        }
      }, 120);
    });

    toggle.addEventListener("mousedown", function (event) {
      event.preventDefault();
    });

    toggle.addEventListener("click", function () {
      if (isOpen) {
        syncFromSelect();
        setOpen(false);
      } else {
        input.focus();
        render("", true);
      }
    });

    select.addEventListener("change", syncFromSelect);
    observer.observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "selected"] });
    setDisabledState();
  }

  function initSearchableSelects(root) {
    var scope = root || document;
    scope.querySelectorAll(SELECTOR).forEach(enhanceSelect);
  }

  function validateRequiredSelects(form) {
    var invalid = null;
    form.querySelectorAll("select.rm-native-select[data-rm-required='true']").forEach(function (select) {
      if (invalid || select.disabled || select.value) return;
      var wrapper = select.nextElementSibling;
      if (!wrapper || !wrapper.classList.contains("rm-combobox")) return;
      wrapper.classList.add("is-invalid");
      invalid = wrapper.querySelector(".rm-combobox-input");
    });
    if (invalid) {
      invalid.focus();
      return false;
    }
    return true;
  }

  document.addEventListener("DOMContentLoaded", function () {
    initSearchableSelects(document);
  });

  document.addEventListener("submit", function (event) {
    if (!validateRequiredSelects(event.target)) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  document.addEventListener("htmx:afterSwap", function (event) {
    initSearchableSelects(event.target);
  });

  window.rmInitSearchableSelects = initSearchableSelects;
})();
