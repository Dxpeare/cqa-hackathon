/* CQA front-end: vanilla JS, hash routing, talks to /api/*.
   Everything lives inside #cqa-app so it can be dropped into a Dynamics 365 web resource. */
(function () {
  "use strict";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const view = $("#view");
  const state = { settings: null, checklists: [], conv: null, ev: null, role: "operator", jobTimer: null };

  // ------------------------------------------------------------ helpers
  async function api(path, method, body) {
    const r = await fetch(path, { method: method || "GET", headers: { "Content-Type": "application/json", "X-CQA-Role": state.role },
      body: body === undefined ? undefined : JSON.stringify(body) });
    let data = null;
    try { data = await r.json(); } catch (e) { data = { error: "сервер вернул не JSON" }; }
    if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
    return data;
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function toast(msg, isError) {
    const t = $("#toast"); t.textContent = msg; t.className = "toast" + (isError ? " error" : ""); t.hidden = false;
    clearTimeout(t._h); t._h = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 3000);
  }
  function fmtScore(v) { return v == null ? "—" : Number(v).toFixed(2); }
  function scoreClass(v) { return v == null ? "" : v >= 80 ? "good" : v >= 50 ? "mid" : "bad"; }
  function answerLabel(c, a) {
    if (a == null) return "—";
    if (c && c.critical) return a === "yes" ? "Есть нарушение" : a === "no" ? "Нет нарушений" : "Не применимо";
    if (a === "na") return "Не применимо";
    if (a === "yes") return "Да";
    if (a === "no") return "Нет";
    return a;
  }
  function answerClass(c, a) {
    if (c.critical) return a === "yes" ? "crit-yes" : "yes";
    if (a === "na") return "na";
    if (c.type === "scale") { const top = Math.max(...c.scale.map(s => +s.value)); return +a >= top ? "yes" : +a > 0 ? "" : "no"; }
    return a;
  }
  const GROUP_NAMES = { DEV: "Учебный набор", FBROKER: "Контрольный набор", SWAP: "Дополнительный набор", UPLOADED: "Загруженные вручную" };
  function groupName(g) { return GROUP_NAMES[g] || g; }
  const PRETTY_MODELS = { "gemma4:31b": "Gemma 4 (31B)", "qwen3.6:35b": "Qwen 3.6 (35B)" };
  function prettyModel(m) { return PRETTY_MODELS[m] || m || "—"; }
  function modelName(m) {
    if (!m) return "модель";
    if (m === "mock") return "демо-эвристика (не ИИ)";
    if (m.startsWith("ollama:")) return "локальная модель " + prettyModel(m.slice(7));
    if (m.startsWith("gemini:")) return "Gemini " + m.slice(7);
    return "Claude " + m;
  }
  function roleName(r) { return { operator: "Оператор", client: "Клиент", bot: "Бот", system: "Система" }[r] || r; }
  function modelLabel(s) {
    const ext = s.hybrid_external === "gemini" ? s.gemini_model : s.anthropic_model;
    return { mock: "Демо без ИИ: оценки ненастоящие", ollama: "Оценивает ИИ: " + prettyModel(s.ollama_model) + " · локально", gemini: "Gemini · " + s.gemini_model,
      anthropic: "Claude · " + s.anthropic_model, hybrid: "Гибрид · " + s.ollama_model + " + " + ext }[s.provider] || s.provider;
  }
  function setRole(role) {
    state.role = role;
    document.querySelectorAll(".role-switch button").forEach(b => b.classList.toggle("active", b.dataset.role === role));
  }
  document.querySelectorAll(".role-switch button").forEach(b => b.addEventListener("click", () => {
    setRole(b.dataset.role);
    if (state.ev && $("#right")) renderResult(state.conv, state.ev);
  }));
  setRole(state.role);
  function setStatus() {
    api("/api/settings").then(s => {
      state.settings = s; const p = $("#model-pill");
      p.textContent = modelLabel(s); p.classList.toggle("warn", s.provider === "mock");
    }).catch(() => { $("#model-pill").textContent = "сервер недоступен"; });
    api("/api/appeals").then(list => {
      const n = list.filter(a => a.status === "open").length; const b = $("#appeals-badge");
      b.hidden = !n; b.textContent = n;
    }).catch(() => {});
  }

  // ------------------------------------------------------------ routing
  const routes = { list: renderList, card: renderCard, checklists: renderChecklists, appeals: renderAppeals, settings: renderSettings,
    analytics: renderAnalytics, audit: renderAudit };
  function go(hash) { location.hash = hash; }
  async function render() {
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    const name = routes[parts[0]] ? parts[0] : "list";
    document.querySelectorAll(".cqa-nav button").forEach(b => b.classList.toggle("active", b.dataset.view === (name === "card" ? "list" : name)));
    view.innerHTML = '<div class="empty"><span class="spinner" style="border-color:#999;border-right-color:transparent"></span> Загрузка…</div>';
    try { await routes[name](parts.slice(1).map(decodeURIComponent)); }
    catch (e) { view.innerHTML = '<div class="panel">Ошибка: ' + esc(e.message) + "</div>"; }
    setStatus();
  }
  window.addEventListener("hashchange", render);
  document.querySelectorAll(".cqa-nav button").forEach(b => b.addEventListener("click", () => go("/" + b.dataset.view)));

  // ------------------------------------------------------------ list view
  async function renderList() {
    const [convs, datasets, checklists, jobs] = await Promise.all([api("/api/conversations"), api("/api/datasets"), api("/api/checklists"), api("/api/jobs")]);
    state.checklists = checklists;
    const groups = [...new Set(convs.map(c => c.group_id))];
    const filt = state.filter || { group: "", lang: "", channel: "", attn: "", q: "" };
    const ATTN = { attention: "Требуют внимания", critical: "Грубые ошибки", low: "Балл ниже 70", unsure: "Модель не уверена",
      appeals: "Открытые апелляции", none: "Не оценены" };
    const running = jobs.find(j => j.status === "running");
    const evd = convs.filter(c => c.evaluation);
    const totals = evd.map(c => c.evaluation.total).filter(v => v != null);
    const avg = totals.length ? totals.reduce((a, b) => a + b, 0) / totals.length : null;
    view.innerHTML = `
      <section class="hero">
        <h1><span class="grad">Оценка качества</span> диалогов</h1>
        <div class="kpis">
          <div class="kpi"><b>${convs.length}</b><span>диалогов</span></div>
          <div class="kpi accent"><b>${evd.length}</b><span>оценено</span></div>
          <div class="kpi"><b class="${scoreClass(avg)}">${fmtScore(avg)}</b><span>средний балл</span></div>
          <div class="kpi ${evd.some(c => c.evaluation.critical_error) ? "alert" : ""}"><b>${evd.filter(c => c.evaluation.critical_error).length}</b><span>грубых ошибок</span></div>
          <div class="kpi"><b>${evd.reduce((a, c) => a + c.evaluation.open_appeals, 0)}</b><span>открытых апелляций</span></div>
        </div>
      </section>
      </div>
      <div class="panel" style="margin-bottom:16px">
        <div class="row">
          <select id="f-group" style="width:200px"><option value="">Все наборы</option>${groups.map(g => `<option value="${esc(g)}" ${filt.group === g ? "selected" : ""}>${esc(groupName(g))}</option>`).join("")}</select>
          <select id="f-lang" style="width:110px"><option value="">Язык</option>${["RU", "KZ", "EN"].map(l => `<option ${filt.lang === l ? "selected" : ""}>${l}</option>`).join("")}</select>
          <select id="f-channel" style="width:140px"><option value="">Канал</option><option value="agent chat" ${filt.channel === "agent chat" ? "selected" : ""}>Чат</option><option value="voice call" ${filt.channel === "voice call" ? "selected" : ""}>Звонок</option></select>
          <select id="f-attn" style="width:200px"><option value="">Все оценки</option>${Object.entries(ATTN).map(([k, v]) => `<option value="${k}" ${filt.attn === k ? "selected" : ""}>${v}</option>`).join("")}</select>
          <input id="f-q" type="text" placeholder="Поиск по ID диалога" value="${esc(filt.q)}" style="width:170px">
          <span class="break"></span>
          <span class="grp"><span class="grp-l">Данные</span>
            <button class="btn secondary sm" id="btn-upload" title="Загрузить файл JSON с диалогами (формат API организаторов)">Загрузить диалоги</button>
            <input id="file-upload" type="file" accept=".json,application/json" hidden>
            <button class="btn secondary sm" id="btn-fetch" title="Скачать набор диалогов из API организаторов по его group_id">Скачать набор…</button></span>
          <span class="grp"><span class="grp-l">Оценка</span>
            <select id="batch-checklist" style="width:210px" title="Какой чек-лист применить ко всем диалогам набора"><option value="">Чек-лист: по типу диалога</option>${checklists.map(c => `<option value="${esc(c.id)}">${esc(c.name)} (v${c.version})</option>`).join("")}</select>
            <button class="btn sm" id="btn-batch" ${running ? "disabled" : ""} title="Запустить модель по всем диалогам выбранного набора, у которых ещё нет оценки">Оценить неоценённые</button></span>
          <span class="grp"><span class="grp-l">Результаты</span>
            <button class="btn secondary sm" id="btn-export" title="Сохранить оценки набора в results/…/results.json в формате организаторов">Выгрузить results.json</button></span>
        </div>
        <details class="raw" style="margin-top:8px"><summary>Дополнительно: переоценка и удаление</summary>
          <div class="row" style="margin-top:8px">
            <button class="btn secondary sm" id="btn-reeval" ${running ? "disabled" : ""}>Переоценить все диалоги набора заново</button>
            <button class="btn danger sm" id="btn-clear">Удалить все оценки набора</button>
          </div></details>
        <div id="job" style="margin-top:10px" ${running ? "" : "hidden"}></div>
      </div>
      <div class="panel" style="padding:0"><table class="list"><thead><tr>
        <th>ID</th><th>Набор</th><th>Канал</th><th>Язык</th><th>Реплик</th><th>Чек-лист</th><th>Итог</th><th>Статус</th><th></th>
      </tr></thead><tbody id="rows"></tbody></table></div>`;
    function apply() {
      state.filter = { group: $("#f-group").value, lang: $("#f-lang").value, channel: $("#f-channel").value, attn: $("#f-attn").value, q: $("#f-q").value.trim().toLowerCase() };
      const f = state.filter;
      const attn = c => {
        const e = c.evaluation; if (f.attn === "none") return !e; if (!e) return false;
        const t = { critical: e.critical_error, low: e.total != null && e.total < 70, unsure: e.min_confidence != null && e.min_confidence < 0.6, appeals: e.open_appeals > 0 };
        return f.attn === "attention" ? Object.values(t).some(Boolean) : t[f.attn];
      };
      const rows = convs.filter(c => (!f.group || c.group_id === f.group) && (!f.lang || c.language === f.lang) && (!f.channel || c.channel === f.channel) && (!f.attn || attn(c)) && (!f.q || c.conversation_id.toLowerCase().includes(f.q)));
      $("#rows").innerHTML = rows.map(c => {
        const e = c.evaluation;
        const st = !e ? '<span class="muted small">не оценён</span>' : (e.open_appeals ? `<span class="tag warn">апелляция (${e.open_appeals})</span>` : e.has_changes ? '<span class="tag">пересмотрено</span>' : '<span class="tag ok">оценён</span>');
        return `<tr class="clickable" data-id="${esc(c.conversation_id)}">
          <td><b>${esc(c.conversation_id)}</b></td><td class="small">${esc(groupName(c.group_id))}</td>
          <td>${c.channel === "voice call" ? '<span class="tag voice">звонок</span>' : '<span class="tag">чат</span>'}${c.has_audio ? '<span class="tag voice">аудио</span>' : ""}</td>
          <td><span class="tag lang">${esc(c.language)}</span></td><td>${c.messages}</td>
          <td class="small">${e ? esc(e.checklist) : ""}</td>
          <td class="score ${e ? scoreClass(e.total) : ""}">${e ? fmtScore(e.total) : ""}${e && e.critical_error ? ' <span class="flag" title="грубая ошибка">⚠</span>' : ""}</td>
          <td>${st}</td><td><button class="btn sm ${e ? "secondary" : ""}" data-open="${esc(c.conversation_id)}">${e ? "Открыть" : "Оценить"}</button></td></tr>`;
      }).join("") || '<tr><td colspan="9" class="empty">Нет диалогов. Нажмите «Загрузить диалоги» или «Скачать набор…».</td></tr>';
    }
    apply();
    ["#f-group", "#f-lang", "#f-channel", "#f-attn"].forEach(s => $(s).addEventListener("change", apply));
    $("#f-q").addEventListener("input", apply);
    $("#rows").addEventListener("click", e => {
      const b = e.target.closest("[data-open]"); const tr = e.target.closest("tr[data-id]");
      if (b) go("/card/" + encodeURIComponent(b.dataset.open) + (b.textContent === "Оценить" ? "/evaluate" : ""));
      else if (tr) go("/card/" + encodeURIComponent(tr.dataset.id));
    });
    $("#btn-upload").addEventListener("click", () => $("#file-upload").click());
    $("#file-upload").addEventListener("change", async e => {
      const f = e.target.files[0]; if (!f) return;
      try { const obj = JSON.parse(await f.text()); const r = await api("/api/datasets/upload", "POST", obj); toast("Загружено диалогов: " + r.count); render(); }
      catch (err) { toast(err.message, true); }
    });
    $("#btn-fetch").addEventListener("click", async () => {
      const g = prompt("group_id набора из письма организаторов (например DEV, FBroker, SWAP):", "DEV"); if (!g) return;
      try { const r = await api("/api/datasets/fetch", "POST", { group_id: g }); toast("Скачано " + r.count + " диалогов (" + r.group_id + ")"); render(); }
      catch (err) { toast(err.message, true); }
    });
    $("#btn-batch").addEventListener("click", async () => {
      const g = $("#f-group").value || groups[0]; if (!g) return toast("нет набора", true);
      try { await api("/api/evaluate/batch", "POST", { group_id: g, checklist_id: $("#batch-checklist").value || null }); pollJob(); }
      catch (err) { toast(err.message, true); }
    });
    $("#btn-reeval").addEventListener("click", async () => {
      const g = $("#f-group").value || groups[0]; if (!g) return toast("нет набора", true);
      const n = convs.filter(c => c.group_id === g).length;
      if (!confirm(`Заново оценить все ${n} диалогов набора «${groupName(g)}»? Старые оценки останутся в истории карточек.`)) return;
      try { await api("/api/evaluate/batch", "POST", { group_id: g, checklist_id: $("#batch-checklist").value || null, force: true }); pollJob(); }
      catch (err) { toast(err.message, true); }
    });
    $("#btn-clear").addEventListener("click", async () => {
      const g = $("#f-group").value || groups[0]; if (!g) return toast("нет набора", true);
      if (!confirm(`Удалить ВСЕ оценки и апелляции набора «${groupName(g)}»? Это нельзя отменить. Действие попадёт в журнал.`)) return;
      try { const r = await api("/api/evaluations/delete", "POST", { group_id: g }); toast("Удалено оценок: " + r.removed); render(); }
      catch (err) { toast(err.message, true); }
    });
    $("#btn-export").addEventListener("click", async () => {
      const g = $("#f-group").value || groups[0]; if (!g) return toast("нет набора", true);
      const folder = g === "DEV" ? "dev" : (prompt("Папка результата (dev или prod):", "prod") || "").trim();
      if (!folder) return;
      try { const r = await api("/api/export", "POST", { group_id: g, folder }); toast("Записано " + r.path + ": ok " + r.ok_records + ", error " + r.error_records + " из " + r.total_records + (r.problems.length ? " · ПРОБЛЕМЫ: " + r.problems.join("; ") : ""), r.problems.length > 0); }
      catch (err) { toast(err.message, true); }
    });
    function pollJob() {
      clearInterval(state.jobTimer);
      state.jobTimer = setInterval(async () => {
        const js = await api("/api/jobs"); const j = js[0]; const box = $("#job"); if (!box) return clearInterval(state.jobTimer);
        if (!j) return;
        const done = j.done + j.failed; box.hidden = false;
        box.innerHTML = `<div class="row small"><span>${j.status === "running" ? '<span class="spinner"></span>' : "✔"} ${esc(j.group_id)}: ${done} / ${j.total} (ошибок ${j.failed}, уже были оценены ${j.skipped})</span></div>
          <div class="progress"><div style="width:${j.total ? Math.round(done / j.total * 100) : 100}%"></div></div>
          ${j.errors.slice(0, 5).map(e => `<div class="small" style="color:#d64545">${esc(e.conversation_id)}: ${esc(e.error)}</div>`).join("")}`;
        if (j.status !== "running") { clearInterval(state.jobTimer); render(); }
      }, 1500);
    }
    if (running) pollJob();
  }

  // ------------------------------------------------------------ card view
  async function renderCard(args) {
    const cid = args[0]; const autoEval = args[1] === "evaluate";
    const conv = await api("/api/conversations/" + encodeURIComponent(cid));
    state.checklists = await api("/api/checklists");
    state.conv = conv;
    let ev = null;
    if (args[1] && args[1] !== "evaluate") ev = await api("/api/evaluations/" + args[1]);
    else if (conv.evaluations.length) ev = await api("/api/evaluations/" + conv.evaluations[0].id);
    state.ev = ev;
    const defaultCl = (state.settings && state.settings.default_checklists && state.settings.default_checklists[conv.channel]) || (conv.channel === "voice call" ? "call" : "chat");
    view.innerHTML = `
      <div class="row" style="margin-bottom:12px">
        <button class="btn secondary sm" id="back">← К списку</button>
        <h2 style="margin:0">${esc(conv.conversation_id)}</h2>
        <span class="tag">${conv.channel === "voice call" ? "звонок" : "чат"}</span><span class="tag lang">${esc(conv.language)}</span><span class="tag">${esc(groupName(conv.group_id))}</span>
        <span class="grow"></span>
        <span class="muted small">Оценивать по чек-листу:</span>
        <select id="cl-select" style="width:230px" title="Какой чек-лист и какая его версия будут использованы при следующей оценке">${state.checklists.map(c => `<option value="${esc(c.id)}" ${c.id === defaultCl ? "selected" : ""}>${esc(c.name)} (v${c.version})</option>`).join("")}</select>
        <button class="btn ${ev ? "secondary" : ""}" id="btn-eval" title="${ev ? "Создаст новую оценку по выбранному чек-листу, прежняя останется в истории" : "Модель прочитает диалог и оценит оператора по каждому критерию"}">${ev ? "Оценить заново" : "Запустить оценку"}</button>
      </div>
      <div class="card">
        <div class="panel" id="left"></div>
        <div class="panel" id="right"></div>
      </div>`;
    $("#back").addEventListener("click", () => go("/list"));
    renderTranscript(conv, ev);
    renderResult(conv, ev);
    $("#btn-eval").addEventListener("click", runEval);
    if (autoEval && !ev) runEval();

    async function runEval() {
      const b = $("#btn-eval"); b.disabled = true; b.innerHTML = '<span class="spinner"></span>Оцениваю…';
      $("#right").innerHTML = '<div class="empty"><span class="spinner" style="border-color:#999;border-right-color:transparent"></span> Модель анализирует диалог…</div>';
      try {
        const r = await api("/api/evaluate", "POST", { conversation_id: cid, checklist_id: $("#cl-select").value });
        toast("Оценка готова: " + fmtScore(r.score.total)); go("/card/" + encodeURIComponent(cid) + "/" + r.id);
        if (location.hash.endsWith("/" + r.id)) render();
      } catch (e) { toast(e.message, true); b.disabled = false; b.textContent = ev ? "Оценить заново" : "Запустить оценку"; renderResult(conv, ev); }
    }
  }

  function renderTranscript(conv, ev) {
    const left = $("#left");
    const audio = conv.audio_url ? `<audio id="audio" controls preload="none" src="${esc(conv.audio_url)}"></audio>` : "";
    const metrics = conv.metrics ? `<details class="raw"><summary>Метрики звонка: ${conv.metrics.operator_words} слов оператора, ${conv.metrics.operator_active_minutes || "?"} мин, ${conv.metrics.long_pauses.length} пауз ≥15 с</summary>
      <div class="small">${conv.metrics.long_pauses.map(p => `пауза ${p.seconds} с перед #${p.before + 1} (${roleName(p.next_speaker)})`).join("<br>") || "долгих пауз нет"}</div></details>` : "";
    left.innerHTML = `<h3 style="margin-top:0">Транскрипт <span class="muted small">(${conv.transcript.length} реплик)</span></h3>${audio}${metrics}
      <div class="transcript" id="transcript">${conv.transcript.map(m => `
        <div class="msg ${esc(m.speaker)}" id="m-${m.index}"><div class="idx">#${m.index + 1}</div><div class="who">${roleName(m.speaker)}</div>
        <div>${esc(m.text)}<span class="ts">${esc(m.offset)}${conv.audio_url && m.offset_sec != null ? ` <button class="play" data-sec="${m.offset_sec}" title="слушать с этого места">▶</button>` : ""}</span></div></div>`).join("")}</div>`;
    left.onclick = e => {
      const p = e.target.closest(".play"); if (!p) return;
      const a = $("#audio"); if (!a) return; a.currentTime = +p.dataset.sec; a.play().catch(() => toast("Запись пока недоступна", true));
    };
  }

  function highlight(ids, range) {
    document.querySelectorAll(".msg").forEach(m => m.classList.remove("highlight", "range"));
    if (range) for (let i = range[0]; i <= range[1]; i++) { const m = $("#m-" + i); if (m) m.classList.add("range"); }
    (ids || []).forEach(i => { const m = $("#m-" + i); if (m) m.classList.add("highlight"); });
    const first = $("#m-" + ((ids && ids.length) ? ids[0] : (range ? range[0] : -1)));
    if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function renderResult(conv, ev) {
    const right = $("#right");
    if (!ev) { right.innerHTML = '<div class="empty">Оценки ещё нет. Нажмите «Запустить оценку» — модель прочитает диалог и оценит оператора по каждому критерию чек-листа (30–90 секунд).</div>'; return; }
    const cl = ev.checklist; const fs = ev.final_score || ev.score;
    const changed = ev.has_changes;
    const critById = Object.fromEntries(cl.criteria.map(c => [c.id, c]));
    const history = conv.evaluations.length > 1 ? `<span class="muted small">История оценок:</span><select id="ev-hist" class="small" style="width:auto">${conv.evaluations.map(e => `<option value="${e.id}" ${e.id === ev.id ? "selected" : ""}>${esc(e.created_at.slice(0, 16).replace("T", " "))} · ${esc(e.checklist)} v${e.checklist_version} · ${fmtScore(e.total)}</option>`).join("")}</select>` : "";
    right.innerHTML = `
      <div class="row"><h3 style="margin:0">Результат оценки</h3><span class="muted small">${esc(cl.name)} v${cl.version} · ${esc(modelName(ev.model))}, ${ev.duration_sec || 0} с</span><span class="grow"></span>${history}</div>
      <div class="total" style="margin:10px 0">
        <span class="big score ${scoreClass(fs.total)}">${fmtScore(fs.total)}</span><span class="muted">из ${fs.max_total == null ? "—" : fs.max_total}</span>
        ${changed ? `<span class="strike" title="исходная оценка">${fmtScore(ev.score.total)}</span><span class="tag">итог после апелляции</span>` : ""}
        ${fs.critical_error ? '<span class="flag">⚠ грубая ошибка</span>' : '<span class="muted small">грубых ошибок нет</span>'}
        ${fs.total == null ? '<span class="muted small">итог не рассчитывается: нет применимых критериев</span>' : ""}
      </div>
      <p class="hint">Итог — сумма баллов по критериям с учётом их веса, максимум 100.${(fs.criteria || []).some(x => x.status === "na") ? " " + (fs.criteria || []).filter(x => x.status === "na").length + " критерий(я) не применимы к этому диалогу: баллы за них не начисляются, их вес распределён между остальными." : ""} Грубая ошибка отмечается отдельным флагом и итог не обнуляет.<br>${state.role === "operator"
        ? "Не согласны с ответом по критерию? Нажмите «Оспорить оценку» под ним и напишите, почему. Проверяющий рассмотрит апелляцию, и итог пересчитается."
        : "Открытые апелляции отмечены оранжевым. Выберите «Подтвердить» или «Изменить на…», укажите причину и нажмите «Сохранить» — итог пересчитается."}</p>
      <div class="sections">${(fs.sections || []).map(s => `<div><b>${esc(s.title)}</b>: ${fmtScore(s.points)} / ${fmtScore(s.max_points)}</div>`).join("")}</div>
      ${ev.summary ? `<p class="small muted">${esc(ev.summary)}</p>` : ""}
      ${ev.validation_errors && ev.validation_errors.length ? `<p class="small" style="color:#d64545">Проблемы ответа модели: ${esc(ev.validation_errors.join("; "))}</p>` : ""}
      <div id="crits">${ev.results.map(r => {
        const c = critById[r.criterion_id]; const finalAns = (ev.final_answers || {})[r.criterion_id];
        const appeals = (ev.appeals || []).filter(a => a.criterion_id === r.criterion_id);
        const open = appeals.find(a => a.status === "open");
        const frow = (fs.criteria || []).find(x => x.criterion_id === r.criterion_id) || {};
        const ans = finalAns != null ? finalAns : r.answer;
        return `<div class="crit" data-cid="${esc(c.id)}">
          <div class="head"><span class="ans ${answerClass(c, ans)}">${answerLabel(c, ans)}</span>
            <div><b>${esc(c.title)}</b> <span class="muted small">${c.critical ? "· отдельный флаг, без баллов" : "· вес " + c.weight + " б." + (frow.max_points != null && Math.abs(frow.max_points - c.weight) > 0.005 ? " → " + fmtScore(frow.max_points) + " после перераспределения N/A" : "")}${c.type === "scale" ? " · шкала 0–" + Math.max(...c.scale.map(s => +s.value)) : ""}${r.confidence != null ? ` · <span title="Насколько модель уверена в ответе по этому критерию. Ниже 60% — стоит проверить вручную"${r.confidence < 0.6 ? ' class="warn-text"' : ""}>модель уверена на ${Math.round(r.confidence * 100)}%</span>` : ""}</span></div>
            <span class="pts">${c.critical ? (ans === "yes" ? '<span class="flag">⚠ нарушение</span>' : '<span class="muted small">нет</span>') : frow.points == null ? '<span class="muted small" title="Критерий неприменим к этому диалогу: баллы не начисляются, его вес распределён между остальными">не применимо</span>' : `<b>${fmtScore(frow.points)}</b> / ${fmtScore(frow.max_points)}`}</span></div>
          <div class="expl">${esc(r.explanation || "—")}</div>
          <div class="evid">${(r.evidence_ids || []).length || r.checked_range ? '<span class="muted small" style="margin-right:6px">Где в диалоге:</span>' : ""}${(r.evidence_ids || []).map(i => `<button data-ev="${i}" title="Показать реплику в транскрипте: ${esc(((conv.transcript[i] || {}).text || "").slice(0, 120))}">реплика #${i + 1} ${esc((conv.transcript[i] || {}).offset || "")}</button>`).join("")}
            ${r.checked_range ? `<button data-range="${r.checked_range[0]}-${r.checked_range[1]}" title="Участок диалога, где ожидалось действие оператора">проверен участок #${r.checked_range[0] + 1}–${r.checked_range[1] + 1}</button>` : ""}</div>
          ${finalAns != null && finalAns !== r.answer ? `<div class="changed">Исходная оценка модели: <b>${answerLabel(c, r.answer)}</b> → после пересмотра: <b>${answerLabel(c, finalAns)}</b></div>` : ""}
          ${appeals.map(a => `<div class="appeal ${a.status}"><b>Апелляция</b> (${esc(a.author)}, ${esc(a.created_at.slice(0, 16).replace("T", " "))}): ${esc(a.comment)}
            ${a.decision ? `<div style="margin-top:4px"><b>Решение (${esc(a.decision.reviewer)}):</b> ${a.decision.action === "confirm" ? "оценка подтверждена" : "оценка изменена на <b>" + answerLabel(c, a.decision.new_answer) + "</b>"} — ${esc(a.decision.reason)}</div>` : '<div class="muted small">ожидает решения проверяющего</div>'}</div>`).join("")}
          <div class="appeal-box" id="ab-${esc(c.id)}">${appealControls(c, ans, open)}</div>
        </div>`;
      }).join("")}</div>
      <div class="row" style="margin-top:10px"><details class="raw"><summary>Служебное: JSON оценки</summary><pre class="json">${esc(JSON.stringify(ev, null, 1).slice(0, 20000))}</pre></details><span class="grow"></span>${state.role === "reviewer" ? '<button class="btn secondary sm" id="ev-delete" title="Удалить эту оценку вместе с апелляциями">Удалить оценку</button>' : ""}</div>`;

    function appealControls(c, ans, open) {
      if (state.role === "operator") {
        if (open) return `<span class="muted small">Апелляция подана и ждёт решения. Чтобы вынести решение, переключите роль на «Проверяющий» справа вверху.</span>`;
        return `<button class="btn secondary sm" data-appeal="${esc(c.id)}" title="Не согласны с оценкой по этому критерию? Напишите комментарий — проверяющий рассмотрит">Оспорить оценку</button>`;
      }
      if (!open) return "";
      const opts = (c.type === "scale" ? c.scale.map(s => String(s.value)) : ["yes", "no"]).concat(c.allow_na ? ["na"] : []).filter(v => v !== ans);
      return `<div class="row"><b class="small">Решение проверяющего:</b>
        <select data-dec="${open.id}" style="width:auto"><option value="confirm">Подтвердить оценку</option>${opts.map(v => `<option value="${esc(v)}">Изменить на «${answerLabel(c, v)}»</option>`).join("")}</select>
        <input type="text" data-reason="${open.id}" placeholder="Причина решения" style="width:260px">
        <button class="btn sm" data-decide="${open.id}" data-crit="${esc(c.id)}">Сохранить</button></div>`;
    }

    if ($("#ev-delete")) $("#ev-delete").addEventListener("click", async () => {
      if (!confirm("Удалить эту оценку вместе с апелляциями? Это нельзя отменить. Действие попадёт в журнал.")) return;
      try { await api("/api/evaluations/" + ev.id, "DELETE"); toast("Оценка удалена"); go("/card/" + encodeURIComponent(conv.conversation_id)); render(); }
      catch (err) { toast(err.message, true); }
    });
    const hist = $("#ev-hist"); if (hist) hist.addEventListener("change", () => go("/card/" + encodeURIComponent(conv.conversation_id) + "/" + hist.value));
    right.onclick = async e => {
      const evb = e.target.closest("[data-ev]"); if (evb) return highlight([+evb.dataset.ev]);
      const rb = e.target.closest("[data-range]"); if (rb) { const [a, b] = rb.dataset.range.split("-").map(Number); return highlight([], [a, b]); }
      const ab = e.target.closest("[data-appeal]");
      if (ab) {
        const box = $("#ab-" + ab.dataset.appeal);
        box.innerHTML = `<div class="field"><label>Почему вы не согласны с оценкой по этому критерию?</label><textarea id="ap-text" placeholder="Например: клиент сам завершил чат, попрощаться было невозможно"></textarea></div>
          <div class="row" style="margin-top:6px"><button class="btn sm" data-send-appeal="${esc(ab.dataset.appeal)}">Отправить апелляцию</button><button class="btn secondary sm" data-cancel="1">Отмена</button></div>`;
        $("#ap-text").focus(); return;
      }
      if (e.target.closest("[data-cancel]")) return renderResult(conv, ev);
      const sb = e.target.closest("[data-send-appeal]");
      if (sb) {
        try { const r = await api("/api/evaluations/" + ev.id + "/appeals", "POST", { criterion_id: sb.dataset.sendAppeal, comment: $("#ap-text").value }); toast("Апелляция отправлена проверяющему"); state.ev = r; renderResult(conv, r); setStatus(); }
        catch (err) { toast(err.message, true); }
        return;
      }
      const db = e.target.closest("[data-decide]");
      if (db) {
        const id = db.dataset.decide; const sel = right.querySelector(`[data-dec="${id}"]`).value; const reason = right.querySelector(`[data-reason="${id}"]`).value;
        const body = sel === "confirm" ? { action: "confirm", reason } : { action: "change", new_answer: sel, reason };
        try { const r = await api("/api/evaluations/" + ev.id + "/appeals/" + id + "/decision", "POST", body); toast("Решение сохранено. Итог: " + fmtScore((r.final_score || r.score).total)); state.ev = r; conv.evaluations = conv.evaluations.map(x => x.id === r.id ? Object.assign(x, { total: (r.final_score || r.score).total }) : x); renderResult(conv, r); setStatus(); }
        catch (err) { toast(err.message, true); }
      }
    };
  }

  // ------------------------------------------------------------ appeals view
  async function renderAppeals() {
    const [list, lists] = await Promise.all([api("/api/appeals"), api("/api/checklists")]);
    const critTitle = id => { for (const cl of lists) { const c = cl.criteria.find(x => x.id === id); if (c) return c.title; } return id; };
    view.innerHTML = `<div class="row" style="margin-bottom:12px"><h2 style="margin:0">Апелляции</h2>
        <span class="muted small">Оператор оспаривает критерий → проверяющий подтверждает или меняет оценку с указанием причины → итог диалога пересчитывается. Нажмите на строку, чтобы открыть карточку в роли проверяющего.</span></div>
      <div class="panel" style="padding:0"><table class="list"><thead><tr><th>Статус</th><th>Диалог</th><th>Чек-лист</th><th>Критерий</th><th>Ответ модели</th><th>Комментарий оператора</th><th>Решение проверяющего</th><th>Когда подана</th></tr></thead><tbody>
      ${list.map(a => `<tr class="clickable" data-go="/card/${encodeURIComponent(a.conversation_id)}/${a.evaluation_id}">
        <td>${a.status === "open" ? '<span class="tag warn">открыта</span>' : '<span class="tag">решена</span>'}</td>
        <td><b>${esc(a.conversation_id)}</b></td><td class="small">${esc(a.checklist)}</td><td>${esc(critTitle(a.criterion_id))}</td><td>${esc(answerLabel({}, a.disputed_answer))}</td>
        <td class="small">${esc(a.comment)}</td><td class="small">${a.decision ? (a.decision.action === "confirm" ? "подтверждена" : "изменена → " + esc(answerLabel({}, a.decision.new_answer))) + ": " + esc(a.decision.reason) : "—"}</td>
        <td class="small">${esc(a.created_at.slice(0, 16).replace("T", " "))}</td></tr>`).join("") || '<tr><td colspan="8" class="empty">Апелляций пока нет. Оператор подаёт их из карточки диалога кнопкой «Оспорить оценку».</td></tr>'}
    </tbody></table></div>`;
    view.querySelectorAll("tr[data-go]").forEach(tr => tr.addEventListener("click", () => { setRole("reviewer"); toast("Вы перешли в роль проверяющего"); go(tr.dataset.go); }));
  }

  // ------------------------------------------------------------ analytics
  async function renderAnalytics() {
    const [groups, a] = await Promise.all([api("/api/datasets"), api("/api/analytics?group=" + encodeURIComponent(state.agroup || ""))]);
    const bar = (label, value, max, text, tip) => `<div class="bar-row" title="${esc(tip || label + ": " + text)}"><span class="bar-label">${esc(label)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${max && value != null ? Math.max(0, value / max * 100) : 0}%"></span></span><span class="bar-value">${text}</span></div>`;
    const avgRows = list => list.map(x => bar(x.key, x.avg, 100, `${fmtScore(x.avg)} <span class="muted">· ${x.count}</span>`, `${x.key}: средний балл ${fmtScore(x.avg)}, оценок ${x.count}`)).join("") || '<div class="muted small">Нет данных</div>';
    const maxBin = Math.max(1, ...a.distribution.map(d => d.count));
    view.innerHTML = `<div class="row" style="margin-bottom:14px"><h2 style="margin:0">Аналитика</h2>
        <span class="muted small">Где операторы чаще всего теряют баллы и как распределены итоги. Считается по последней оценке каждого диалога с учётом апелляций.</span><span class="grow"></span>
        <select id="an-group" style="width:200px"><option value="">Все наборы</option>${groups.map(g => `<option value="${esc(g.group_id)}" ${state.agroup === g.group_id ? "selected" : ""}>${esc(groupName(g.group_id))}</option>`).join("")}</select></div>
      <div class="kpis" style="margin-bottom:16px">
        <div class="kpi"><b>${a.count}</b><span>оценено диалогов</span></div>
        <div class="kpi accent"><b>${fmtScore(a.avg)}</b><span>средний балл</span></div>
        <div class="kpi ${a.critical ? "alert" : ""}"><b>${a.critical}</b><span>с грубыми ошибками</span></div>
        <div class="kpi"><b>${a.appeals_open}</b><span>открытых апелляций</span></div>
        <div class="kpi"><b>${a.appeals_changed}</b><span>оценок изменено по апелляции</span></div>
      </div>
      ${a.count ? `<div class="an-grid">
        <div class="panel"><h3 style="margin-top:0">Сколько операторы набирают по каждому критерию, % от максимума</h3>
          ${a.criteria.map(c => c.critical
            ? bar(c.title, null, 100, `нарушений: <b>${c.fails}</b> из ${c.scored}`, `${c.title}: нарушения в ${c.fails} из ${c.scored} диалогов`)
            : bar(c.title, c.avg_percent, 100, `${fmtScore(c.avg_percent)}% <span class="muted">· N/A ${c.na}</span>`, `${c.title}: ${fmtScore(c.avg_percent)}% в среднем, 0 баллов в ${c.fails} диалогах, N/A в ${c.na}`)).join("")}
          <p class="small muted" style="margin:10px 0 0">Самые короткие полосы — то, чему в первую очередь учить операторов. Для «Грубых ошибок» показано число диалогов с нарушением.</p></div>
        <div class="panel"><h3 style="margin-top:0">Сколько диалогов получили какой итог</h3>
          ${a.distribution.map(d => bar(d.key, d.count, maxBin, String(d.count), `${d.key} баллов: ${d.count} диалогов`)).join("")}
          <h3>Средний итог по языкам</h3>${avgRows(a.by_language)}
          <h3>Средний итог по каналам</h3>${avgRows(a.by_channel.map(x => Object.assign({}, x, { key: x.key === "voice call" ? "звонки" : x.key === "agent chat" ? "чаты" : x.key })))}</div>
      </div>` : '<div class="panel empty">Оценок пока нет. Откройте вкладку «Диалоги» и нажмите «Оценить неоценённые».</div>'}`;
    $("#an-group").addEventListener("change", e => { state.agroup = e.target.value; render(); });
  }

  // ------------------------------------------------------------ audit log
  async function renderAudit() {
    const list = await api("/api/audit");
    const actors = [...new Set(list.map(a => a.actor))];
    view.innerHTML = `<div class="row" style="margin-bottom:12px"><h2 style="margin:0">Журнал действий</h2>
        <span class="muted small">Все изменения оценок, апелляций, чек-листов и настроек. Записи не редактируются и не удаляются.</span><span class="grow"></span>
        <select id="a-actor" style="width:180px"><option value="">Все участники</option>${actors.map(a => `<option>${esc(a)}</option>`).join("")}</select>
        <input id="a-q" type="text" placeholder="Поиск" style="width:200px"></div>
      <div class="panel" style="padding:0"><table class="list"><thead><tr><th style="width:150px">Когда</th><th style="width:130px">Кто</th><th style="width:240px">Действие</th><th>Подробности</th></tr></thead><tbody id="a-rows"></tbody></table></div>`;
    function apply() {
      const who = $("#a-actor").value, q = $("#a-q").value.trim().toLowerCase();
      const rows = list.filter(a => (!who || a.actor === who) && (!q || (a.action + " " + a.details).toLowerCase().includes(q)));
      $("#a-rows").innerHTML = rows.map(a => `<tr><td class="small muted">${esc(a.at.slice(0, 19).replace("T", " "))}</td><td><span class="tag ${a.actor === "Проверяющий" ? "voice" : a.actor === "Оператор" ? "lang" : ""}">${esc(a.actor)}</span></td>
        <td><b>${esc(a.action)}</b></td><td class="small">${esc(a.details)}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">Записей нет</td></tr>';
    }
    apply();
    $("#a-actor").addEventListener("change", apply); $("#a-q").addEventListener("input", apply);
  }

  // ------------------------------------------------------------ checklist editor
  async function renderChecklists(args) {
    const lists = await api("/api/checklists"); state.checklists = lists;
    const id = args[0] || (lists[0] && lists[0].id);
    let cl = id ? JSON.parse(JSON.stringify(lists.find(c => c.id === id) || lists[0])) : null;
    if (args[0] === "new") cl = { id: "", name: "Новый чек-лист", channel: "agent chat", description: "", rounding: 2, sections: [{ id: "main", title: "Основное" }], criteria: [] };
    view.innerHTML = `<div class="row" style="margin-bottom:12px"><h2 style="margin:0">Чек-листы</h2>
      <select id="cl-pick" style="width:260px">${lists.map(c => `<option value="${esc(c.id)}" ${cl && c.id === cl.id ? "selected" : ""}>${esc(c.name)} (v${c.version})</option>`).join("")}</select>
      <button class="btn secondary sm" id="cl-new">+ Новый</button><button class="btn secondary sm" id="cl-copy">Копировать как новый</button><span class="grow"></span>
      <button class="btn" id="cl-save">Сохранить как новую версию</button></div>
      <div class="panel" id="editor"></div>`;
    $("#cl-pick").addEventListener("change", e => go("/checklists/" + e.target.value));
    $("#cl-new").addEventListener("click", () => go("/checklists/new"));
    $("#cl-copy").addEventListener("click", () => { cl.id = cl.id + "_copy"; cl.name += " (копия)"; delete cl.version; draw(); });
    $("#cl-save").addEventListener("click", async () => {
      collect();
      try { const r = await api("/api/checklists", "POST", cl); toast("Сохранено: " + r.name + " v" + r.version + ". Чтобы применить: откройте диалог, выберите этот чек-лист и нажмите «Оценить заново»"); go("/checklists/" + r.id); if (location.hash === "#/checklists/" + r.id) render(); }
      catch (e) { toast(e.message, true); }
    });
    if (!cl) { $("#editor").innerHTML = '<div class="empty">Нет чек-листов</div>'; return; }
    draw();

    function draw() {
      const ed = $("#editor");
      ed.innerHTML = `<div class="row">
        <div class="field" style="width:160px"><label>ID (латиница)</label><input type="text" id="e-id" value="${esc(cl.id)}" ${cl.version ? "readonly" : ""}></div>
        <div class="field grow"><label>Название</label><input type="text" id="e-name" value="${esc(cl.name)}"></div>
        <div class="field" style="width:160px"><label>Для какого канала</label><select id="e-channel"><option value="agent chat" ${cl.channel === "agent chat" ? "selected" : ""}>Чат</option><option value="voice call" ${cl.channel === "voice call" ? "selected" : ""}>Звонок</option><option value="" ${!cl.channel ? "selected" : ""}>Любой</option></select></div>
        <div class="field" style="width:120px"><label>Версия</label><input type="text" value="${cl.version ? "v" + cl.version + " → v" + (cl.version + 1) : "новый"}" readonly></div></div>
        <div class="field" style="margin-top:8px"><label>Описание</label><input type="text" id="e-desc" value="${esc(cl.description || "")}"></div>
        <h3>Секции чек-листа <span class="muted small">— группы критериев, через запятую в виде код:Название, например start:Начало, safety:Безопасность</span></h3>
        <input type="text" id="e-sections" value="${esc((cl.sections || []).map(s => s.id + ":" + s.title).join(", "))}">
        <h3>Критерии <span class="muted small">— вес: максимум баллов за критерий; сумма весов обычно 100. Каждое сохранение создаёт новую версию, старые оценки хранят свою.</span></h3>
        <div id="e-crits">${cl.criteria.map((c, i) => critEditor(c, i)).join("")}</div>
        <div class="row" style="margin-top:8px"><button class="btn secondary sm" id="add-crit">+ Добавить критерий</button><span class="muted small">Сумма весов: <b id="wsum">${cl.criteria.filter(c => !c.critical).reduce((a, c) => a + (+c.weight || 0), 0)}</b></span></div>`;
      ed.querySelector("#add-crit").addEventListener("click", () => { collect(); cl.criteria.push({ id: "c" + String(cl.criteria.length + 1).padStart(2, "0"), section: (cl.sections[0] || {}).id || "", title: "", type: "binary", weight: 10, allow_na: false, description: "", na_condition: "", scale: [{ value: 10, label: "" }, { value: 0, label: "" }] }); draw(); });
      ed.onclick = e => {
        const t = e.target;
        if (t.dataset.del != null) { collect(); cl.criteria.splice(+t.dataset.del, 1); draw(); }
        if (t.dataset.up != null) { collect(); const i = +t.dataset.up; if (i > 0) { [cl.criteria[i - 1], cl.criteria[i]] = [cl.criteria[i], cl.criteria[i - 1]]; draw(); } }
        if (t.dataset.down != null) { collect(); const i = +t.dataset.down; if (i < cl.criteria.length - 1) { [cl.criteria[i + 1], cl.criteria[i]] = [cl.criteria[i], cl.criteria[i + 1]]; draw(); } }
        if (t.dataset.addStep != null) { collect(); (cl.criteria[+t.dataset.addStep].scale = cl.criteria[+t.dataset.addStep].scale || []).push({ value: 0, label: "" }); draw(); }
        if (t.dataset.delStep != null) { collect(); const [i, j] = t.dataset.delStep.split("-").map(Number); cl.criteria[i].scale.splice(j, 1); draw(); }
      };
      ed.onchange = e => { if (e.target.dataset.type != null) { collect(); draw(); } if (e.target.dataset.weight != null) collect(); };
    }
    function critEditor(c, i) {
      return `<div class="crit-edit" data-i="${i}">
        <div class="grid">
          <div class="field"><label>ID</label><input type="text" data-f="id" value="${esc(c.id)}"></div>
          <div class="field"><label>Название</label><input type="text" data-f="title" value="${esc(c.title)}"></div>
          <div class="field"><label>Тип</label><select data-f="type" data-type="1"><option value="binary" ${c.type === "binary" ? "selected" : ""}>Да / Нет</option><option value="scale" ${c.type === "scale" ? "selected" : ""}>Шкала</option></select></div>
          <div class="field"><label>Вес</label><input type="number" data-f="weight" data-weight="1" min="0" value="${c.weight}"></div>
          <div class="field"><label>Секция</label><select data-f="section">${(cl.sections || []).map(s => `<option value="${esc(s.id)}" ${c.section === s.id ? "selected" : ""}>${esc(s.title)}</option>`).join("")}</select></div>
          <div class="field"><label>&nbsp;</label><label class="small" title="Можно ли ответить «не применимо»"><input type="checkbox" data-f="allow_na" ${c.allow_na ? "checked" : ""}> может быть N/A</label><label class="small" title="Грубая ошибка: без баллов, ставит флаг ⚠"><input type="checkbox" data-f="critical" ${c.critical ? "checked" : ""}> грубая ошибка</label></div>
        </div>
        <div class="field" style="margin-top:6px"><label>Правило: что значит «Да» или как ставить балл</label><textarea data-f="description" style="min-height:44px">${esc(c.description || "")}</textarea></div>
        <div class="field" style="margin-top:6px"><label>Когда критерий неприменим (условие N/A)</label><input type="text" data-f="na_condition" value="${esc(c.na_condition || "")}" ${c.allow_na ? "" : "disabled"}></div>
        ${c.type === "scale" ? `<div style="margin-top:6px"><label class="small muted">Ступени шкалы (верхняя даёт полный вес)</label>${(c.scale || []).map((s, j) => `<div class="scale-row"><input type="number" data-s="${j}" data-sv="1" value="${s.value}"><input type="text" data-s="${j}" data-sl="1" value="${esc(s.label)}" placeholder="описание ступени"><button class="btn secondary sm" data-del-step="${i}-${j}">×</button></div>`).join("")}
          <button class="btn secondary sm" data-add-step="${i}" style="margin-top:4px">+ ступень</button></div>` : ""}
        <div class="tools"><button class="btn secondary sm" data-up="${i}">↑</button><button class="btn secondary sm" data-down="${i}">↓</button><button class="btn danger sm" data-del="${i}">Удалить</button></div>
      </div>`;
    }
    function collect() {
      cl.id = $("#e-id").value.trim(); cl.name = $("#e-name").value.trim(); cl.channel = $("#e-channel").value; cl.description = $("#e-desc").value.trim();
      cl.sections = $("#e-sections").value.split(",").map(s => s.trim()).filter(Boolean).map(s => { const [id, ...t] = s.split(":"); return { id: id.trim(), title: (t.join(":") || id).trim() }; });
      cl.criteria = [...view.querySelectorAll(".crit-edit")].map(el => {
        const g = f => el.querySelector(`[data-f="${f}"]`);
        const c = { id: g("id").value.trim(), title: g("title").value.trim(), type: g("type").value, weight: +g("weight").value || 0, section: g("section").value,
          allow_na: g("allow_na").checked, critical: g("critical").checked, description: g("description").value.trim(), na_condition: g("na_condition").value.trim() };
        if (c.type === "scale") c.scale = [...el.querySelectorAll("[data-sv]")].map((v, j) => ({ value: +v.value, label: el.querySelector(`[data-sl][data-s="${j}"]`).value.trim() }));
        if (c.critical) c.weight = 0;
        return c;
      });
      const w = $("#wsum"); if (w) w.textContent = cl.criteria.filter(c => !c.critical).reduce((a, c) => a + c.weight, 0);
    }
  }

  // ------------------------------------------------------------ settings
  async function renderSettings() {
    const [s, lists, models] = await Promise.all([api("/api/settings"), api("/api/checklists"), api("/api/models")]);
    state.settings = s;
    const opt = (list, cur) => (list.includes(cur) ? list : [cur, ...list]).map(m => `<option ${m === cur ? "selected" : ""}>${esc(m)}</option>`).join("");
    view.innerHTML = `<div class="panel" style="max-width:820px">
      <h2>Настройки</h2>
      <p class="muted small" style="margin-top:-6px">Здесь выбирают ИИ-модель, которая ставит оценки, и чек-листы, которые подставляются автоматически.</p>
      <h3>ИИ-модель, которая оценивает диалоги</h3>
      <p class="small">Сейчас оценивает: <b>${esc(prettyModel(s.ollama_model))}</b> — работает на этом компьютере, диалоги клиентов никуда не отправляются, оценка ничего не стоит. Один чат — 30–90 секунд, звонок — до 3 минут.${models.ollama.length ? "" : ' <span class="warn-text">Ollama не запущена: откройте приложение Ollama и обновите страницу.</span>'}</p>
      <h3>Чек-лист по умолчанию для новых оценок</h3>
      <p class="small muted" style="margin-top:-4px">Подставляется автоматически, когда вы открываете диалог или оцениваете весь набор. В карточке чек-лист можно сменить вручную.</p>
      <div class="row">${["agent chat", "voice call"].map(ch => `<div class="field" style="width:260px"><label>${ch === "agent chat" ? "Для чатов" : "Для звонков"}</label><select data-def="${ch}">${lists.map(c => `<option value="${esc(c.id)}" ${s.default_checklists[ch] === c.id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></div>`).join("")}</div>
      <details class="raw" style="margin-top:14px"><summary>Дополнительно: смена модели, сдача результатов, журнал</summary>
        <div class="row" style="margin-top:8px"><div class="field" style="width:300px"><label>Локальная модель (установлено: ${models.ollama.length})</label><select id="s-ollama-model">${opt(models.ollama, s.ollama_model)}</select></div>
          <div class="field grow"><label>Адрес сервера Ollama (менять не нужно)</label><input type="text" id="s-ollama-url" value="${esc(s.ollama_url)}"></div></div>
        <div class="field" style="max-width:420px;margin-top:8px"><label>Название команды — подпись в файле results.json для организаторов хакатона</label><input type="text" id="s-team" value="${esc(s.team_name || "")}"></div>
        <p class="small muted"><a href="#/audit">Журнал действий</a> — кто и что менял в оценках, апелляциях, чек-листах и настройках.</p></details>
      <div class="row" style="margin-top:16px"><button class="btn" id="s-save">Сохранить</button></div>
    </div>`;
    $("#s-save").addEventListener("click", async () => {
      const patch = { team_name: $("#s-team").value.trim(), provider: "ollama",
        ollama_url: $("#s-ollama-url").value.trim(), ollama_model: $("#s-ollama-model").value,
        default_checklists: Object.fromEntries([...view.querySelectorAll("[data-def]")].map(el => [el.dataset.def, el.value])) };
      try { state.settings = await api("/api/settings", "PUT", patch); toast("Настройки сохранены"); setStatus(); } catch (e) { toast(e.message, true); }
    });
  }

  api("/api/settings").then(s => { state.settings = s; }).finally(render);
})();
