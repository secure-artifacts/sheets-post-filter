const $ = (id) => document.getElementById(id);

const templateNames = {
  filter: "贴文筛选汇总",
  catalog: "目录表驱动汇总",
  align: "字段映射 / 表头对齐",
  video: "视频提取时长",
  custom: "自定义数据汇总",
};

const scalarFields = {
  filter: ["credentials_file", "target_url", "hot_target_url", "output_sheet", "hot_output_sheet", "output_start_row", "hot_start_row", "start_date", "end_date", "likes_threshold", "schedule_minutes", "exclude_id_value", "date_field", "sort_field", "cf_publish_url", "cf_publish_secret", "cf_publish_source"],
  catalog: ["catalog_index_url", "catalog_index_sheet", "catalog_start_row", "catalog_url_col", "catalog_sheet_col", "catalog_target_url", "catalog_output_sheet", "catalog_output_start_row"],
  align: ["align_target_url", "align_output_sheet", "align_start_row", "align_source_sheet", "align_header_row", "align_schedule_minutes"],
  video: ["vd_source_url", "vd_source_sheets", "vd_start_row", "vd_col_date", "vd_col_link", "vd_col_name", "vd_col_type", "vd_types", "vd_start_date", "vd_end_date", "vd_type_filter_mode", "vd_dest_url", "vd_report_sheet", "vd_log_sheet", "vd_out_start_row", "vd_count_mode", "vd_unit_seconds", "vd_batch_size", "vd_schedule_minutes"],
  custom: ["vd_source_url", "vd_source_sheets", "vd_start_row", "vd_col_date", "vd_col_link", "vd_col_name", "vd_col_type", "vd_types", "vd_start_date", "vd_end_date", "vd_type_filter_mode", "vd_dest_url", "vd_report_sheet", "vd_log_sheet", "vd_out_start_row", "vd_count_mode", "vd_unit_seconds", "vd_batch_size", "vd_schedule_minutes"],
};

const checkFields = {
  filter: ["include_headers", "hot_include_headers", "add_source_column", "sort_descending", "write_all", "write_hot", "upsert_by_id", "schedule_enabled", "schedule_only_if_changed", "cf_publish_after_sync"],
  catalog: ["catalog_keep_each_header"],
  align: ["align_include_headers", "align_schedule_enabled", "align_schedule_only_if_changed"],
  video: ["vd_date_filter_enabled", "vd_write_log", "vd_include_headers", "vd_schedule_enabled"],
  custom: ["vd_date_filter_enabled", "vd_write_log", "vd_include_headers", "vd_schedule_enabled"],
};

let defaultFields = [];
let menus = [];
let activeId = "";
let renderedId = "";
let currentMappingKey = "__default__";
let defaultMappings = [];
let mappingProfiles = {};

function newId(template) {
  return `${template}-${Date.now()}-${Math.random().toString(16).slice(2, 7)}`;
}

function defaultMenusFromConfig(cfg) {
  return Object.keys(templateNames).map((template) => ({
    id: `${template}-default`,
    name: templateNames[template],
    template,
    settings: collectInitialSettings(cfg, template),
  }));
}

function normalizeMenus(cfg) {
  const stored = Array.isArray(cfg.ui_menus) ? JSON.parse(JSON.stringify(cfg.ui_menus)) : [];
  if (!stored.length) return defaultMenusFromConfig(cfg);
  let migratedCustom = false;
  stored.forEach((item) => {
    if (item.template === "video" && (item.settings?.vd_write_log === false || String(item.name || "").includes("自定义"))) {
      item.template = "custom";
      item.settings = { ...(item.settings || {}), vd_write_log: false };
      migratedCustom = true;
    }
  });
  if (migratedCustom && !stored.some((item) => item.template === "video")) {
    stored.splice(Math.max(0, stored.length - 1), 0, {
      id: "video-original-default",
      name: templateNames.video,
      template: "video",
      settings: collectInitialSettings(cfg, "video"),
    });
  }
  if (!stored.some((item) => item.template === "custom")) {
    stored.push({ id: "custom-default", name: templateNames.custom, template: "custom", settings: collectInitialSettings(cfg, "custom") });
  }
  return stored.filter((item) => templateNames[item.template]);
}

function collectInitialSettings(cfg, template) {
  const result = {};
  [...(scalarFields[template] || []), ...(checkFields[template] || [])].forEach((key) => {
    if (cfg[key] !== undefined) result[key] = cfg[key];
  });
  if (template === "filter") {
    result.sources = cfg.sources || cfg.source_urls || [];
    result.fields = cfg.fields || [];
  }
  if (template === "align") {
    result.align_sources = cfg.align_sources || [];
    result.align_mappings = cfg.align_mappings || (cfg.align_headers || []).map((name) => ({ target: name, source: name }));
    result.align_mapping_profiles = cfg.align_mapping_profiles || {};
  }
  if (template === "video" || template === "custom") {
    result.vd_columns = cfg.vd_columns || [];
    result.vd_write_log = template === "video";
  }
  return result;
}

function activeMenu() {
  return menus.find((item) => item.id === activeId) || menus[0];
}

function setState(kind, text) {
  $("jobState").className = `badge ${kind}`;
  $("jobState").textContent = text;
}

function showMessage(text, bad = false) {
  $("summary").hidden = false;
  $("summary").textContent = text;
  if (bad) setState("bad", "需要检查");
}

function renderMenus() {
  $("menuList").innerHTML = "";
  menus.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `menu-item${item.id === activeId ? " on" : ""}`;
    button.innerHTML = `<span><strong></strong><small>${templateNames[item.template] || item.template}</small></span><span class="menu-more">›</span>`;
    button.querySelector("strong").textContent = item.name;
    button.addEventListener("click", () => switchMenu(item.id));
    button.addEventListener("dblclick", (event) => { event.preventDefault(); renameActive(item.id); });
    $("menuList").appendChild(button);
  });
}

function switchMenu(id) {
  const previous = renderedId ? menus.find((item) => item.id === renderedId) : null;
  if (previous) previous.settings = collectTemplate(previous.template);
  activeId = id;
  const item = activeMenu();
  if (!item) return;
  const panelTemplate = item.template === "custom" ? "video" : item.template;
  document.querySelectorAll(".template-panel").forEach((panel) => { panel.hidden = panel.id !== `panel-${panelTemplate}`; });
  $("workspaceTitle").textContent = item.name;
  $("templateLabel").textContent = templateNames[item.template] || "模板";
  fillTemplate(item.template, item.settings || {});
  renderedId = id;
  renderMenus();
  poll();
}

function renameActive(id = activeId) {
  const item = menus.find((entry) => entry.id === id);
  if (!item) return;
  const value = prompt("修改菜单名称：", item.name);
  if (value && value.trim()) {
    item.name = value.trim();
    if (id === activeId) $("workspaceTitle").textContent = item.name;
    renderMenus();
    saveConfig(true);
  }
}

function addSourceRow(containerId, item = {}) {
  const align = containerId === "alignSourceRows";
  const row = document.createElement("div");
  row.className = "src-row";
  row.innerHTML = `<input class="src-name" type="text" placeholder="${align ? "工作表名" : "小组名"}" /><input class="src-url" type="text" placeholder="https://docs.google.com/spreadsheets/d/…" /><button class="ghost src-del" type="button">删除</button>`;
  row.querySelector(".src-name").value = item.sheet || item.name || "";
  row.querySelector(".src-url").value = item.url || "";
  row.querySelector(".src-del").addEventListener("click", () => { row.remove(); ensureSourceRow(containerId); updateCounts(); if (align) refreshMappingProfileOptions(); });
  row.querySelectorAll("input").forEach((input) => input.addEventListener("input", () => { updateCounts(); if (align) refreshMappingProfileOptions(); }));
  if (align) row.querySelectorAll("input").forEach((input) => input.addEventListener("change", () => { saveCurrentMappingProfile(); refreshMappingProfileOptions(); }));
  $(containerId).appendChild(row);
}

function ensureSourceRow(containerId) {
  if (!$(containerId).children.length) addSourceRow(containerId, {});
}

function setSources(containerId, list) {
  $(containerId).innerHTML = "";
  (Array.isArray(list) && list.length ? list : [{}, {}]).forEach((item) => addSourceRow(containerId, typeof item === "string" ? { url: item } : item));
  updateCounts();
}

function readSources(containerId) {
  const align = containerId === "alignSourceRows";
  return [...$(containerId).querySelectorAll(".src-row")].map((row) => {
    const name = row.querySelector(".src-name").value.trim();
    const url = row.querySelector(".src-url").value.trim();
    return align ? { sheet: name, url } : { name, url };
  }).filter((item) => item.url);
}

function updateCounts() {
  $("sourceCount").textContent = `${readSources("sourceRows").length} 个链接`;
  $("alignSourceCount").textContent = `${readSources("alignSourceRows").length} 个链接`;
  $("fieldCount").textContent = `${readFieldMaps().length} 列`;
  $("mappingCount").textContent = `${readMappings().length} 个字段`;
}

function addFieldRow(item = {}) {
  const row = document.createElement("div");
  row.className = "field-row";
  row.innerHTML = `<input class="f-name" type="text" placeholder="字段名" /><input class="f-sheet" type="text" placeholder="当月贴文库" /><input class="f-range" type="text" placeholder="AB2:AB" /><button class="ghost src-del" type="button">删除</button>`;
  row.querySelector(".f-name").value = item.name || "";
  row.querySelector(".f-sheet").value = item.sheet || "当月贴文库";
  row.querySelector(".f-range").value = item.range || "";
  row.querySelector("button").addEventListener("click", () => { row.remove(); if (!$("fieldRows").children.length) addFieldRow(); updateCounts(); });
  $("fieldRows").appendChild(row);
}

function setFieldMaps(list) {
  $("fieldRows").innerHTML = "";
  (Array.isArray(list) && list.length ? list : defaultFields).forEach(addFieldRow);
  if (!$("fieldRows").children.length) addFieldRow();
  updateCounts();
}

function readFieldMaps() {
  return [...$("fieldRows").querySelectorAll(".field-row")].map((row) => ({ name: row.querySelector(".f-name").value.trim(), sheet: row.querySelector(".f-sheet").value.trim(), range: row.querySelector(".f-range").value.trim() })).filter((item) => item.name && item.range);
}

function addMappingRow(item = {}) {
  const row = document.createElement("div");
  row.className = "mapping-row";
  row.innerHTML = `<input class="map-target" type="text" placeholder="目标字段" /><input class="map-source" type="text" placeholder="源字段" /><button class="ghost" type="button">删除</button>`;
  row.querySelector(".map-target").value = item.target || "";
  row.querySelector(".map-source").value = item.source || item.target || "";
  row.querySelector("button").addEventListener("click", () => { row.remove(); if (!$("mappingRows").children.length) addMappingRow(); updateCounts(); });
  $("mappingRows").appendChild(row);
}

function setMappings(list) {
  $("mappingRows").innerHTML = "";
  (Array.isArray(list) && list.length ? list : [{}]).forEach(addMappingRow);
  updateCounts();
}

function readMappings() {
  return [...$("mappingRows").querySelectorAll(".mapping-row")].map((row) => ({ target: row.querySelector(".map-target").value.trim(), source: row.querySelector(".map-source").value.trim() || row.querySelector(".map-target").value.trim() })).filter((item) => item.target);
}

function addVdColumnRow(item = { field: "分类", role: "type", column: "" }) {
  const row = document.createElement("div");
  row.className = "vd-map-row";
  row.innerHTML = `<input class="vd-field" type="text" placeholder="字段名称" /><select class="vd-role"><option value="date">日期</option><option value="link">视频链接</option><option value="name">名字</option><option value="type">类型/分类</option></select><input class="vd-column" type="text" placeholder="E" /><button class="ghost" type="button">删除</button>`;
  row.querySelector(".vd-field").value = item.field || "分类";
  row.querySelector(".vd-role").value = item.role || "type";
  row.querySelector(".vd-column").value = item.column || "";
  row.querySelector("button").addEventListener("click", () => row.remove());
  $("vdColumnRows").appendChild(row);
}

function setVdColumns(list) {
  $("vdColumnRows").innerHTML = "";
  const defaults = [
    { field: "日期", role: "date", column: "A" },
    { field: "视频链接", role: "link", column: "B" },
    { field: "名字", role: "name", column: "H" },
    { field: "类型", role: "type", column: "E" },
  ];
  (Array.isArray(list) && list.length ? list : defaults).forEach(addVdColumnRow);
}

function readVdColumns() {
  return [...$("vdColumnRows").querySelectorAll(".vd-map-row")].map((row) => ({ field: row.querySelector(".vd-field").value.trim() || "分类", role: row.querySelector(".vd-role").value, column: row.querySelector(".vd-column").value.trim().toUpperCase() })).filter((item) => item.column);
}

function saveCurrentMappingProfile() {
  const value = readMappings();
  if (currentMappingKey === "__default__") defaultMappings = value;
  else mappingProfiles[currentMappingKey] = value;
}

function refreshMappingProfileOptions(selected = currentMappingKey) {
  const select = $("mappingProfile");
  select.innerHTML = `<option value="__default__">所有链接的默认映射</option>`;
  readSources("alignSourceRows").forEach((source, index) => {
    const option = document.createElement("option");
    option.value = source.url;
    option.textContent = `单独配置：${source.sheet || `链接 ${index + 1}`}`;
    select.appendChild(option);
  });
  select.value = [...select.options].some((option) => option.value === selected) ? selected : "__default__";
  currentMappingKey = select.value;
}

function collectTemplate(template) {
  const data = {};
  (scalarFields[template] || []).forEach((id) => { if ($(id)) data[id] = $(id).value.trim(); });
  (checkFields[template] || []).forEach((id) => { if ($(id)) data[id] = $(id).checked; });
  if (template === "filter") { data.sources = readSources("sourceRows"); data.source_urls = data.sources.map((source) => source.url); data.fields = readFieldMaps(); }
  if (template === "align") { saveCurrentMappingProfile(); data.align_sources = readSources("alignSourceRows"); data.align_mappings = defaultMappings; data.align_headers = defaultMappings.map((item) => item.target); data.align_mapping_profiles = mappingProfiles; }
  if (template === "video" || template === "custom") { data.vd_source_sheets = $("vd_source_sheets").value.split(/\r?\n|，|,/).map((x) => x.trim()).filter(Boolean); data.vd_types = $("vd_types").value.split(/\r?\n|，|,/).map((x) => x.trim()).filter(Boolean); data.vd_columns = readVdColumns(); data.vd_write_log = template === "video"; }
  return data;
}

function fillTemplate(template, settings) {
  (scalarFields[template] || []).forEach((id) => { if ($(id) && settings[id] !== undefined && settings[id] !== null) $(id).value = Array.isArray(settings[id]) ? settings[id].join("\n") : settings[id]; });
  (checkFields[template] || []).forEach((id) => { if ($(id) && typeof settings[id] === "boolean") $(id).checked = settings[id]; });
  if (template === "filter") { setSources("sourceRows", settings.sources || settings.source_urls || []); setFieldMaps(settings.fields || defaultFields); }
  if (template === "align") {
    setSources("alignSourceRows", settings.align_sources || []);
    defaultMappings = settings.align_mappings || (settings.align_headers || []).map((name) => ({ target: name, source: name }));
    mappingProfiles = settings.align_mapping_profiles || {};
    currentMappingKey = "__default__";
    refreshMappingProfileOptions();
    setMappings(defaultMappings);
  }
  if (template === "video" || template === "custom") {
    if (Array.isArray(settings.vd_source_sheets)) $("vd_source_sheets").value = settings.vd_source_sheets.join("\n");
    if (Array.isArray(settings.vd_types)) $("vd_types").value = settings.vd_types.join("\n");
    setVdColumns(settings.vd_columns || []);
    const custom = template === "custom";
    $("vd_write_log").checked = !custom;
    $("vdLogField").hidden = custom;
    $("vdCountModeField").hidden = custom;
    $("vdUnitField").hidden = custom;
    $("vdOutputTitle").textContent = custom ? "4. 写入分类数据表" : "4. 写入日志表和数据表";
    $("vdOutputNote").textContent = custom
      ? "自定义数据汇总只写数据表：第 1 行姓名、第 3 行类型、第 5 行汇总、第 8 行起为每日数据；刷新保留姓名顺序和样式。"
      : "视频提取时长会写入日志表和数据表；日志保存每条原视频的时长，已处理链接下次跳过并可断点续跑。";
  }
  updateCounts();
}

function fullPayload() {
  const item = activeMenu();
  if (item) item.settings = collectTemplate(item.template);
  return { ...(item ? item.settings : {}), ui_menus: menus, ui_active_menu: activeId };
}

async function saveConfig(silent = false) {
  const response = await fetch("/api/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fullPayload()) });
  const data = await response.json();
  if (!silent) setState(data.ok ? "ok" : "bad", data.ok ? "已保存" : "保存失败");
  return data.ok;
}

function renderSchedule(schedule, infoId, buttonId) {
  const info = $(infoId), button = $(buttonId);
  if (!schedule || !info || !button) return;
  if (!schedule.enabled) { info.textContent = schedule.last_sync ? `定时未启动 · 上次 ${schedule.last_sync}` : "定时未启动"; button.textContent = "保存并启动定时"; }
  else { info.textContent = `已启动 · 每 ${schedule.minutes} 分钟${schedule.next_run ? ` · 下次 ${schedule.next_run}` : ""}`; button.textContent = "停止定时"; }
}

async function loadConfig() {
  const response = await fetch("/api/config");
  const data = await response.json();
  const cfg = data.config || {};
  defaultFields = data.default_fields || [];
  menus = normalizeMenus(cfg);
  activeId = menus.some((item) => item.id === cfg.ui_active_menu) ? cfg.ui_active_menu : menus[0].id;
  $("saEmail").textContent = data.service_account_email || "未找到 credentials.json";
  $("copySa").disabled = !data.service_account_email;
  renderSchedule(data.schedule, "schedInfo", "schedBtn");
  renderSchedule(data.align_schedule, "alignSchedInfo", "alignSchedBtn");
  renderSchedule(data.video_schedule, "videoSchedInfo", "videoSchedBtn");
  switchMenu(activeId);
}

function setRunDisabled(disabled) {
  ["runBtn", "runCatalogBtn", "runAlignBtn", "runVideoBtn", "publishCfBtn"].forEach((id) => { if ($(id)) $(id).disabled = disabled; });
}

function renderLogs(logs) {
  $("log").textContent = (logs || []).map((line) => `[${line.t}] ${line.msg}`).join("\n");
  $("log").scrollTop = $("log").scrollHeight;
}

async function poll() {
  if (!activeId) return;
  const state = await (await fetch(`/api/status?job_id=${encodeURIComponent(activeId)}`)).json();
  renderLogs(state.logs);
  if (state.running) { setState("run", "运行中"); setRunDisabled(true); return; }
  setRunDisabled(false);
  if (state.error) { setState("bad", "失败"); showMessage(state.error); return; }
  if (!state.result) return;
  const result = state.result;
  setState(result.ok ? "ok" : "bad", result.ok ? "完成" : "部分失败");
  const failed = (result.sources || []).filter((item) => item.error).length;
  if (result.mode === "catalog") { showMessage(`目录汇总写入 ${result.total_rows} 行${failed ? ` · ${failed} 项失败` : ""}`); if (result.target_url) { $("openCatalog").href = result.target_url; $("openCatalog").hidden = false; } }
  else if (result.mode === "align") { showMessage(`字段映射写入 ${result.total_rows} 行${failed ? ` · ${failed} 个源失败` : ""}`); if (result.target_url) { $("openAlign").href = result.target_url; $("openAlign").hidden = false; } }
  else if (result.mode === "video" || result.mode === "video_custom") { showMessage(`${result.mode === "video_custom" ? "分类条数" : "视频时长"}数据表已更新 · ${result.people || 0} 人 · 本次 ${result.appended || 0} 条`); if (result.target_url) { $("openVideo").href = result.target_url; $("openVideo").hidden = false; } }
  else if (result.mode === "cloudflare") showMessage(result.skipped ? "内容未变化，已跳过发布" : `图库已发布 ${result.totalRows || 0} 条`);
  else { showMessage(`全部 ${result.total_rows || 0} 行${result.hot_rows != null ? ` · 高赞 ${result.hot_rows} 行` : ""}`); if (result.target_url) { $("openTarget").href = result.target_url; $("openTarget").hidden = false; } if (result.hot_url) { $("openHot").href = result.hot_url; $("openHot").hidden = false; } }
}

function beginJob() {
  $("summary").hidden = true;
  $("log").textContent = "";
  setState("run", "运行中");
  setRunDisabled(true);
  poll();
}

async function startJob(path, validate) {
  const payload = fullPayload();
  const error = validate ? validate(payload) : "";
  if (error) { showMessage(error, true); return; }
  const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const data = await response.json();
  if (!data.ok) { showMessage(data.error || "无法开始", true); return; }
  beginJob();
}

async function peekHeaders() {
  saveCurrentMappingProfile();
  const sources = readSources("alignSourceRows");
  const source = currentMappingKey === "__default__" ? sources[0] : sources.find((item) => item.url === currentMappingKey);
  if (!source) { showMessage("请先填写数据源链接", true); return; }
  const response = await fetch("/api/peek-headers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...fullPayload(), url: source.url, sheet: source.sheet || $("align_source_sheet").value.trim(), header_row: $("align_header_row").value }) });
  const data = await response.json();
  if (!data.ok) { showMessage(data.error || "读取表头失败", true); return; }
  const mappings = (data.headers || []).map((name) => ({ target: name, source: name }));
  setMappings(mappings);
  saveCurrentMappingProfile();
  setState("ok", "已读取表头");
}

$("addSource").addEventListener("click", () => addSourceRow("sourceRows"));
$("clearSources").addEventListener("click", () => setSources("sourceRows", []));
$("addAlignSource").addEventListener("click", () => { addSourceRow("alignSourceRows"); refreshMappingProfileOptions(); });
$("clearAlignSources").addEventListener("click", () => { setSources("alignSourceRows", []); refreshMappingProfileOptions(); });
$("addField").addEventListener("click", () => addFieldRow());
$("resetFields").addEventListener("click", () => setFieldMaps(defaultFields));
$("addMapping").addEventListener("click", () => addMappingRow());
$("addVdColumn").addEventListener("click", () => addVdColumnRow());
$("mappingProfile").addEventListener("change", () => { saveCurrentMappingProfile(); currentMappingKey = $("mappingProfile").value; setMappings(currentMappingKey === "__default__" ? defaultMappings : (mappingProfiles[currentMappingKey] || defaultMappings)); });
$("peekHeaders").addEventListener("click", peekHeaders);
document.querySelectorAll(".save-template").forEach((button) => button.addEventListener("click", () => saveConfig(false)));
$("runBtn").addEventListener("click", () => startJob("/api/run", (p) => !p.sources?.length ? "请至少填写一个源表链接" : (!p.start_date || !p.end_date ? "请填写开始和结束日期" : "")));
$("runCatalogBtn").addEventListener("click", () => startJob("/api/run-catalog", (p) => !p.catalog_index_url ? "请填写目录表链接" : (!p.catalog_target_url ? "请填写目标表链接" : "")));
$("runAlignBtn").addEventListener("click", () => startJob("/api/run-align", (p) => !p.align_sources?.length ? "请至少填写一个源表链接" : (!p.align_target_url ? "请填写目标表链接" : (!p.align_mappings?.length ? "请添加字段映射" : ""))));
$("runVideoBtn").addEventListener("click", () => startJob("/api/run-video", (p) => !p.vd_source_url ? "请填写源表链接" : (!p.vd_dest_url ? "请填写目标表链接" : "")));
$("publishCfBtn").addEventListener("click", () => startJob("/api/publish-cf", (p) => !p.cf_publish_url || !p.cf_publish_secret ? "请填写发布地址和密钥" : ""));

$("schedBtn").addEventListener("click", async () => { const enable = !$("schedBtn").textContent.includes("停止"); $("schedule_enabled").checked = enable; const data = await (await fetch("/api/schedule", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fullPayload()) })).json(); if (!data.ok) showMessage(data.error, true); else renderSchedule(data.schedule, "schedInfo", "schedBtn"); });
$("alignSchedBtn").addEventListener("click", async () => { const enable = !$("alignSchedBtn").textContent.includes("停止"); $("align_schedule_enabled").checked = enable; const data = await (await fetch("/api/align-schedule", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fullPayload()) })).json(); if (!data.ok) showMessage(data.error, true); else renderSchedule(data.align_schedule, "alignSchedInfo", "alignSchedBtn"); });
$("videoSchedBtn").addEventListener("click", async () => { const enable = !$("videoSchedBtn").textContent.includes("停止"); $("vd_schedule_enabled").checked = enable; const data = await (await fetch("/api/video-schedule", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fullPayload()) })).json(); if (!data.ok) { $("vd_schedule_enabled").checked = false; showMessage(data.error, true); } else renderSchedule(data.video_schedule, "videoSchedInfo", "videoSchedBtn"); });

$("renameMenu").addEventListener("click", () => renameActive());
$("deleteMenu").addEventListener("click", () => { if (menus.length <= 1) { showMessage("至少保留一个配置菜单", true); return; } const item = activeMenu(); if (!confirm(`删除配置“${item.name}”？`)) return; menus = menus.filter((entry) => entry.id !== activeId); activeId = menus[0].id; switchMenu(activeId); saveConfig(true); });
$("addMenu").addEventListener("click", () => { $("newMenuName").value = ""; $("menuDialog").showModal(); });
$("confirmAddMenu").addEventListener("click", (event) => { event.preventDefault(); const template = $("newMenuTemplate").value; const name = $("newMenuName").value.trim() || `${templateNames[template]}副本`; const base = menus.find((item) => item.template === template); const settings = base ? JSON.parse(JSON.stringify(base.settings || {})) : {}; const item = { id: newId(template), name, template, settings }; menus.push(item); $("menuDialog").close(); switchMenu(item.id); saveConfig(true); });
$("copySa").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("saEmail").textContent); $("copySa").textContent = "已复制"; setTimeout(() => $("copySa").textContent = "复制", 1000); } catch (_) { prompt("复制服务账号邮箱：", $("saEmail").textContent); } });

loadConfig();
setInterval(() => { poll().catch(() => {}); }, 800);
