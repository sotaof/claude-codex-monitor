/* Scheduled tasks share the existing theme, translations, authentication and session detail routes. */
window.Schedules = (() => {
  let data = null, fetched = 0, pending = null, error = null;
  let source = 'all', card = 'fail', sort = 'recent', selected = null, lastRoute = '', scrolledRoute = '';
  const logs = new Map();
  const diagnoses = new Map(), saving = new Set();
  const groups = ['fail', 'run', 'ok', 'off', 'svc'];
  const results = ['ok', 'fail', 'stuck', 'miss', 'run', 'unknown'];
  const sourceName = s => t('schedule.source.' + s);
  const statusName = s => t('schedule.result.' + s);
  const needsHandling = run => ['fail', 'stuck', 'miss'].includes(run.status);
  const isHandled = run => needsHandling(run) && !!run.handled;
  const path = task => '#/schedules/' + task.source + '/' + encodeURIComponent(task.id);
  const sessionPath = run => '#/s/' + encodeURIComponent(run.session_key);
  const time = ts => ts == null ? '—' : fullLabel(ts);
  // Within a day: how long ago; older: the date and time (the same rule on every page)
  const age = ts => ts == null ? '—' : Date.now() / 1000 - ts < 86400 ? t('time.ago', dur(Math.max(0, Date.now() / 1000 - ts))) : md(ts) + ' ' + hm(ts);
  const started = run => run.before_monitoring ? t('schedule.before_monitoring') : time(run.start);
  const elapsed = run => run.duration_unknown || run.start == null || run.status === 'miss' || run.status === 'unknown' ? '—' :
    run.end != null ? dur(Math.max(0, run.end - run.start)) :
    run.status === 'run' ? t('time.running', dur(Date.now() / 1000 - run.start)) : '—';
  const dot = status => status === 'stuck' ? 'wait' : status === 'miss' ? 'fail' : status === 'unknown' ? 'off' : status;
  const badge = task => `<span class="cl c-${task.source}">${sourceName(task.source)}</span>`;
  const note = () => `<div class="history-note">${t('schedule.history_note')}</div>`;
  const raw = task => `<span class="raw" title="${esc(task.raw)}">${esc(tm(task.raw_summary || task.raw))}</span>`;
  const pauseTime = ts => {
    if (ts == null) return t('schedule.until_now');
    const date = new Date(ts * 1000), pad = n => String(n).padStart(2, '0');
    return (date.getFullYear() !== new Date().getFullYear() ? date.getFullYear() + '/' : '') +
      (date.getMonth() + 1) + '/' + date.getDate() + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  };
  const pauseLabel = period => t('schedule.disabled_period', pauseTime(period.start), pauseTime(period.end)) +
    (period.inferred || period.end_inferred ? ' · ' + t('schedule.inferred') : '');
  const timeline = task => [
    ...(task.runs || []).map(run => ({run, at: run.start || 0})),
    ...(task.disabled_periods || []).map(period => ({period, at: period.end == null ? data.now : period.end}))
  ].sort((a, b) => b.at - a.at);

  function route(hash) {
    const match = (hash || '').match(/^#\/schedules(?:\/([^/]+)\/([^?]+))?(?:\?(.*))?$/);
    if (!match) return null;
    try {
      return {name: 'schedules', source: match[1], id: match[2] && decodeURIComponent(match[2]),
        run: new URLSearchParams(match[3] || '').get('run')};
    } catch (e) { return {name: 'schedules'}; }
  }
  function navLink(active) {
    const n = data ? data.tasks.filter(task => task.group === 'fail').length : 0;
    return `<a href="#/schedules" class="${active ? 'on' : ''}"${active ? ' aria-current="page"' : ''}>${t('schedule.title')}${n ? `<span class="badge" title="${t('schedule.badge')}">${n}</span>` : ''}</a>`;
  }
  function nav(active) {
    return `<nav class="nav" aria-label="${t('schedule.navigation')}"><a href="#/" class="${active ? '' : 'on'}"${active ? '' : ' aria-current="page"'}>${t('schedule.sessions')}</a>
      ${navLink(active)}</nav>`;
  }
  async function refresh(force) {
    if (pending) return pending;
    // The source collector still runs every 45s; the cheap snapshot overlay follows session messages.
    if (!force && data && data.ready && Date.now() - fetched < 3000) return;
    pending = getJson('/api/schedules').then(value => {
      data = value; fetched = Date.now(); error = null;
      // Logs from a live run can grow. Closed runs are immutable within this snapshot.
      for (const task of data.tasks || []) for (const run of task.runs || [])
        if (run.status === 'run') logs.delete(task.id + ':' + run.id);
    }).catch(e => { error = e.userMessage || e.message; }).finally(() => { pending = null; });
    return pending;
  }
  function next(task) {
    if (!task.enabled) return '—';
    return esc(time(task.next_run));
  }
  function strip(task) {
    const entries = timeline(task).slice(0, 14).reverse();
    return `<span class="strip">${'<i class="block b-empty" aria-hidden="true"></i>'.repeat(14 - entries.length)}${entries.map(({run, period}) => {
      if (period) return `<i class="block b-paused" role="img" aria-label="${esc(pauseLabel(period))}" title="${esc(pauseLabel(period))}"></i>`;
      const label = started(run) + ' · ' + statusName(run.status) + (isHandled(run) ? ' · ' + t('schedule.handled') : '');
      return `<a class="block b-${run.status}${isHandled(run) ? ' handled' : ''}" href="${path(task)}?run=${encodeURIComponent(run.id)}" aria-label="${esc(label)}" title="${esc(label)}">${isHandled(run) ? '✓' : ''}</a>`;
    }).join('')}</span>`;
  }
  function row(task) {
    const last = task.runs[0];
    const lastHtml = !last ? `<span class="faint">${task.group === 'off' ? '—' : t('schedule.never')}</span>` :
      `<span class="st s-${last.status}"><span class="dot d-${dot(last.status)}"></span>${statusName(last.status)}${isHandled(last) ? ' · ' + t('schedule.handled') : ''} · ${last.before_monitoring ? started(last) : age(last.start)}</span>
       <span class="why" title="${esc(tm(last.summary))}">${esc(last.status === 'ok' ? elapsed(last) : tm(last.summary))}</span>`;
    return `<div class="srow r-${task.group}" data-schedule="${esc(path(task))}" tabindex="0" role="link">
      ${badge(task)}<span class="name"><span class="title" title="${esc(task.title)}">${esc(task.title)}</span>${task.source !== 'launchd' ? `<span class="id">${esc(task.id)}</span>` : ''}${!last && task.group === 'ok' ? `<span class="chip">${t('schedule.unrun')}</span>` : ''}${!task.enabled ? `<span class="raw">${esc(tm(task.off_reason))}</span>` : ''}</span>
      <span class="sched">${esc(tm(task.schedule))}${raw(task)}</span>
      <span class="last">${lastHtml}</span>
      <span class="next">${next(task)}</span>${strip(task)}</div>`;
  }
  function serviceList(services) {
    if (!services.length) return `<div class="empty">${t('schedule.empty_services')}</div>`;
    const columns = ['source', 'name', 'state', 'pid', 'uptime', 'restarts'];
    return `<div class="scols svc-cols">${columns.map(c => `<span>${t('schedule.' + c)}</span>`).join('')}</div>` +
      services.map(service => {
        const running = service.loaded && service.pid && service.enabled;
        const label = !service.enabled ? t('schedule.off') : !service.loaded ? t('schedule.unloaded') :
          running ? statusName('run') : t('schedule.stopped');
        return `<div class="srow svc-row">${badge(service)}<span class="name mono">${esc(service.title)}</span>
          <span class="st s-${running ? 'ok' : 'unknown'}"><span class="dot d-${running ? 'ok' : 'off'}"></span>${label}</span>
          <span class="mono">${service.pid ? esc(service.pid) : '—'}</span>
          <span title="${t('schedule.uptime_observed')}">${service.uptime == null ? '—' : dur(service.uptime)}</span>
          <span title="${t('schedule.observed_restarts')}">${t('schedule.restart_count', service.restarts_24h || 0)}${service.history_limited ? '+' : ''}</span></div>`;
      }).join('') + note();
  }
  function list() {
    const q = (query || '').toLowerCase();
    const found = task => !q || (task.title + ' ' + task.id + ' ' + (task.raw || '')).toLowerCase().includes(q);
    const tasks = data.tasks.filter(task => (source === 'all' || task.source === source) && found(task));
    const services = source === 'all' || source === 'launchd' ? data.services : [];
    const count = g => g === 'svc' ? services.length : tasks.filter(task => task.group === g).length;
    const ctx = {
      fail: tasks.filter(task => task.group === 'fail').slice(0, 2).map(task => task.title).join(t('common.list_separator')) || t('schedule.none'),
      run: tasks.filter(task => task.group === 'run').slice(0, 2).map(task => task.title).join(t('common.list_separator')) || t('schedule.none'),
      ok: t('schedule.today_count', tasks.filter(task => task.group === 'ok' && task.next_run &&
        new Date(task.next_run * 1000).toDateString() === new Date().toDateString()).length),
      off: t('schedule.off_context'), svc: t('schedule.service_context')
    };
    // The same toolbar as the session list: search on the left; source, sort and when it was updated on the right
    let html = `<div class="tools"><label class="search">${ICON.search}<input id="search" placeholder="${t('schedule.search')}" aria-label="${t('schedule.search')}" value="${esc(query)}"><kbd>/</kbd></label><span class="sp"></span>
      <div class="sg" aria-label="${t('schedule.source')}">${['all', 'launchd', 'claude', 'codex'].map(s =>
      `<button data-schedule-source="${s}" class="${source === s ? 'on' : ''}" aria-pressed="${source === s}">${s === 'all' ? t('common.all') : sourceName(s)} <span class="faint">${s === 'all' ? data.tasks.length : data.tasks.filter(task => task.source === s).length}</span></button>`).join('')}</div>
      ${sortSeg('data-schedule-sort', sort, ['recent', 'next'].map(s => [s, t('schedule.sort_' + s)]))}<span title="${esc(t('schedule.refresh', 45))}">${updatedHtml(data.now)}</span></div>
      <div class="stcards">${groups.map(g => `<button class="scard k-${g} ${card === g ? 'on' : ''}" data-schedule-card="${g}" aria-pressed="${card === g}">
        <span class="lb"><span class="dot d-${g === 'svc' ? 'off' : g}"></span>${t('schedule.group.' + g)}</span>
        <span class="n">${count(g)}</span><span class="ctx" title="${esc(ctx[g])}">${esc(ctx[g])}</span></button>`).join('')}</div>`;
    if (card === 'svc') return html + serviceList(services);
    const shown = tasks.filter(task => task.group === card);
    shown.sort((a, b) => sort === 'next' ? (a.next_run == null ? Infinity : a.next_run) - (b.next_run == null ? Infinity : b.next_run) :
      ((b.runs[0] || {}).start || b.last_run || 0) - ((a.runs[0] || {}).start || a.last_run || 0));
    html += `<div class="listhead"><span>${t('schedule.group.' + card)} · ${t('schedule.count', shown.length)} · ${t('schedule.sort_note_' + sort)}</span><span class="faint">${t('schedule.lead.' + card)}</span></div>`;
    if (!shown.length) return html + `<div class="empty">${t('schedule.empty.' + card)}</div>`;
    html += `<div class="scols">${['source', 'name', 'schedule', 'last', 'next', 'history14'].map(c => `<span>${t('schedule.' + c)}</span>`).join('')}</div>` +
      shown.map(row).join('') + `<div class="legend">${results.map(r => `<span><i class="b-${r}"></i>${statusName(r)}</span>`).join('')}<span><i class="b-stuck handled">✓</i>${t('schedule.handled')}</span></div>`;
    if (shown.some(task => task.source === 'launchd')) html += note();
    return html;
  }
  function runAction(task, run) {
    let action = run.status === 'miss' ? '' : task.source === 'launchd' ?
      `<button class="logbtn" data-schedule-run="${esc(run.id)}">${t(selected === run.id ? 'schedule.hide_log' : 'schedule.show_log')}</button>` :
      run.session_key ? `<a href="${sessionPath(run)}">${t('schedule.open_session')}</a>` : `<span class="faint">${t('schedule.no_session')}</span>`;
    if (needsHandling(run))
      action += `<button class="logbtn" data-schedule-handle="${esc(run.id)}"${saving.size ? ' disabled' : ''}>${t(isHandled(run) ? 'schedule.undo_handled' : 'schedule.ignore')}</button>`;
    return action;
  }
  const diagnosisKey = (task, run) => task.source + ':' + task.id + ':' + run.id;
  function diagnosis(task, run) {
    const value = diagnoses.get(diagnosisKey(task, run));
    if (!value || value.loading) return `<div class="faint">${t('loading.detail')}</div>`;
    const fields = ['asleep', ...(task.source === 'launchd' ? ['launchd_loaded'] :
      task.source === 'claude' ? ['desktop_running', 'dispatched', 'delayed', 'modified', 'disabled'] : [])];
    return `<dl class="evidence">${fields.map(field => {
      const state = value[field] === true ? 'yes' : value[field] === false ? 'no' : 'unknown';
      return `<dt>${t('schedule.evidence.' + field)}</dt><dd>${t('schedule.evidence.' + state)}${field === 'disabled' && value.state_inferred ? ' · ' + t('schedule.inferred') : ''}</dd>`;
    }).join('')}</dl>${value.last_change_at ? `<p>${t('schedule.last_change', time(value.last_change_at), t('schedule.change.' + value.last_change_kind))}</p>` : ''}
      ${value.error ? `<div>${esc(tm(value.error))}</div>` : ''}`;
  }
  function guidance(task, run) {
    let body = '';
    if (run.status === 'miss') {
      body = diagnosis(task, run) + `<p>${t('schedule.advice.miss.' + task.source)}</p>`;
      if (task.state_history_unavailable || run.state_unconfirmed)
        body += `<p>${t('schedule.advice.state_unknown')}</p>`;
      if (task.consecutive_misses >= 2) body += `<p>${t('schedule.advice.consecutive', task.consecutive_misses)}</p>`;
    } else if (run.status === 'stuck') {
      const items = run.items || [];
      body = items.length ? `<ol>${items.slice(0, 3).map(i => `<li>${esc(tm(i.text))}</li>`).join('')}</ol>` :
        `<p>${t('schedule.advice.wait_unknown')}</p>`;
    } else {
      const text = tm(run.summary);
      const kind = /ENOTFOUND|Unable to connect/i.test(text) ? 'network' :
        /\b403\b|authenticat|unauthorized|认证失败/i.test(text) ? 'auth' :
        task.source === 'launchd' ? 'launchd' : null;
      if (kind) body = `<p>${t('schedule.advice.' + kind)}</p>`;
    }
    return body ? `<h3>${t('schedule.next_step')}</h3>${body}` : '';
  }
  function expanded(task, run) {
    if (task.source !== 'launchd') return `<pre>${esc(tm(run.summary))}</pre>`;
    const log = logs.get(task.id + ':' + run.id);
    if (!log || log.loading) return `<div class="faint">${t('loading.detail')}</div>`;
    return `<div class="faint">${t('schedule.log_excerpt')}</div>${log.error ? `<div class="s-fail">${esc(tm(log.error))}</div>` : ''}
      ${log.unavailable ? `<div class="faint">${t('schedule.log_unavailable')}</div>` : ''}
      <pre>${esc((log.lines || []).map(line => '[' + line.stream + '] ' + line.text).join('\n') || t('schedule.log_empty'))}</pre>`;
  }
  function detail(task) {
    const runs = task.runs || [], last = runs[0], done = runs.filter(run => ['ok', 'fail', 'stuck', 'miss'].includes(run.status));
    const ok = done.filter(run => run.status === 'ok').length;
    const bad = done.filter(run => ['fail', 'stuck'].includes(run.status)).length;
    const miss = done.filter(run => run.status === 'miss').length;
    let alert = '';
    const focus = runs.find(run => run.id === selected) || last;
    if (focus && needsHandling(focus)) {
      alert = `<div class="alert ${focus.status === 'stuck' ? 'stuck' : 'fail'}"><b>${statusName(focus.status)} · ${started(focus)}${isHandled(focus) ? ' · ' + t('schedule.handled') : ''}</b>
        <span class="text">${esc(tm(focus.summary))}</span>${guidance(task, focus)}
        <div class="actions">${runAction(task, {...focus, session_key: null}).replace(`<span class="faint">${t('schedule.no_session')}</span>`, '')}
        ${focus.status === 'stuck' && focus.session_key ? `<a href="${sessionPath(focus)}">${t('schedule.reply')}</a>` : ''}</div></div>`;
    }
    const previous = runs.slice(1).filter(run => needsHandling(run) && !isHandled(run));
    const bulk = previous.length ? `<button class="btn" data-schedule-previous="${esc(task.id)}"${saving.size ? ' disabled' : ''}>${t('schedule.handle_previous')}</button>` : '';
    const kv = (label, value, mono = false) => `<dt>${t('schedule.' + label)}</dt><dd${mono ? ' class="mono"' : ''}>${value || '—'}</dd>`;
    let html = `<div class="dhead"><a class="back" href="#/schedules">← ${t('schedule.title')}</a>${badge(task)}<h2>${esc(task.title)}</h2>
      ${!task.enabled ? `<span class="chip">${esc(tm(task.off_reason))}</span>` : ''}</div>
      <div class="sec">${alert}${bulk ? `<div class="actions">${bulk}</div>` : ''}<h3>${t('schedule.config')}</h3><dl class="kv">` +
      kv('type', t('schedule.type.' + task.source) + (task.kind === 'heartbeat' ? ' · ' + t('schedule.heartbeat') : '')) +
      kv('schedule', esc(tm(task.schedule)) + raw(task)) +
      kv('next', next(task)) + kv('id', esc(task.id), true) +
      kv(task.source === 'launchd' ? 'command' : 'description', esc(task.description), task.source === 'launchd') +
      kv('config_path', esc(task.config_path), true) +
      (task.skill_path ? kv('skill_path', esc(task.skill_path), true) : '') +
      (task.log_paths ? kv('log_paths', task.log_paths.map(p => esc(p)).join('<br>'), true) : '') +
      `</dl>${task.history_fallback ? `<div class="history-note">${t('schedule.heartbeat_fallback')}</div>` : ''}${task.next_estimated ? `<div class="history-note">${t('schedule.next_estimated')}</div>` : ''}</div>
      <div class="sec"><h3>${t('schedule.runs')}</h3>`;
    const entries = timeline(task);
    if (entries.length) {
      html += `<div class="rate"><span><span class="big">${done.length ? Math.round(ok / done.length * 100) + '%' : '—'}</span>${t('schedule.rate', done.length)}</span>
        <span><span class="big s-ok">${ok}</span>${statusName('ok')}</span><span><span class="big s-fail">${bad}</span>${t('schedule.bad_count')}</span>
        <span><span class="big s-fail">${miss}</span>${statusName('miss')}</span></div>
        <table class="runs"><thead><tr><th class="started">${t('schedule.started')}</th><th class="result">${t('schedule.result')}</th>
        <th class="hide-m elapsed">${t('schedule.elapsed')}</th><th>${t('schedule.summary')}</th><th class="open">${t('schedule.action')}</th></tr></thead><tbody>` +
        entries.map(({run, period}) => period ? `<tr class="pause-note"><td colspan="5">${esc(pauseLabel(period))}</td></tr>` :
          `<tr class="rr ${selected === run.id ? 'sel' : ''}" data-schedule-run="${esc(run.id)}"><td>${started(run)}</td>
          <td><span class="st s-${run.status}">${statusName(run.status)}</span>${isHandled(run) ? `<span class="handled-label" title="${t('schedule.handled.' + (run.handled_reason || 'marked'))}">✓ ${t('schedule.handled')}</span>` : ''}
          ${run.mode ? `<span class="raw">${t('schedule.mode.' + run.mode)}${run.mode_inferred ? ' · ' + t('schedule.inferred') : ''}</span>` :
            run.state_inferred ? `<span class="raw">${t('schedule.inferred')}</span>` : ''}</td><td class="hide-m">${elapsed(run)}</td>
          <td>${esc(tm(run.summary))}${run.approximate ? `<span class="raw">${t(run.before_monitoring ? 'schedule.end_estimated' : 'schedule.time_observed')}</span>` : ''}</td><td class="open">${runAction(task, run)}</td></tr>
          ${selected === run.id && run.status !== 'miss' ? `<tr><td colspan="5">${expanded(task, run)}</td></tr>` : ''}`).join('') + '</tbody></table>';
    } else html += `<div class="empty">${t('schedule.never')}</div>`;
    html += '</div>';
    if (task.source === 'launchd') html += note();
    return html;
  }
  function render(rt) {
    if (lastRoute !== location.hash) { selected = rt.run; lastRoute = location.hash; }
    let html;
    if (error || data && data.error) html = `<div class="empty s-fail">${esc(tm(error || data.error))}</div>`;
    else if (!data || !data.ready) html = `<div class="loading">${t('loading.detail')}</div>`;
    else if (rt.id) {
      const task = data.tasks.find(task => task.source === rt.source && task.id === rt.id);
      html = task ? detail(task) : `<div class="empty"><a href="#/schedules">${t('schedule.title')}</a> · ${t('schedule.task_missing')}</div>`;
      if (task && selected && task.source === 'launchd') loadLog(task, selected);
      if (task) {
        const run = task.runs.find(run => run.id === selected) || task.runs[0];
        if (run && run.status === 'miss') loadDiagnosis(task, run);
      }
    } else html = list();
    return `<div class="schedules">${html}</div>`;
  }
  function afterRender(rt) {
    if (!rt.run) { scrolledRoute = ''; return; }
    if (scrolledRoute === location.hash) return;
    const selectedRow = document.querySelector('.schedules .runs .sel');
    if (selectedRow) {
      scrolledRoute = location.hash;
      selectedRow.scrollIntoView({block: 'center'});
    }
  }
  async function loadLog(task, id) {
    const key = task.id + ':' + id;
    const run = task.runs.find(run => run.id === id);
    if (logs.has(key) || !run || run.status === 'miss') return;
    logs.set(key, {lines: [], loading: true});
    try {
      logs.set(key, await getJson('/api/schedules/launchd/' + encodeURIComponent(task.id) + '/runs/' + encodeURIComponent(id) + '/log'));
    } catch (e) { logs.set(key, {error: e.userMessage || e.message}); }
    if (route(location.hash)) window.render();
  }
  async function loadDiagnosis(task, run) {
    const key = diagnosisKey(task, run), cached = diagnoses.get(key);
    if (cached && (cached.loading || Date.now() - cached.at < 300000)) return;
    diagnoses.set(key, {loading: true, at: Date.now()});
    try {
      const value = await getJson('/api/schedules/' + task.source + '/' + encodeURIComponent(task.id) +
        '/runs/' + encodeURIComponent(run.id) + '/diagnosis');
      diagnoses.set(key, {...value, at: Date.now()});
    } catch (e) { diagnoses.set(key, {error: e.userMessage || e.message, at: Date.now()}); }
    if (route(location.hash)) window.render();
  }
  async function handle(task, run, bulk) {
    if (saving.size || !bulk && !needsHandling(run)) return;
    saving.add(task.id);
    window.render();
    try {
      const url = bulk ? '/api/schedules/previous' : isHandled(run) ? '/api/restore' : '/api/dismiss';
      const body = bulk ? {source: task.source, id: task.id} : {keys: [run.dismiss_key]};
      const response = await getJson(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
      const undo = bulk || !isHandled(run);
      await refresh(true);
      await tick(true);
      toast(() => t(undo ? 'schedule.saved' : 'toast.restored'), undo ? async () => {
        try { await post('/api/restore', response.keys); await refresh(true); await tick(true); }
        catch (e) { toast(e.userMessage || e.message); }
      } : null);
    } catch (e) { toast(e.userMessage || e.message); }
    finally { saving.clear(); window.render(); }
  }
  function click(event) {
    const el = event.target;
    for (const [attribute, update] of [
      ['data-schedule-source', value => { source = value; }],
      ['data-schedule-sort', value => { sort = value; }],
      ['data-schedule-card', value => { card = value; }]
    ]) {
      const button = el.closest('[' + attribute + ']');
      if (button) { update(button.getAttribute(attribute)); window.render(); return true; }
    }
    const handled = el.closest('[data-schedule-handle]'), bulk = el.closest('[data-schedule-previous]');
    if (handled || bulk) {
      const rt = route(location.hash);
      const task = data.tasks.find(t => t.source === rt.source && t.id === rt.id);
      const run = handled && task.runs.find(r => r.id === handled.getAttribute('data-schedule-handle'));
      if (task && (run || bulk)) handle(task, run, !!bulk);
      return true;
    }
    if (el.closest('a')) return false;
    const run = el.closest('[data-schedule-run]');
    if (run) {
      selected = selected === run.dataset.scheduleRun ? null : run.dataset.scheduleRun;
      window.render(); return true;
    }
    const row = el.closest('[data-schedule]');
    if (row) { location.hash = row.dataset.schedule; return true; }
    return false;
  }
  return {route, nav, navLink, refresh, render, afterRender, click};
})();
