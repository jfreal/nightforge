// PR dashboard page. Data comes from /api/prs; Merge and Send post back to the server.
'use strict';

const FILTERS = [
  { id: 'all', name: 'All' },
  { id: 'ready', name: 'Ready to merge' },
  { id: 'fix', name: 'Needs a fix' },
  { id: 'wait', name: 'Waiting' },
  { id: 'draft', name: 'Drafts' }
];
const RISK = { low: 'Low', med: 'Medium', high: 'High' };
const REFRESH_MS = 60000;
const TICK_MS = 30000;

const state = { rows: [], filter: 'all', open: new Set(), local: {}, error: '', fetchedAt: null, screens: {} };

const ICON = {
  merge: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#1F4E8C" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="5" r="2.5"></circle><circle cx="6" cy="19" r="2.5"></circle><circle cx="18" cy="12" r="2.5"></circle><path d="M6 7.5v9M6 7.5c0 4 4 4.5 9.5 4.5"></path></svg>',
  claude: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#EA580C" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4 17 6-5-6-5"></path><path d="M12 19h8"></path></svg>',
  hard: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 20h20L12 3z"></path><path d="M12 10v4"></path><path d="M12 17h.01"></path></svg>',
  soft: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="8"></circle><path d="M12 8v4h3"></path></svg>',
  check: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 5 5 9-10"></path></svg>',
  review: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#EA580C" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"></circle><path d="m16 16 4.5 4.5"></path><path d="m8.5 11 2 2 3.5-4"></path></svg>',
  camera: '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#1F4E8C" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h3l2-3h6l2 3h3v11H4z"></path><circle cx="12" cy="13" r="3.5"></circle></svg>',
  github: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#64748B" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6"></path><path d="M20 4 10 14"></path><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"></path></svg>'
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const $ = (id) => document.getElementById(id);
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function ageOf(created, now) {
  const mins = Math.max(0, Math.floor((now - Date.parse(created)) / 60000));
  const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
  return { hours: mins / 60, text: d > 0 ? d + 'd ' + h + 'h' : h + 'h ' + String(m).padStart(2, '0') + 'm' };
}
function heat(hours) {
  if (hours < 24) return { label: 'New', bar: '#CBD2DC', text: '#3B4654' };
  if (hours < 72) return { label: 'Warming', bar: '#FFD166', text: '#1A1A1A' };
  if (hours < 120) return { label: 'Hot', bar: '#FFB020', text: '#1A1A1A' };
  return { label: 'Burning', bar: '#EA580C', text: '#1A1A1A' };
}

function chips(r) {
  const word = {
    checks: { pass: ['Passed', 'good'], fail: ['Failed', 'bad'], running: ['Running', 'wait'], none: ['No checks', 'wait'] },
    review: { done: ['Done', 'good'], changes: ['Changes asked', 'bad'], stale: ['Older commit', 'wait'], pending: ['Not yet', 'wait'] },
    verify: { pass: ['Passed', 'good'], fail: ['Failed', 'bad'], none: ['Not run', 'wait'] },
    conflict: { no: ['None', 'good'], yes: ['Yes', 'bad'], checking: ['Checking', 'wait'] }
  };
  return [['Checks', word.checks[r.checks]], ['CodeRabbit review', word.review[r.review]],
          ['Verify', word.verify[r.verify]], ['Conflicts', word.conflict[r.conflict]]];
}

// Why a row gets Fix and merge instead of Merge, in the words of its blockers.
function whyNotReady(r) {
  const reasons = r.blockers.map((b) => b.text.charAt(0).toLowerCase() + b.text.slice(1));
  if (r.draft) reasons.unshift('it is a draft');
  return reasons.length ? reasons.join(', ') + '.' : 'GitHub has not marked it ready yet.';
}

function rowHtml(r, now) {
  const age = ageOf(r.created, now), h = heat(age.hours), local = state.local[r.key] || {}, open = state.open.has(r.key);
  const blockers = r.blockers.length
    ? r.blockers.map((b) => `<span class="blocker ${b.hard ? '' : 'soft'}">${b.hard ? ICON.hard : ICON.soft}${esc(b.text)}</span>`).join('')
    : `<span class="blocker clear">${ICON.check}Nothing blocking</span>`;
  let actions = '';
  // Every row can be merged: a ready PR merges now, any other hands Claude a fix-then-merge job.
  if (local.merged) actions += '<span class="done merged">Merged</span>';
  else if (local.fixing) actions += `<span class="done">Claude is fixing it, then merging${esc(local.where)}</span>`;
  else if (r.lane === 'ready') actions += `<button class="btn" data-act="merge" data-key="${esc(r.key)}" ${local.busy ? 'disabled' : ''}>${ICON.merge}Merge</button>`;
  else {
    actions += `<button class="btn" data-act="fixmerge" data-key="${esc(r.key)}" ${local.busy ? 'disabled' : ''}>${ICON.merge}Fix and merge</button>`;
    actions += `<span class="why">Not plain Merge: ${esc(whyNotReady(r))}</span>`;
  }
  // No review yet (or only of an older commit): one click asks Claude for one.
  if (local.reviewing) actions += `<span class="done">Claude is reviewing it${esc(local.where)}</span>`;
  else if (!local.merged && (r.review === 'pending' || r.review === 'stale') && r.verify !== 'pass')
    actions += `<button class="btn" data-act="review" data-key="${esc(r.key)}" ${local.busy ? 'disabled' : ''}>${ICON.review}Review with Claude</button>`;
  if (local.sent) actions += `<span class="done">Claude is on it${esc(local.where)}</span>`;
  else if (!local.merged && !local.fixing) actions += `<button class="btn" data-act="send" data-key="${esc(r.key)}" ${local.busy ? 'disabled' : ''}>${ICON.claude}${r.lane === 'ready' ? 'Ask Claude first' : 'Send to Claude'}</button>`;
  const shots = state.screens[r.key];
  if (shots && shots.ok && shots.assets.length) {
    const changed = (shots.summary.changed || 0) + (shots.summary.added || 0);
    actions += `<button class="btn" data-act="screens" data-key="${esc(r.key)}">${ICON.camera}Screenshots${changed ? ` (${changed} changed)` : ''}</button>`;
  }
  actions += `<button class="btn" data-act="toggle" data-key="${esc(r.key)}" aria-expanded="${open}" aria-label="Details for ${esc(r.repo)} #${r.num}">Details<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${open ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'}"></path></svg></button>`;
  if (local.error) actions += `<span class="err" role="alert">${esc(local.error)}</span>`;
  if (local.note) actions += `<span class="err" role="status">${esc(local.note)}</span>`;

  let details = '';
  if (open) {
    const areas = r.areas.map((a) => `<li title="${esc(a.paths.join('\n'))}"><span>${esc(a.name)}</span><b>${a.count}</b></li>`).join('');
    details = `<div class="details">
<div><h3>What it does</h3>${r.warning ? `<p class="warning">${esc(r.warning)}</p>` : ''}<div class="desc">${esc(r.description || 'No description.')}</div></div>
<div><h3>Status</h3>${chips(r).map(([label, [word, tone]]) => `<span class="chip"><span>${label}</span><b class="${tone}">${word}</b></span>`).join('')}
<h3 style="margin-top: 18px">Why this risk</h3>${r.risk.reasons.map((x) => `<p>${esc(x)}</p>`).join('')}</div>
<div><h3>Size</h3><p style="font-family: var(--mono); font-size: 22px"><span class="plus">+${r.plus}</span> <span class="minus">−${r.minus}</span></p>
<p>${plural(r.files, 'file')} · ${plural(r.comments, 'comment')}</p><h3 style="margin-top: 18px">Where it changes</h3><ul class="areas">${areas}</ul>
<p style="margin-top: 14px"><a class="btn" href="${esc(r.url)}" target="_blank" rel="noopener">${ICON.github}Open on GitHub</a></p></div>
</div>`;
  }
  return `<div class="row ${open ? 'open' : ''}">
<div class="grid">
<div data-created="${esc(r.created)}"><div class="age" style="color: ${h.text}">${age.text}</div><div class="bar" aria-hidden="true"><div style="width: ${Math.min(100, Math.round(age.hours / 168 * 100))}%; background: ${h.bar}"></div></div><div class="heat" style="color: ${h.text}">${h.label}</div></div>
<div style="min-width: 0"><div class="slug">${esc(r.repo)} #${r.num}${r.draft ? ' · <b>Draft</b>' : ''}</div><a class="title" href="${esc(r.url)}" target="_blank" rel="noopener" style="color: inherit; text-decoration: none">${esc(r.title)}</a>${r.summary ? `<div class="summary">${esc(r.summary)}</div>` : ''}</div>
<div class="blockers">${blockers}</div>
<div class="risk"><span class="risk-tag risk-${r.risk.level}">${RISK[r.risk.level]}</span><span class="size">${plural(r.files, 'file')} · ${plural(r.plus + r.minus, 'line')}</span></div>
<div class="actions">${actions}</div>
</div>
${details}
</div>`;
}

function render() {
  const now = Date.now();
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset : {};
  const visible = state.filter === 'all' ? state.rows : state.rows.filter((r) => r.lane === state.filter);
  $('filters').innerHTML = FILTERS.map((f) => {
    const n = f.id === 'all' ? state.rows.length : state.rows.filter((r) => r.lane === f.id).length;
    return `<button class="filter" data-filter="${f.id}" aria-pressed="${state.filter === f.id}">${f.name} <span>${n}</span></button>`;
  }).join('');
  $('rows').innerHTML = visible.length ? visible.map((r) => rowHtml(r, now)).join('') : '<div class="empty">Nothing here.</div>';
  const repos = new Set(state.rows.map((r) => r.repo)).size;
  if (state.rows.length) {
    $('sub').textContent = `${state.rows.length} open across ${repos} projects. Click Details to see what is inside.`;
    const o = state.rows[0];
    $('oldest-text').textContent = `${ageOf(o.created, now).text} · ${o.repo} #${o.num}`;
    $('oldest').hidden = false;
  } else {
    // Empty list: say why, so "nothing open" never looks like "still loading" or "broken".
    $('sub').textContent = state.error ? 'Could not load pull requests from GitHub.'
      : state.fetchedAt ? 'No open pull requests.' : 'Loading from GitHub…';
    $('oldest').hidden = true;
  }
  $('error').hidden = !state.error;
  $('error').textContent = state.error ? 'GitHub refresh failed, showing the last list: ' + state.error : '';
  if (state.fetchedAt) $('fetched').textContent = 'Updated ' + new Date(state.fetchedAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  // Re-rendering replaces the buttons; put keyboard focus back where it was.
  if (focused.act || focused.filter) {
    const sel = focused.filter ? `[data-filter="${focused.filter}"]` : `[data-act="${focused.act}"][data-key="${CSS.escape(focused.key || '')}"]`;
    const el = document.querySelector(sel);
    if (el) el.focus();
  }
}

// Ages move every 30 s without rebuilding the table, so nothing jumps under the mouse.
function tick() {
  const now = Date.now();
  document.querySelectorAll('[data-created]').forEach((cell) => {
    const age = ageOf(cell.dataset.created, now), h = heat(age.hours);
    cell.querySelector('.age').textContent = age.text;
    cell.querySelector('.age').style.color = h.text;
    cell.querySelector('.heat').textContent = h.label;
    const bar = cell.querySelector('.bar div');
    bar.style.width = Math.min(100, Math.round(age.hours / 168 * 100)) + '%';
    bar.style.background = h.bar;
  });
  if (state.rows.length) $('oldest-text').textContent = `${ageOf(state.rows[0].created, now).text} · ${state.rows[0].repo} #${state.rows[0].num}`;
}

async function load(force) {
  try {
    const res = await fetch('/api/prs' + (force ? '?force=1' : ''));
    const data = await res.json();
    // A new bootId means the server restarted (new code, new token): reload to match it.
    if (state.bootId && data.bootId && data.bootId !== state.bootId) return location.reload();
    state.bootId = data.bootId;
    state.rows = data.rows; state.error = data.error; state.fetchedAt = data.fetchedAt;
    const live = new Set(state.rows.map((r) => r.key));
    // A merged PR drops out of the list; keep its "Merged" note only while it is still listed.
    for (const k of Object.keys(state.local)) if (!live.has(k)) delete state.local[k];
  } catch (e) {
    state.error = 'The dashboard server is not answering. Is the window still open?';
  }
  render();
  probeScreens();
}

async function post(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Dash-Token': window.DASH.token }, body: JSON.stringify(body) });
  return res.json();
}

function confirmBox(title, body, yes, icon) {
  return new Promise((resolve) => {
    const d = $('confirm');
    $('confirm-title').textContent = title;
    $('confirm-body').textContent = body;
    $('confirm-yes').innerHTML = (icon || ICON.merge) + esc(yes);
    const done = (ok) => { d.close(); resolve(ok); };
    $('confirm-yes').onclick = () => done(true);
    $('confirm-no').onclick = () => done(false);
    d.oncancel = () => resolve(false);
    d.showModal();
    $('confirm-no').focus();
  });
}

async function act(kind, key) {
  const r = state.rows.find((x) => x.key === key);
  if (!r) return;
  if (kind === 'screens') return openGallery(r);
  if (kind === 'toggle') {
    state.open.has(key) ? state.open.delete(key) : state.open.add(key);
    return render();
  }
  if (kind === 'merge') {
    const ok = await confirmBox(`Merge ${r.repo} #${r.num}?`, `"${r.title}" will be merged with ${r.mergeMethod}. This cannot be undone from here.`, `Yes, ${r.mergeMethod} merge`);
    if (!ok) return;
  }
  if (kind === 'fixmerge') {
    const todo = r.blockers.map((b) => b.text).join(', ') || 'nothing listed';
    const careful = r.risk.level === 'high' ? ' This one is high risk, so Claude will ask you before the final merge.' : '';
    const ok = await confirmBox(`Fix and merge ${r.repo} #${r.num}?`,
      `Claude starts a session that works on:${todo}${r.draft ? ', marks the draft ready' : ''}, waits for checks to pass, then merges with ${r.mergeMethod}.${careful}`,
      'Yes, start Claude', ICON.claude);
    if (!ok) return;
  }
  state.local[key] = { busy: true };
  render();
  let result;
  try {
    result = await post('/api/' + kind, { key, headSha: r.headSha });
  } catch (e) {
    result = { ok: false, error: 'The dashboard server is not answering.' };
  }
  if (!result.ok) state.local[key] = { error: result.error };
  else if (result.dryRun) state.local[key] = { note: 'Dry run: ' + (Array.isArray(result.command) ? result.command.join(' ') : result.command) };
  else state.local[key] = { ...({ merge: { merged: true }, fixmerge: { fixing: true }, review: { reviewing: true } }[kind] || { sent: true }),
    where: { background: ' · open it in Claude desktop', cloud: ' · finish the setup in the new terminal tab, then open it in Claude desktop' }[result.target] || '' };
  render();
  if (kind === 'merge' && result.ok && !result.dryRun) setTimeout(() => load(true), 4000);
}

document.addEventListener('click', (e) => {
  const f = e.target.closest('[data-filter]');
  if (f) { state.filter = f.dataset.filter; return render(); }
  const b = e.target.closest('[data-act]');
  if (b) act(b.dataset.act, b.dataset.key);
});
$('refresh').addEventListener('click', () => load(true));
$('dry').hidden = !window.DASH.dryRun;

// Screenery screenshots. Ask once per PR, one at a time, after the list loads; the server
// caches each answer for ten minutes, so a refresh costs nothing.
let probing = false;
async function probeScreens() {
  if (probing) return;
  probing = true;
  try {
    for (const r of state.rows) {
      const known = state.screens[r.key];
      // A miss may be a CI run still uploading, so ask again after a minute, not ten.
      if (known && Date.now() - known.at < (known.ok ? 10 : 1) * 60000) continue;
      try {
        const res = await (await fetch('/api/screens?key=' + encodeURIComponent(r.key))).json();
        state.screens[r.key] = { ...res, at: Date.now() };
      } catch (e) { break; }
      if (hasShots(state.screens[r.key])) render();
    }
  } finally { probing = false; }
}
const hasShots = (s) => s && s.ok && s.assets && s.assets.length > 0;

const gal = { row: null, show: 'changed', before: new Set() };
function openGallery(r) {
  gal.row = r; gal.before = new Set();
  const s = state.screens[r.key];
  gal.show = (s.summary.changed || 0) + (s.summary.added || 0) > 0 ? 'changed' : 'all';
  drawGallery();
  $('gallery').showModal();
  $('gallery-close').focus();
}
function drawGallery() {
  const r = gal.row, s = state.screens[r.key];
  const changed = s.assets.filter((a) => a.status !== 'unchanged');
  const list = gal.show === 'changed' ? changed : s.assets;
  $('gallery-title').textContent = `${r.repo} #${r.num} screenshots`;
  $('gallery-sub').textContent = `${plural(s.summary.total || s.assets.length, 'screenshot')}, ${changed.length} changed or new, compared with the main branch.`
    + (s.incomplete ? ` Showing the first ${s.assets.length}; open Screenery for the rest.` : '');
  $('gallery-open').href = s.url || '#';
  $('gallery-open').hidden = !s.url;
  $('gallery-filters').innerHTML =
    `<button class="filter" data-gal="changed" aria-pressed="${gal.show === 'changed'}">Changed <span>${changed.length}</span></button>` +
    `<button class="filter" data-gal="all" aria-pressed="${gal.show === 'all'}">All <span>${s.assets.length}</span></button>`;
  $('gallery-grid').innerHTML = list.length ? list.map((a, i) => {
    const showBefore = gal.before.has(a.name) && a.from;
    const img = showBefore ? a.from : (a.to || a.from);
    const word = { changed: 'Changed', added: 'New', removed: 'Removed', unchanged: 'Same' }[a.status] || a.status;
    return `<figure class="shot ${esc(a.status)}" style="margin: 0">
<div class="shot-head"><div><div class="shot-name">${esc(a.label)}</div><div class="shot-group">${esc(a.group)}${a.flaky ? ' · flaky' : ''}</div></div><span class="tag ${esc(a.status)}">${word}</span></div>
<a class="shot-img" href="${esc(img.url)}" target="_blank" rel="noopener" title="Open full size"><img loading="lazy" src="${esc(img.url)}" alt="${esc(a.label)}, ${showBefore ? 'before' : 'this PR'}"></a>
${a.status === 'changed' && a.from ? `<div class="shot-foot"><button class="btn" data-shot="${esc(a.name)}" data-side="new" aria-pressed="${!showBefore}">This PR</button><button class="btn" data-shot="${esc(a.name)}" data-side="before" aria-pressed="${!!showBefore}">Before (main)</button></div>` : ''}
</figure>`;
  }).join('') : '<div class="empty">No changed screenshots. Pick All to see every one.</div>';
}
$('gallery-close').addEventListener('click', () => $('gallery').close());
$('gallery').addEventListener('click', (e) => {
  const g = e.target.closest('[data-gal]');
  if (g) { gal.show = g.dataset.gal; return drawGallery(); }
  const b = e.target.closest('[data-shot]');
  if (b) { b.dataset.side === 'before' ? gal.before.add(b.dataset.shot) : gal.before.delete(b.dataset.shot); drawGallery(); }
});

// Shipping numbers from the weekly Night Forge report. The file changes once a week, so
// reading it every half hour is plenty.
async function loadStats() {
  let s;
  try { s = await (await fetch('/api/stats')).json(); } catch (e) { return; }
  if (!s.ok) return;
  const n = (x) => Number(x).toLocaleString();
  const asOf = new Date(s.generatedAt).toLocaleDateString([], { month: 'short', day: 'numeric' });
  $('stats').innerHTML = [
    `<span>Merged this week <strong>${n(s.week)}</strong></span>`,
    `<span>week before <strong>${n(s.prevWeek)}</strong></span>`,
    `<span><strong>${s.perDay.toFixed(1)}</strong> a day</span>`,
    `<span>median <strong>${s.medianMergeHours} h</strong> to merge</span>`,
    `<span><strong>${n(s.allTime)}</strong> all time</span>`,
    `<a href="${esc(s.url)}" target="_blank" rel="noopener">report, ${esc(asOf)}</a>`
  ].join('');
  $('stats').hidden = false;
}

load(false);
loadStats();
setInterval(() => load(false), REFRESH_MS);
setInterval(loadStats, 30 * 60000);
setInterval(tick, TICK_MS);
