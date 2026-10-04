const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const words = q => q.toLowerCase().split(/\s+/).filter(Boolean);

const TRAINS = JSON.parse($('#trains-data').textContent);
const STATIONS = JSON.parse($('#stations-data').textContent);
const form = $('#search-form');

// ── Search mode accordion: exactly one of the two sections is open ──
// Inputs in the closed section are disabled so they're neither validated nor submitted.
const modes = $$('details.mode');
function setMode(name) {
  modes.forEach(d => {
    const on = d.dataset.mode === name;
    if (d.open !== on) d.open = on;
    $$('input, select', d).forEach(el => { el.disabled = !on; });
  });
  $('#mode').value = name;
}
modes.forEach(d => d.addEventListener('toggle', () => {
  if (d.open) setMode(d.dataset.mode);
  else if (!modes.some(m => m.open)) setMode(modes.find(m => m !== d).dataset.mode);
}));
setMode($('#mode').value || 'train');

// ── Date fields: clicking anywhere on the box (not just the icon) opens the calendar ──
$$('input[type=date]').forEach(input => {
  const box = input.closest('.input') || input;
  box.classList.add('date-box');
  box.addEventListener('click', () => {
    if (input.disabled) return;
    try { input.showPicker(); } catch (e) { input.focus(); }  // older browsers: just focus
  });
});

// ── Generic combobox ──
function combobox(input, list, { items, filter, render, value, onPick }) {
  let shown = [], active = -1;

  function draw() {
    shown = filter(items, input.value.trim());
    list.innerHTML = shown.length
      ? shown.map((it, i) => `<li role="option" id="${list.id}-${i}" data-i="${i}"
            class="${value(it) === input.value.trim() ? 'selected' : ''}">${render(it)}</li>`).join('')
      : '<li class="none">No match</li>';
    active = -1;
  }
  const open = () => { draw(); list.hidden = false; input.setAttribute('aria-expanded', 'true'); };
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); };
  function move(i) {
    const opts = $$('li[role=option]', list);
    if (!opts.length) return;
    active = (i + opts.length) % opts.length;
    opts.forEach((el, k) => el.classList.toggle('active', k === active));
    opts[active].scrollIntoView({ block: 'nearest' });
    input.setAttribute('aria-activedescendant', opts[active].id);
  }
  function pick(it) { input.value = value(it); close(); onPick && onPick(it); }

  ['focus', 'click', 'input'].forEach(ev => input.addEventListener(ev, open));
  input.addEventListener('blur', () => setTimeout(close, 120));
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); list.hidden ? open() : move(active + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(active - 1); }
    else if (e.key === 'Enter' && !list.hidden && active >= 0) { e.preventDefault(); pick(shown[active]); }
    else if (e.key === 'Escape') close();
  });
  list.addEventListener('mousedown', e => {
    const li = e.target.closest('li[role=option]');
    if (li) { e.preventDefault(); pick(shown[+li.dataset.i]); }
  });
}

// Train names: match name/code/origin/destination; show everything when the box holds an exact pick.
combobox($('#train'), $('#train-options'), {
  items: TRAINS,
  filter: (all, q) => all.some(t => t.name === q) ? all
    : all.filter(t => words(q).every(w => `${t.name} ${t.origin} ${t.destination}`.toLowerCase().includes(w))),
  value: t => t.name,
  render: t => `
    <span class="opt-main"><b>${esc(t.short_name)}</b><span class="tc-code">${esc(t.code)}</span></span>
    <span class="opt-route"><i class="fa-solid fa-location-dot from"></i>${esc(t.origin)}
      <i class="fa-solid fa-arrow-right-long"></i><i class="fa-solid fa-location-dot to"></i>${esc(t.destination)}
      <span class="opt-time">${esc(t.departure)}</span></span>`,
  onPick: () => { if (!$('#date').value) $('#date').focus(); },
});

// Stations: prefix matches first, then busiest stations.
const stationFilter = (all, q) => {
  const n = q.toLowerCase();
  if (!n || all.some(s => s.label === q)) return [...all].sort((a, b) => b.trains - a.trains);
  return all.filter(s => s.label.toLowerCase().includes(n))
    .sort((a, b) => b.label.toLowerCase().startsWith(n) - a.label.toLowerCase().startsWith(n) || b.trains - a.trains);
};
const stationOpt = s => `<span class="opt-main"><b>${esc(s.label)}</b><small class="opt-count">${s.trains} trains</small></span>`;
combobox($('#from'), $('#from-options'), {
  items: STATIONS, filter: stationFilter, value: s => s.label, render: stationOpt,
  onPick: () => { if (!$('#to').value) $('#to').focus(); },
});
combobox($('#to'), $('#to-options'), {
  items: STATIONS, filter: stationFilter, value: s => s.label, render: stationOpt,
  onPick: () => { if (!$('#rdate').value) $('#rdate').focus(); },
});
$('#minimize').addEventListener('change', e => { $('#window').hidden = !e.target.checked; });
$('#swap').addEventListener('click', () => {
  [$('#from').value, $('#to').value] = [$('#to').value, $('#from').value];
});

// ── Login storage ──
// "Remember" keeps the pasted login in localStorage (survives closing the
// browser). Otherwise it lives in sessionStorage: kept while this tab is open
// (each search reloads the page) and gone when the tab closes.
const tokenInput = $('#token');
const remember = $('#remember-login');
const TOKEN_KEY = 'shohozToken';
const store = {
  get(area) { try { return window[area].getItem(TOKEN_KEY) || ''; } catch (e) { return ''; } },
  set(area, v) { try { v ? window[area].setItem(TOKEN_KEY, v) : window[area].removeItem(TOKEN_KEY); } catch (e) {} },
};
function showLoginState() {
  $('#login-state').textContent = !tokenInput.value.trim() ? 'optional if .env is filled'
    : remember.checked ? 'saved in this browser' : 'kept until this tab closes';
}
const saved = store.get('localStorage');
remember.checked = Boolean(saved);  // people who saved it before keep that behaviour
tokenInput.value = saved || store.get('sessionStorage');
showLoginState();
remember.addEventListener('change', () => {
  if (!remember.checked) store.set('localStorage', '');  // forget immediately
  showLoginState();
});

form.addEventListener('submit', e => {
  const err = $('#form-error');
  err.hidden = true;
  if ($('#mode').value === 'route') {
    const known = v => STATIONS.some(s => s.label.toLowerCase() === v.trim().toLowerCase());
    let msg = null;
    if (!known($('#from').value) || !known($('#to').value)) msg = 'Pick both stations from the list.';
    else if ($('#from').value.trim().toLowerCase() === $('#to').value.trim().toLowerCase()) msg = 'From and To must be different.';
    if (msg) { e.preventDefault(); err.textContent = msg; err.hidden = false; return; }
  }
  const v = tokenInput.value.trim();
  store.set('localStorage', remember.checked ? v : '');
  store.set('sessionStorage', remember.checked ? '' : v);
  $('#loading').hidden = false;
});
// Back/forward cache can restore the page with the spinner still showing.
window.addEventListener('pageshow', () => { $('#loading').hidden = true; });

// ── Train list on the home page: filter + click to select ──
const grid = $('#train-grid');
if (grid) {
  $('#train-filter').addEventListener('input', e => {
    const ws = words(e.target.value);
    let n = 0;
    $$('.train-card', grid).forEach(c => {
      const ok = ws.every(w => c.dataset.search.includes(w));
      c.hidden = !ok; n += ok;
    });
    $('#train-count').textContent = n;
    $('#train-empty').hidden = n > 0;
  });
  grid.addEventListener('click', e => {
    const card = e.target.closest('.train-card');
    if (!card) return;
    $$('.train-card.chosen', grid).forEach(c => c.classList.remove('chosen'));
    card.classList.add('chosen');
    setMode('train');
    $('#train').value = card.dataset.name;
    $('#date').scrollIntoView({ behavior: 'smooth', block: 'center' });
    $('#date').focus();
  });
}

// ── Poll the background seat job and swap in the result ──
const prog = $('#progress');
if (prog) {
  const started = Date.now();
  const dialog = $('#cancel-dialog');
  const cancelBtn = $('#cancel-job');
  cancelBtn.addEventListener('click', () => dialog.showModal());
  dialog.addEventListener('close', async () => {
    if (dialog.returnValue !== 'cancel') return;
    cancelBtn.disabled = true;
    cancelBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Cancelling…';
    try { await fetch(`/job/${prog.dataset.job}/cancel`, { method: 'POST' }); } catch (e) {}
    // polling continues and picks up the "cancelled" result
  });
  const poll = async () => {
    let j;
    try { j = await (await fetch('/job/' + prog.dataset.job)).json(); }
    catch (e) { return setTimeout(poll, 3000); }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    $('#bar-fill').style.width = pct + '%';
    let text = j.total > 1 ? `${j.done} / ${j.total} segments (${pct}%)` : 'Finding trains on this route…';
    if (j.done > 1 && j.done < j.total) {
      const eta = (Date.now() - started) / j.done * (j.total - j.done) / 1000;
      text += ` · about ${Math.ceil(eta / 60)} min left`;
    }
    if (j.waiting) {
      const left = Math.max(0, Math.round(j.waiting - Date.now() / 1000));
      const mmss = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
      text = `${j.done} / ${j.total} segments · Shohoz rate limit hit: the login is locked for about 10 minutes. Resuming in ${mmss}…`;
    }
    $('#progress-text').textContent = text;
    if ('html' in j) {
      const box = $('#matrices');
      box.innerHTML = (j.error ? '<p class="card alert"></p>' : '') + j.html;
      if (j.error) $('.alert', box).textContent = j.error;
    } else setTimeout(poll, 1500);
  };
  poll();
}
