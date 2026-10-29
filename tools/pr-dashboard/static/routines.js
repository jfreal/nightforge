// Routines page. Rows come from /api/routines, worst first; a row's run history from /api/runs.
'use strict';

const FILTERS = [
  { id: 'all', name: 'All' },
  { id: 'you', name: 'Needs you' },
  { id: 'broken', name: 'Broken or late' },
  { id: 'healthy', name: 'Healthy' },
  { id: 'paused', name: 'Paused' }
];
const STATUS = { ok: 'OK', found: 'Found something', 'needs-you': 'Needs you', failed: 'Failed' };
const REFRESH_MS = 60000;
const TICK_MS = 30000;

const state = { rows: [], filter: 'all', open: new Set(), runs: {}, error: '', collectors: [], collectedAt: null, db: '' };

const ICON = {
  ok: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 5 5 9-10"></path></svg>',
  found: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"></path><path d="m7 10 5 5 5-5"></path><path d="M5 21h14"></path></svg>',
  'needs-you': '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"></circle><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"></path></svg>',
  failed: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2 20h20L12 3z"></path><path d="M12 10v4"></path><path d="M12 17h.01"></path></svg>',
  link: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#64748B" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6"></path><path d="M20 4 10 14"></path><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"></path></svg>'
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const $ = (id) => document.getElementById(id);
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
// Links come from bots; the server only stores http(s) ones, and this checks again.
const safeUrl = (u) => /^https?:\/\//i.test(String(u || '')) ? u : '#';

function span(mins) {
  mins = Math.max(0, Math.round(mins));
  const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
  return d > 0 ? d + 'd ' + h + 'h' : h > 0 ? h + 'h ' + String(m).padStart(2, '0') + 'm' : m + 'm';
}
const minsSince = (t, now) => (now - Date.parse(t)) / 60000;
const ago = (t, now) => minsSince(t, now) < 1 ? 'just now' : span(minsSince(t, now)) + ' ago';
function clock(t) {
  const d = new Date(t), today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString([], today ? { hour: 'numeric', minute: '2-digit' } : { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}
// A cadence or grace, as short as it reads: 15m, 6h, 1d, 1d 12h.
function dur(mins) {
  const parts = [[Math.floor(mins / 1440), 'd'], [Math.floor((mins % 1440) / 60), 'h'], [Math.round(mins % 60), 'm']];
  return parts.filter(([n]) => n).map(([n, u]) => n + u).join(' ') || '0m';
}
const cadence = (every) => every ? 'every ' + dur(every) : '';

// The Last ran cell: how long ago, a bar that fills as the next report comes due, and a word.
function timingOf(r, now) {
  const h = r.health, last = r.last;
  const ago = last ? span(minsSince(last.finished, now)) : '';
  const fill = last && h.every ? Math.min(100, Math.round(minsSince(last.finished, now) / h.every * 100)) : 0;
  const words = {
    ontime: { label: h.dueAt ? 'On time · due ' + clock(h.dueAt) : 'On time', bar: '#CBD2DC', text: '#3B4654' },
    due: { label: 'Due now', bar: '#FFD166', text: '#1A1A1A' },
    late: { label: 'Late by ' + span(minsSince(h.lateAt, now)), bar: '#EA580C', text: '#1A1A1A' },
    never: { label: 'Never reported', bar: '#EA580C', text: '#1A1A1A' },
    waiting: { label: h.dueAt ? 'First report due ' + clock(h.dueAt) : 'No report yet', bar: '#CBD2DC', text: '#3B4654' },
    unscheduled: { label: 'No cadence set', bar: '#CBD2DC', text: '#3B4654' },
    paused: { label: 'Paused', bar: '#CBD2DC', text: '#3B4654' }
  }[h.timing];
  return { ago, fill: h.timing === 'late' || h.timing === 'never' ? 100 : fill, ...words };
}

function countsText(counts) {
  const parts = Object.entries(counts || {}).filter(([, v]) => v).map(([k, v]) => `${v} ${k}`);
  return parts.join(' · ');
}

function statusPill(status) {
  return status ? `<span class="st st-${esc(status)}">${ICON[status] || ''}${esc(STATUS[status] || status)}</span>`
    : '<span class="st st-none">No report yet</span>';
}

function rowHtml(r, now) {
  const t = timingOf(r, now), open = state.open.has(r.id), last = r.last;
  const kind = [r.kind, r.schedule || cadence(r.health.every) + (r.health.everyGuessed ? ' (guessed)' : '')].filter(Boolean).join(' · ');
  const strip = r.strip.map((s) => `<span class="sq sq-${esc(s.status)}" title="${esc(clock(s.finished) + ' · ' + (STATUS[s.status] || s.status) + (s.summary ? ' · ' + s.summary : ''))}"></span>`).join('');
  const week = countsText(r.counts7d);
  const firstLink = last && last.links.length ? last.links[0] : null;
  let actions = `<button class="btn" data-act="toggle" data-id="${esc(r.id)}" aria-expanded="${open}" aria-label="Details for ${esc(r.name)}">Details<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${open ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'}"></path></svg></button>`;
  if (firstLink) actions += `<a class="btn" href="${esc(safeUrl(firstLink.url))}" target="_blank" rel="noopener" title="${esc(firstLink.label)}">${ICON.link}Last run</a>`;
  else if (r.url) actions += `<a class="btn" href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener">${ICON.link}Manage</a>`;

  return `<div class="row ${open ? 'open' : ''}">
<div class="grid rgrid">
<div data-timing="${esc(r.id)}"><div class="age" style="color: ${t.text}">${last ? esc(t.ago) + ' <span style="font-size: 18px">ago</span>' : '—'}</div><div class="bar" aria-hidden="true"><div style="width: ${t.fill}%; background: ${t.bar}"></div></div><div class="when" style="color: ${t.text}">${esc(t.label)}</div></div>
<div style="min-width: 0"><div class="kindline">${esc(kind || r.id)}</div><div class="name">${esc(r.name)}</div>${r.description ? `<div class="summary">${esc(r.description)}</div>` : ''}</div>
<div class="result">${statusPill(last && last.status)}${last && last.summary ? `<span class="said">${esc(last.summary)}</span>` : ''}${last && countsText(last.counts) ? `<span class="counts">${esc(countsText(last.counts))}</span>` : ''}</div>
<div class="record"><span class="record-n"><strong>${r.runs7d}</strong> ${r.runs7d === 1 ? 'run' : 'runs'} this week · ${r.runsTotal} total</span>${strip ? `<span class="strip" aria-label="Last ${r.strip.length} runs">${strip}</span>` : ''}${week ? `<span class="counts">${esc(week)} this week</span>` : ''}</div>
<div class="actions">${actions}</div>
</div>
${open ? detailsHtml(r) : ''}
</div>`;
}

function detailsHtml(r) {
  const h = r.health, runs = state.runs[r.id];
  const facts = [
    ['Id', `<span style="font-family: var(--mono)">${esc(r.id)}</span>`],
    ['Kind', esc(r.kind || 'not set')],
    ['Schedule', esc(r.schedule || 'not set')],
    ['Should report', h.every ? esc(cadence(h.every)) + (h.everyGuessed ? ' <i>(guessed from its last runs)</i>' : '') : 'not set'],
    ['Late after', h.grace ? esc(dur(h.grace)) + ' past due' : '—'],
    ['Next due', h.dueAt ? esc(clock(h.dueAt)) : '—'],
    r.nextAt ? ['Next scheduled', esc(clock(r.nextAt))] : null,
    ['First seen', esc(clock(r.createdAt))]
  ].filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  const manage = r.url ? `<p style="margin-top: 14px"><a class="btn" href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener">${ICON.link}Manage it</a></p>` : '';
  const every = h.every ? dur(h.every).replace(/ /g, '') : '1d';
  const setCmd = cloudReady
    ? `curl -fsS -X PUT ${location.origin}/jobs/${r.id} -H "Authorization: Bearer $NF_ADMIN_TOKEN" -d '{"every": "${every}", "schedule": "…", "does": "…"}'`
    : `python botlog.py bot ${r.id} --every ${every} --schedule "…" --desc "…"`;
  let list;
  if (!runs || runs.loading) list = '<p>Loading runs…</p>';
  else if (runs.error) list = `<p class="err">${esc(runs.error)}</p>`;
  else if (!runs.runs.length) list = '<p>No runs recorded yet.</p>';
  else list = '<ul class="runs">' + runs.runs.map((x) => `<li>
<time datetime="${esc(x.finished)}">${esc(clock(x.finished))}${x.started ? `<br>${esc(span((Date.parse(x.finished) - Date.parse(x.started)) / 60000))} long` : ''}</time>
<span>${statusPill(x.status)}</span>
<div>${esc(x.summary || '(no summary)')}${countsText(x.counts) ? `<div class="counts">${esc(countsText(x.counts))}</div>` : ''}${x.links.length ? `<div class="links">${x.links.map((l) => `<a href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">${esc(l.label)}</a>`).join('')}</div>` : ''}</div>
${x.details ? `<details><summary>Notes</summary><pre>${esc(x.details)}</pre></details>` : ''}
</li>`).join('') + '</ul>';
  return `<div class="details rdetails">
<div><h3>What it should do</h3>${r.description ? `<p>${esc(r.description)}</p>` : ''}<dl class="facts">${facts}</dl>${manage}
<h3 style="margin-top: 18px">Change it</h3><div class="cmd">${esc(setCmd)}</div></div>
<div><h3>Runs, newest first</h3>${list}</div>
</div>`;
}

function render() {
  const now = Date.now();
  const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset : {};
  const visible = state.filter === 'all' ? state.rows : state.rows.filter((r) => r.health.lane === state.filter);
  const n = (lane) => state.rows.filter((r) => r.health.lane === lane).length;
  $('filters').innerHTML = FILTERS.map((f) =>
    `<button class="filter" data-filter="${f.id}" aria-pressed="${state.filter === f.id}">${f.name} <span>${f.id === 'all' ? state.rows.length : n(f.id)}</span></button>`).join('');
  $('rows').innerHTML = visible.length ? visible.map((r) => rowHtml(r, now)).join('')
    : `<div class="empty">${state.rows.length ? 'Nothing here.' : 'No routines yet.'}</div>`;

  if (state.rows.length) {
    const parts = [plural(state.rows.length, 'routine')];
    if (n('you')) parts.push(`${n('you')} need${n('you') === 1 ? 's' : ''} you`);
    if (n('broken')) parts.push(`${n('broken')} broken or late`);
    parts.push(`${n('healthy')} healthy`);
    $('sub').textContent = parts.join(' · ') + '.';
  } else {
    $('sub').textContent = state.error ? 'Could not read the routines database.' : 'Nothing has reported yet.';
  }
  const runsToday = state.rows.reduce((a, r) => a + r.strip.filter((s) => minsSince(s.finished, now) < 1440).length, 0);
  const week = state.rows.reduce((a, r) => a + r.runs7d, 0);
  $('stats').innerHTML = state.rows.length ? `<span><strong>${runsToday}</strong> runs in 24 h</span><span><strong>${week}</strong> this week</span>` : '';
  $('stats').hidden = !state.rows.length;
  const newest = state.rows.filter((r) => r.last).sort((a, b) => Date.parse(b.last.finished) - Date.parse(a.last.finished))[0];
  $('latest').hidden = !newest;
  if (newest) $('latest-text').textContent = `${ago(newest.last.finished, now)} · ${newest.name}`;

  $('error').hidden = !state.error;
  $('error').textContent = state.error;
  const bad = state.collectors.filter((c) => c.error || c.problems.length);
  $('sources').hidden = !bad.length;
  $('sources').innerHTML = bad.length ? 'Some sources need attention:<ul>' + bad.map((c) =>
    `<li><b>${esc(c.source)}</b>: ${esc(c.error || c.problems.slice(0, 3).join('; '))}</li>`).join('') + '</ul>' : '';
  $('howto').hidden = !!state.rows.length || !!state.error;
  if (!cloudReady) $('howto-cmd').textContent = 'python botlog.py report my-bot ok "Checked 12 sites, nothing new"' + (state.db ? `\n\n(writes to ${state.db})` : '');
  $('fetched').textContent = state.collectedAt ? (cloudReady ? 'Updated ' : 'Collected ') + clock(state.collectedAt) : '';

  if (focused.act || focused.filter) {
    const el = document.querySelector(focused.filter ? `[data-filter="${focused.filter}"]` : `[data-act="${focused.act}"][data-id="${CSS.escape(focused.id || '')}"]`);
    if (el) el.focus();
  }
}

// The Last ran cells move every 30 s without rebuilding the table, so nothing jumps under the mouse.
function tick() {
  const now = Date.now();
  document.querySelectorAll('[data-timing]').forEach((cell) => {
    const r = state.rows.find((x) => x.id === cell.dataset.timing);
    if (!r || !r.last) return;
    const t = timingOf(r, now);
    cell.querySelector('.age').innerHTML = esc(t.ago) + ' <span style="font-size: 18px">ago</span>';
    const bar = cell.querySelector('.bar div');
    bar.style.width = t.fill + '%';
  });
}

// The same page runs on the hosted site (tools/job-reports), which answers with mode "cloud". There
// the jobs report straight to the site, so the wording and the nav change a little.
let cloudReady = false;
function applyCloud(data) {
  if (cloudReady || data.mode !== 'cloud') return;
  cloudReady = true;
  const prs = document.querySelector('nav a[href="/"]');
  if (prs) {
    if (data.prDashboardUrl && /^https?:\/\//i.test(data.prDashboardUrl)) prs.href = data.prDashboardUrl;
    else prs.remove();
  }
  const btn = $('refresh');
  btn.lastChild.textContent = 'Refresh';
  btn.insertAdjacentHTML('afterend', '<form method="post" action="/logout" style="margin: 0"><button class="btn" type="submit">Sign out</button></form>');
  $('howto').innerHTML = `<h2>No jobs yet</h2>
<p>A job shows up here the first time it reports. As the last step of any job, wherever it runs:</p>
<pre>${esc(`curl -fsS -X POST ${location.origin}/report -H "Authorization: Bearer $NF_REPORT_TOKEN" \\\n  -d '{"job": "my-job", "status": "ok", "summary": "Checked 12 sites, nothing new"}'`)}</pre>
<p>To say how often it should report, so this page can tell you when it goes quiet:</p>
<pre>${esc(`curl -fsS -X PUT ${location.origin}/jobs/my-job -H "Authorization: Bearer $NF_ADMIN_TOKEN" \\\n  -d '{"name": "My job", "every": "1d", "runsOn": "github action"}'`)}</pre>
<p>tools/job-reports/README.md has the step for GitHub Actions, cloud routines and desktop tasks.</p>`;
}

async function load(force) {
  try {
    const res = await fetch('/api/routines' + (force ? '?force=1' : ''));
    // The hosted site signs you out after 30 days; send the browser to its sign-in page.
    if (res.status === 401) return location.assign('/login');
    const data = await res.json();
    applyCloud(data);
    if (state.bootId && data.bootId && data.bootId !== state.bootId) return location.reload();
    state.bootId = data.bootId;
    state.rows = data.rows; state.error = data.error; state.collectors = data.collectors || [];
    state.collectedAt = data.collectedAt; state.db = data.db;
    // A run that arrived since a row was opened should show up in its history too.
    for (const id of state.open) loadRuns(id);
  } catch (e) {
    state.error = 'The dashboard server is not answering. Is it still running?';
  }
  render();
}

async function loadRuns(id) {
  state.runs[id] = state.runs[id] && !state.runs[id].error ? state.runs[id] : { loading: true };
  try {
    const data = await (await fetch('/api/runs?bot=' + encodeURIComponent(id))).json();
    state.runs[id] = data.ok ? { runs: data.runs } : { error: data.error };
  } catch (e) {
    state.runs[id] = { error: 'The dashboard server is not answering.' };
  }
  if (state.open.has(id)) render();
}

document.addEventListener('click', (e) => {
  const f = e.target.closest('[data-filter]');
  if (f) { state.filter = f.dataset.filter; return render(); }
  const b = e.target.closest('[data-act="toggle"]');
  if (b) {
    const id = b.dataset.id;
    if (state.open.has(id)) state.open.delete(id);
    else { state.open.add(id); loadRuns(id); }
    render();
  }
});
$('refresh').addEventListener('click', async () => {
  $('refresh').disabled = true;
  await load(true);
  $('refresh').disabled = false;
});

load(false);
setInterval(() => load(false), REFRESH_MS);
setInterval(tick, TICK_MS);
