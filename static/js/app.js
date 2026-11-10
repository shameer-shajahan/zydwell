/* ZyDwell — small vanilla helpers. No framework, no CDN. */
(function () {
  'use strict';

  var root = document.documentElement;
  var STORE = 'zydwell-theme';           // 'light' | 'dark' | absent for auto
  var ORDER = ['auto', 'light', 'dark'];

  /* ---------------- Theme ---------------- */
  function currentTheme() {
    return root.getAttribute('data-theme') || 'auto';
  }

  function applyTheme(mode) {
    if (mode === 'auto') {
      root.removeAttribute('data-theme');
      try { localStorage.removeItem(STORE); } catch (err) { /* private window */ }
    } else {
      root.setAttribute('data-theme', mode);
      try { localStorage.setItem(STORE, mode); } catch (err) { /* private window */ }
    }
    document.querySelectorAll('[data-theme-toggle]').forEach(function (button) {
      button.setAttribute('aria-label', button.getAttribute('data-label-' + mode) || mode);
      button.title = button.getAttribute('aria-label');
    });
    document.querySelectorAll('[data-theme-set]').forEach(function (button) {
      var on = button.getAttribute('data-theme-set') === mode;
      button.classList.toggle('on', on);
      button.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  document.addEventListener('click', function (event) {
    var picker = event.target.closest('[data-theme-set]');
    if (picker) {
      event.preventDefault();
      applyTheme(picker.getAttribute('data-theme-set'));
      return;
    }
    var toggle = event.target.closest('[data-theme-toggle]');
    if (!toggle) { return; }
    event.preventDefault();
    applyTheme(ORDER[(ORDER.indexOf(currentTheme()) + 1) % ORDER.length]);
  });

  applyTheme(currentTheme());

  /* ---------------- Navigation drawer ---------------- */
  function closeNav() {
    var sidebar = document.getElementById('sidebar');
    if (sidebar) { sidebar.classList.remove('open'); }
    document.body.classList.remove('nav-open');
  }

  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-burger]')) {
      event.preventDefault();
      var sidebar = document.getElementById('sidebar');
      if (!sidebar) { return; }
      var open = sidebar.classList.toggle('open');
      document.body.classList.toggle('nav-open', open);
      return;
    }
    if (event.target.closest('[data-nav-close]') || event.target.classList.contains('scrim')) {
      closeNav();
    }
  });

  /* ---------------- Stacked tables on phones ----------------
     Copies each column heading onto its cells so narrow screens can show
     label/value pairs instead of a sideways scroll. Runs once; the class is
     harmless on wide screens. */
  function labelTables() {
    document.querySelectorAll('table.data').forEach(function (table) {
      var head = table.tHead;
      if (!head || !head.rows.length || table.dataset.labelled === '1') { return; }
      var headings = Array.prototype.map.call(head.rows[0].cells, function (cell) {
        return cell.textContent.trim();
      });
      Array.prototype.forEach.call(table.tBodies, function (body) {
        Array.prototype.forEach.call(body.rows, function (row) {
          if (row.cells.length === 1 && row.cells[0].colSpan > 1) { return; }
          Array.prototype.forEach.call(row.cells, function (cell, index) {
            cell.setAttribute('data-label', headings[index] || '');
          });
        });
      });
      table.classList.add('stacked');
      table.dataset.labelled = '1';
    });
  }

  /* ---------------- Modals ---------------- */
  document.addEventListener('click', function (event) {
    var opener = event.target.closest('[data-modal-open]');
    if (opener) {
      event.preventDefault();
      var box = document.getElementById(opener.getAttribute('data-modal-open'));
      if (box) { box.classList.add('open'); }
      return;
    }
    if (event.target.closest('[data-modal-close]') || event.target.classList.contains('modal-back')) {
      var open = document.querySelector('.modal-back.open');
      if (open) { open.classList.remove('open'); }
    }
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') { return; }
    var open = document.querySelector('.modal-back.open');
    if (open) { open.classList.remove('open'); }
    closeNav();
  });

  /* ---------------- Instant client-side filter ---------------- */
  document.addEventListener('input', function (event) {
    var input = event.target.closest('[data-filter]');
    if (!input) { return; }
    var needle = input.value.trim().toLowerCase();
    document.querySelectorAll(input.getAttribute('data-filter')).forEach(function (node) {
      var hay = (node.getAttribute('data-search') || node.textContent).toLowerCase();
      node.hidden = Boolean(needle) && hay.indexOf(needle) === -1;
    });
  });

  /* ---------------- Confirm before a destructive submit ---------------- */
  document.addEventListener('submit', function (event) {
    var form = event.target.closest('[data-confirm]');
    if (form && !window.confirm(form.getAttribute('data-confirm'))) {
      event.preventDefault();
    }
  });

  /* ---------------- Dependent selects ---------------- */
  document.addEventListener('change', function (event) {
    var source = event.target.closest('[data-chain-source]');
    if (!source) { return; }
    var target = document.querySelector(source.getAttribute('data-chain-source'));
    if (!target) { return; }
    var value = source.value;
    var reset = true;
    Array.prototype.forEach.call(target.options, function (option) {
      var parent = option.getAttribute('data-parent');
      var show = !option.value || !parent || parent === value;
      option.hidden = !show;
      option.disabled = !show;
      if (show && option.selected) { reset = false; }
    });
    if (reset) { target.value = ''; }
  });

  /* ---------------- Amount follows the chosen invoice ---------------- */
  document.addEventListener('change', function (event) {
    var picker = event.target.closest('[data-balance-source]');
    if (!picker) { return; }
    var field = document.querySelector(picker.getAttribute('data-balance-source'));
    var option = picker.options[picker.selectedIndex];
    if (field && option && option.getAttribute('data-balance')) {
      field.value = option.getAttribute('data-balance');
    }
  });

  /* ---------------- Sign in ---------------- */

  /* Reveal the password. Staff type these on a phone, in a corridor. */
  document.addEventListener('click', function (event) {
    var toggle = event.target.closest('[data-reveal]');
    if (!toggle) { return; }
    var input = document.querySelector(toggle.getAttribute('data-reveal'));
    if (!input) { return; }
    var showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    toggle.setAttribute('aria-pressed', showing ? 'false' : 'true');
    var label = toggle.getAttribute(showing ? 'data-label-show' : 'data-label-hide');
    if (label) { toggle.setAttribute('aria-label', label); }
    input.focus({ preventScroll: true });
    // Put the caret back at the end rather than the start.
    var end = input.value.length;
    try { input.setSelectionRange(end, end); } catch (err) { /* type change races */ }
  });

  /* Caps Lock is the commonest reason a correct password is refused. */
  function capsWatch(event) {
    var field = event.target;
    if (!field.matches || !field.matches('input[type=password], .pw-wrap input')) { return; }
    var hint = field.closest('.auth-field');
    hint = hint && hint.querySelector('[data-caps-hint]');
    if (!hint || typeof event.getModifierState !== 'function') { return; }
    hint.classList.toggle('on', event.getModifierState('CapsLock'));
  }
  document.addEventListener('keydown', capsWatch, true);
  document.addEventListener('keyup', capsWatch, true);
  document.addEventListener('focusout', function (event) {
    var wrap = event.target.closest && event.target.closest('.auth-field');
    var hint = wrap && wrap.querySelector('[data-caps-hint]');
    if (hint) { hint.classList.remove('on'); }
  }, true);

  /* One submit only. The button keeps its name and value — disabling it before
     the POST would drop them — so a flag guards the second press. */
  document.addEventListener('submit', function (event) {
    var form = event.target.closest('[data-submit-once]');
    if (!form) { return; }
    if (form.dataset.submitting === '1') {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = '1';
    var button = form.querySelector('[data-busy-label]');
    if (button) {
      button.setAttribute('aria-busy', 'true');
      button.textContent = button.getAttribute('data-busy-label');
    }
  });


  /* =====================================================================
     Searchable select

     Every <select> is upgraded into a combobox you can type into. The native
     element stays in the DOM and keeps the value, so forms, validation and the
     server see exactly what they saw before; this only changes what is on
     screen. If the script never runs, the plain select still works.
     ===================================================================== */
  var SEARCH_FROM = 7;          // below this many options a search box is noise
  var pickerSeq = 0;
  var openPicker = null;

  function optionRows(select) {
    var rows = [];
    Array.prototype.forEach.call(select.querySelectorAll('option'), function (option) {
      if (option.hidden || option.disabled) { return; }
      var group = option.parentElement;
      rows.push({
        value: option.value,
        label: option.textContent.trim(),
        group: group && group.tagName === 'OPTGROUP' ? group.label : '',
        selected: option.selected
      });
    });
    return rows;
  }

  function currentLabel(select) {
    var option = select.options[select.selectedIndex];
    return option ? option.textContent.trim() : '';
  }

  function enhance(select) {
    if (select.multiple || select.dataset.picker === '1' ||
        select.closest('[data-no-search]')) {
      return;
    }
    select.dataset.picker = '1';

    var wrap = document.createElement('div');
    wrap.className = 'picker';
    select.parentNode.insertBefore(wrap, select);
    wrap.appendChild(select);
    select.classList.add('picker-native');
    select.setAttribute('tabindex', '-1');
    select.setAttribute('aria-hidden', 'true');

    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'picker-button';
    button.id = 'picker-btn-' + (++pickerSeq);
    button.setAttribute('role', 'combobox');
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    button.innerHTML = '<span class="picker-label"></span>' +
      '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 5l3 3 3-3" fill="none" ' +
      'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" ' +
      'stroke-linejoin="round"/></svg>';
    wrap.appendChild(button);

    var label = select.id && document.querySelector('label[for="' + select.id + '"]');
    if (label) {
      if (!label.id) { label.id = button.id + '-label'; }
      button.setAttribute('aria-labelledby', label.id + ' ' + button.id);
    }

    if (select.title) { button.title = select.title; }

    /* data-placeholder: shown while nothing is chosen ("Floor").
       data-prefix: shown before a choice, so a bare "1" reads "Floor 1". */
    function paint() {
      var text = currentLabel(select);
      var slot = button.querySelector('.picker-label');
      var placeholder = select.getAttribute('data-placeholder');
      var prefix = select.getAttribute('data-prefix');
      slot.textContent = '';
      if (select.value && prefix) {
        var lead = document.createElement('span');
        lead.className = 'picker-prefix';
        lead.textContent = prefix;
        slot.appendChild(lead);
        slot.appendChild(document.createTextNode(' ' + text));
      } else if (!select.value && placeholder) {
        slot.textContent = placeholder;
      } else {
        slot.textContent = text || placeholder || '—';
      }
      slot.classList.toggle('is-empty', !select.value);
      button.classList.toggle('has-value', Boolean(select.value));
      button.disabled = select.disabled;
    }
    paint();

    select.addEventListener('change', paint);
    // A click on the field's <label> focuses the hidden select; move it on.
    select.addEventListener('focus', function () {
      if (!button.disabled) { button.focus(); }
    });
    button.addEventListener('click', function () { openFor(select, button); });
    button.addEventListener('keydown', function (event) {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].indexOf(event.key) !== -1) {
        event.preventDefault();
        openFor(select, button);
      }
    });
    select.pickerPaint = paint;
  }

  function closePicker() {
    if (!openPicker) { return; }
    var state = openPicker;
    openPicker = null;
    state.panel.remove();
    if (state.scrim) { state.scrim.remove(); }
    state.button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('scroll', state.reposition, true);
    window.removeEventListener('resize', state.reposition);
    document.body.classList.remove('picker-open');
  }

  function openFor(select, button) {
    if (openPicker && openPicker.select === select) { closePicker(); return; }
    closePicker();
    if (select.disabled) { return; }

    var rows = optionRows(select);
    var panel = document.createElement('div');
    panel.className = 'picker-panel';
    panel.id = button.id + '-panel';

    var search = document.createElement('input');
    search.type = 'text';
    search.className = 'picker-search';
    search.setAttribute('autocomplete', 'off');
    search.setAttribute('autocapitalize', 'none');
    search.setAttribute('spellcheck', 'false');
    search.setAttribute('role', 'combobox');
    search.setAttribute('aria-expanded', 'true');
    search.placeholder = panel.getAttribute('data-search-label') ||
      (document.documentElement.lang === 'ar' ? 'ابحث…' : 'Search…');
    if (rows.length < SEARCH_FROM) { search.classList.add('is-hidden'); }
    panel.appendChild(search);

    var list = document.createElement('div');
    list.className = 'picker-list';
    list.setAttribute('role', 'listbox');
    list.id = panel.id + '-list';
    panel.appendChild(list);
    search.setAttribute('aria-controls', list.id);

    var empty = document.createElement('div');
    empty.className = 'picker-empty';
    empty.textContent = document.documentElement.lang === 'ar'
      ? 'لا توجد نتائج' : 'Nothing matches';
    panel.appendChild(empty);

    var buttons = [];
    var active = -1;

    function setActive(index) {
      buttons.forEach(function (node) { node.classList.remove('on'); });
      active = index;
      if (index < 0 || !buttons[index]) {
        search.removeAttribute('aria-activedescendant');
        return;
      }
      buttons[index].classList.add('on');
      search.setAttribute('aria-activedescendant', buttons[index].id);
      if (typeof buttons[index].scrollIntoView === 'function') {
        buttons[index].scrollIntoView({ block: 'nearest' });
      }
    }

    function choose(value) {
      select.value = value;
      select.dispatchEvent(new Event('input', { bubbles: true }));
      select.dispatchEvent(new Event('change', { bubbles: true }));
      if (select.pickerPaint) { select.pickerPaint(); }
      closePicker();
      button.focus();
    }

    function render(needle) {
      list.innerHTML = '';
      buttons = [];
      var group = null;
      var shown = 0;
      rows.forEach(function (row, index) {
        if (needle && row.label.toLowerCase().indexOf(needle) === -1) { return; }
        if (row.group && row.group !== group) {
          group = row.group;
          var heading = document.createElement('div');
          heading.className = 'picker-group';
          heading.textContent = row.group;
          list.appendChild(heading);
        }
        var item = document.createElement('button');
        item.type = 'button';
        item.className = 'picker-option';
        item.id = panel.id + '-opt-' + index;
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', row.selected ? 'true' : 'false');
        item.textContent = row.label || '—';
        if (row.selected) { item.classList.add('chosen'); }
        item.addEventListener('click', function () { choose(row.value); });
        list.appendChild(item);
        buttons.push(item);
        shown += 1;
      });
      empty.style.display = shown ? 'none' : '';
      var chosen = -1;
      buttons.forEach(function (node, index) {
        if (chosen === -1 && node.classList.contains('chosen')) { chosen = index; }
      });
      setActive(shown ? Math.max(chosen, 0) : -1);
    }

    render('');

    search.addEventListener('input', function () {
      render(search.value.trim().toLowerCase());
    });
    search.addEventListener('keydown', function (event) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        setActive(Math.min(active + 1, buttons.length - 1));
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        setActive(Math.max(active - 1, 0));
      } else if (event.key === 'Home') {
        event.preventDefault(); setActive(0);
      } else if (event.key === 'End') {
        event.preventDefault(); setActive(buttons.length - 1);
      } else if (event.key === 'Enter') {
        event.preventDefault();
        if (buttons[active]) { buttons[active].click(); }
      } else if (event.key === 'Escape') {
        event.preventDefault();
        closePicker();
        button.focus();
      }
    });

    var scrim = null;
    if (window.matchMedia('(max-width: 680px)').matches) {
      panel.classList.add('as-sheet');
      scrim = document.createElement('div');
      scrim.className = 'picker-scrim';
      scrim.addEventListener('click', closePicker);
      document.body.appendChild(scrim);
      document.body.classList.add('picker-open');
    }

    function reposition() {
      if (panel.classList.contains('as-sheet')) { return; }
      var box = button.getBoundingClientRect();
      var below = window.innerHeight - box.bottom;
      panel.style.width = Math.max(box.width, 240) + 'px';
      panel.style.left = Math.min(box.left, window.innerWidth - panel.offsetWidth - 8) + 'px';
      if (below < 260 && box.top > below) {
        panel.style.top = '';
        panel.style.bottom = (window.innerHeight - box.top + 6) + 'px';
        panel.style.maxHeight = (box.top - 16) + 'px';
      } else {
        panel.style.bottom = '';
        panel.style.top = (box.bottom + 6) + 'px';
        panel.style.maxHeight = (below - 16) + 'px';
      }
    }

    document.body.appendChild(panel);
    reposition();
    button.setAttribute('aria-expanded', 'true');
    button.setAttribute('aria-controls', panel.id);
    document.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);

    openPicker = { select: select, button: button, panel: panel, scrim: scrim,
                   reposition: reposition };
    search.focus({ preventScroll: true });
  }

  document.addEventListener('mousedown', function (event) {
    if (!openPicker) { return; }
    if (openPicker.panel.contains(event.target) ||
        openPicker.button.contains(event.target)) { return; }
    closePicker();
  });

  function enhanceAll(root) {
    (root || document).querySelectorAll('select').forEach(enhance);
  }

  /* Auto-submitting filter bars, without inline javascript. */
  document.addEventListener('change', function (event) {
    var field = event.target.closest('[data-autosubmit]');
    if (field && field.form) { field.form.submit(); }
  });

  /* Building → Floor → Apartment → Room → Bed. A new choice at one level makes
     the levels under it meaningless, so they are emptied (and left out of the
     request) before the list reloads with the next level's options. */
  document.addEventListener('change', function (event) {
    var select = event.target.closest('[data-loc-level]');
    if (!select || !select.form) { return; }
    var level = Number(select.getAttribute('data-loc-level'));
    select.form.querySelectorAll('[data-loc-level]').forEach(function (other) {
      if (Number(other.getAttribute('data-loc-level')) > level) {
        other.value = '';
        other.disabled = true;
      }
    });
    select.form.submit();
  });

  /* ---------------- Tick-all checkboxes ----------------
     Used by the staff permission grid: one box for everything, one per column,
     one per row. They only move the form's own checkboxes — nothing is saved
     until the form is submitted. */
  function realBoxes(scope) {
    return Array.prototype.filter.call(
      scope.querySelectorAll('input[type=checkbox]'),
      function (box) {
        return !box.hasAttribute('data-check-all') &&
               !box.hasAttribute('data-check-col') &&
               !box.hasAttribute('data-check-row');
      });
  }

  function applyTo(boxes, checked) {
    boxes.forEach(function (box) {
      if (box.checked === checked) { return; }
      box.checked = checked;
      box.dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  document.addEventListener('change', function (event) {
    var box = event.target;
    if (!box.matches || box.type !== 'checkbox') { return; }

    if (box.hasAttribute('data-check-all')) {
      var scope = document.querySelector(box.getAttribute('data-check-all'));
      if (scope) { applyTo(realBoxes(scope), box.checked); }
      return;
    }
    if (box.hasAttribute('data-check-col')) {
      var table = box.closest('table');
      var index = parseInt(box.getAttribute('data-check-col'), 10);
      if (!table) { return; }
      var column = [];
      Array.prototype.forEach.call(table.tBodies, function (body) {
        Array.prototype.forEach.call(body.rows, function (row) {
          var cell = row.cells[index];
          if (cell) { column = column.concat(realBoxes(cell)); }
        });
      });
      applyTo(column, box.checked);
      return;
    }
    if (box.hasAttribute('data-check-row')) {
      var row = box.closest('tr');
      if (row) { applyTo(realBoxes(row), box.checked); }
    }
  });

  /* ---------------- Copy to clipboard ---------------- */
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy]');
    if (!button) { return; }
    var field = document.querySelector(button.getAttribute('data-copy'));
    if (!field) { return; }
    var done = function () {
      var original = button.dataset.original || button.textContent;
      button.dataset.original = original;
      button.textContent = button.getAttribute('data-copied') || 'Copied';
      window.setTimeout(function () { button.textContent = original; }, 1600);
    };
    field.select();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(field.value).then(done, function () {
        // Fall back to the old command on browsers that refuse the API.
        try { document.execCommand('copy'); done(); } catch (err) { /* select is enough */ }
      });
    } else {
      try { document.execCommand('copy'); done(); } catch (err) { /* select is enough */ }
    }
  });

  /* ---------------- Print ---------------- */
  document.addEventListener('click', function (event) {
    if (event.target.closest('[data-print]')) {
      event.preventDefault();
      window.print();
    }
  });

  /* ---------------- Filter bars ----------------
     One row of pills that scrolls sideways when it runs out of room. The
     edges fade on whichever side has more, and a mouse wheel scrolls it. */
  function filterEdges(row) {
    var room = row.scrollWidth - row.clientWidth;
    var at = Math.abs(row.scrollLeft);          // negative in right-to-left
    row.classList.toggle('fade-start', room > 2 && at > 2);
    row.classList.toggle('fade-end', room > 2 && at < room - 2);
  }

  /* A search pill says just "Search" until it is tapped, then the full hint. */
  document.addEventListener('focusin', function (event) {
    var field = event.target.closest && event.target.closest('input[data-hint]');
    if (!field) { return; }
    field.dataset.short = field.placeholder;
    field.placeholder = field.getAttribute('data-hint');
  });
  document.addEventListener('focusout', function (event) {
    var field = event.target.closest && event.target.closest('input[data-hint]');
    if (field && field.dataset.short) { field.placeholder = field.dataset.short; }
  });

  function filterRows() {
    document.querySelectorAll('[data-fb-scroll]').forEach(function (row) {
      var refresh = function () { filterEdges(row); };
      refresh();
      row.addEventListener('scroll', refresh, { passive: true });
      row.addEventListener('transitionend', refresh);
      row.addEventListener('focusin', refresh);
      row.addEventListener('focusout', refresh);
      row.addEventListener('wheel', function (event) {
        if (Math.abs(event.deltaX) >= Math.abs(event.deltaY)) { return; }
        var room = row.scrollWidth - row.clientWidth;
        if (room <= 2) { return; }
        var step = document.documentElement.dir === 'rtl' ? -event.deltaY : event.deltaY;
        var at = Math.abs(row.scrollLeft);
        // At the end already: let the page scroll as normal.
        if ((step > 0 && at >= room - 1) || (step < 0 && at <= 1)) { return; }
        row.scrollLeft += step;
        event.preventDefault();
      }, { passive: false });
    });
    window.addEventListener('resize', function () {
      document.querySelectorAll('[data-fb-scroll]').forEach(filterEdges);
    });
  }

  function boot() {
    labelTables();
    enhanceAll();
    filterRows();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
