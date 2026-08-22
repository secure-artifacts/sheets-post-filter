const $ = (id) => document.getElementById(id);

const fields = [
  "target_url",
  "hot_target_url",
  "output_sheet",
  "hot_output_sheet",
  "output_start_row",
  "hot_start_row",
  "start_date",
  "end_date",
  "likes_threshold",
  "schedule_minutes",
  "credentials_file",
  "exclude_id_value",
  "date_field",
  "sort_field",
  "align_target_url",
  "align_output_sheet",
  "align_start_row",
  "align_source_sheet",
  "align_header_row",
  "align_schedule_minutes",
];

const checks = [
  "include_headers",
  "hot_include_headers",
  "add_source_column",
  "sort_descending",
  "write_all",
  "write_hot",
  "upsert_by_id",
  "schedule_enabled",
  "schedule_only_if_changed",
  "align_include_headers",
  "align_schedule_enabled",
  "align_schedule_only_if_changed",
];

let defaultFields = [];

function namePlaceholder(containerId) {
  return containerId === "alignSourceRows" ? "例如：8月份" : "例如：管理组";
}

function addSourceRowTo(containerId, name, url, onCount) {
  const wrap = document.createElement("div");
  wrap.className = "src-row";
  const ph = namePlaceholder(containerId);
  wrap.innerHTML =
    `<input class="src-name" type="text" placeholder="${ph}" />` +
    `<input class="src-url" type="text" spellcheck="false" placeholder="https://docs.google.com/spreadsheets/d/……/edit" />` +
    `<button type="button" class="ghost src-del">删除</button>`;
  wrap.querySelector(".src-name").value = name || "";
  wrap.querySelector(".src-url").value = url || "";
  wrap.querySelector(".src-del").addEventListener("click", () => {
    wrap.remove();
    const box = $(containerId);
    if (!box.children.length) addSourceRowTo(containerId, "", "", onCount);
    if (onCount) onCount();
  });
  wrap.querySelector(".src-url").addEventListener("input", () => onCount && onCount());
  $(containerId).appendChild(wrap);
  if (onCount) onCount();
}

function readSourcesFrom(containerId) {
  const isAlign = containerId === "alignSourceRows";
  return [...$(containerId).querySelectorAll(".src-row")]
    .map((row) => {
      const label = row.querySelector(".src-name").value.trim();
      const url = row.querySelector(".src-url").value.trim();
      return isAlign ? { sheet: label, url } : { name: label, url };
    })
    .filter((s) => s.url);
}

function setSourcesTo(containerId, list, onCount) {
  $(containerId).innerHTML = "";
  const items = Array.isArray(list) ? list : [];
  if (!items.length) {
    addSourceRowTo(containerId, "", "", onCount);
    addSourceRowTo(containerId, "", "", onCount);
    return;
  }
  items.forEach((s) => {
    if (typeof s === "string") addSourceRowTo(containerId, "", s, onCount);
    else addSourceRowTo(containerId, s.sheet || s.name || "", s.url || "", onCount);
  });
}

function addSourceRow(name, url) {
  addSourceRowTo("sourceRows", name, url, updateSourceCount);
}
function addAlignSourceRow(name, url) {
  addSourceRowTo("alignSourceRows", name, url, updateAlignSourceCount);
}
function readSources() {
  return readSourcesFrom("sourceRows");
}
function readAlignSources() {
  return readSourcesFrom("alignSourceRows");
}
function setSources(list) {
  setSourcesTo("sourceRows", list, updateSourceCount);
}
function setAlignSources(list) {
  setSourcesTo("alignSourceRows", list, updateAlignSourceCount);
}

function addFieldRow(item) {
  const wrap = document.createElement("div");
  wrap.className = "field-row";
  wrap.innerHTML =
    `<input class="f-name" type="text" placeholder="字段名" />` +
    `<input class="f-sheet" type="text" placeholder="当月贴文库" />` +
    `<input class="f-range" type="text" placeholder="AB2:AB" spellcheck="false" />` +
    `<button type="button" class="ghost src-del">删除</button>`;
  wrap.querySelector(".f-name").value = (item && item.name) || "";
  wrap.querySelector(".f-sheet").value = (item && item.sheet) || "当月贴文库";
  wrap.querySelector(".f-range").value = (item && item.range) || "";
  wrap.querySelector(".src-del").addEventListener("click", () => {
    wrap.remove();
    if (!$("fieldRows").children.length) addFieldRow({});
    updateFieldCount();
  });
  $("fieldRows").appendChild(wrap);
  updateFieldCount();
}

function readFieldMaps() {
  return [...$("fieldRows").querySelectorAll(".field-row")]
    .map((row) => ({
      name: row.querySelector(".f-name").value.trim(),
      sheet: row.querySelector(".f-sheet").value.trim() || "当月贴文库",
      range: row.querySelector(".f-range").value.trim(),
    }))
    .filter((f) => f.name && f.range);
}

function setFieldMaps(list) {
  $("fieldRows").innerHTML = "";
  const items = Array.isArray(list) && list.length ? list : defaultFields;
  if (!items.length) {
    addFieldRow({});
    return;
  }
  items.forEach((f) => addFieldRow(f));
}

function updateSourceCount() {
  $("sourceCount").textContent = readSources().length + " 个链接";
}
function updateAlignSourceCount() {
  $("alignSourceCount").textContent = readAlignSources().length + " 个链接";
}
function updateFieldCount() {
  $("fieldCount").textContent = readFieldMaps().length + " 列";
}
function updateAlignHeaderCount() {
  const n = $("align_headers").value.split(/\r?\n/).filter((x) => x.trim()).length;
  $("alignHeaderCount").textContent = n + " 列";
}

function payload() {
  const data = {};
  for (const id of fields) data[id] = $(id).value.trim();
  for (const id of checks) data[id] = $(id).checked;
  data.sources = readSources();
  data.source_urls = data.sources.map((s) => s.url);
  data.fields = readFieldMaps();
  data.align_sources = readAlignSources();
  data.align_headers = $("align_headers").value;
  return data;
}

function fill(cfg) {
  for (const id of fields) {
    if (cfg[id] !== undefined && cfg[id] !== null && cfg[id] !== "") {
      $(id).value = cfg[id];
    }
  }
  for (const id of checks) {
    if (typeof cfg[id] === "boolean") $(id).checked = cfg[id];
  }
  if (Array.isArray(cfg.sources) && cfg.sources.length) {
    setSources(cfg.sources);
  } else {
    setSources(cfg.source_urls || []);
  }
  setFieldMaps(cfg.fields || defaultFields);
  setAlignSources(cfg.align_sources || []);
  if (Array.isArray(cfg.align_headers)) {
    $("align_headers").value = cfg.align_headers.join("\n");
  }
  updateAlignHeaderCount();
}

function renderSchedule(s, infoId, btnId) {
  const info = $(infoId || "schedInfo");
  const btn = $(btnId || "schedBtn");
  if (!s || !info || !btn) return;
  if (!s.enabled) {
    info.textContent = s.last_sync ? `定时未启动 · 上次同步 ${s.last_sync}` : "定时未启动";
    btn.textContent = "保存并启动定时";
    return;
  }
  info.textContent =
    `已启动 · 每 ${s.minutes} 分钟` +
    (s.next_run ? ` · 下次 ${s.next_run}` : "") +
    (s.last_sync ? ` · 上次 ${s.last_sync}` : "");
  btn.textContent = "停止定时";
}

async function loadConfig() {
  const res = await fetch("/api/config");
  const data = await res.json();
  defaultFields = data.default_fields || [];
  fill(data.config || {});
  renderSchedule(data.schedule);
  renderSchedule(data.align_schedule, "alignSchedInfo", "alignSchedBtn");
  const email = data.service_account_email || "未找到 credentials.json";
  $("saEmail").textContent = email;
  $("copySa").disabled = !data.service_account_email;
}

function setState(kind, text) {
  const el = $("jobState");
  el.className = "badge " + kind;
  el.textContent = text;
}

function setRunDisabled(disabled) {
  $("runBtn").disabled = disabled;
  $("runAlignBtn").disabled = disabled;
}

function renderLogs(logs) {
  $("log").textContent = (logs || [])
    .map((l) => `[${l.t}] ${l.msg}`)
    .join("\n");
  $("log").scrollTop = $("log").scrollHeight;
}

async function saveConfig(silent) {
  const res = await fetch("/api/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload()),
  });
  const data = await res.json();
  if (!silent) {
    setState(data.ok ? "ok" : "bad", data.ok ? "已保存" : "保存失败");
  }
  return data.ok;
}

let timer = null;

async function poll() {
  const res = await fetch("/api/status");
  const st = await res.json();
  renderLogs(st.logs);
  if (st.running) {
    setState("run", "运行中");
    setRunDisabled(true);
    return;
  }
  setRunDisabled(false);
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  if (st.error) {
    setState("bad", "失败");
    $("summary").hidden = false;
    $("summary").textContent = st.error;
    return;
  }
  if (st.schedule) renderSchedule(st.schedule);
  if (st.align_schedule) renderSchedule(st.align_schedule, "alignSchedInfo", "alignSchedBtn");
  if (st.skipped || (st.result && st.result.skipped)) {
    setState("ok", "已跳过");
    $("summary").hidden = false;
    $("summary").textContent = "源表没有变化，本次未写入";
    return;
  }
  if (st.result) {
    const r = st.result;
    setState(r.ok ? "ok" : "bad", r.ok ? "完成" : "部分失败");
    const failed = (r.sources || []).filter((s) => s.error).length;
    $("summary").hidden = false;
    if (r.mode === "align") {
      $("summary").textContent =
        `对齐写入 ${r.total_rows} 行` + (failed ? ` · ${failed} 个源失败` : "");
      if (r.target_url) {
        $("openAlign").hidden = false;
        $("openAlign").href = r.target_url;
      }
    } else {
      $("summary").textContent =
        `全部 ${r.total_rows} 行` +
        (r.hot_rows != null ? ` · 高赞(≥${r.likes_threshold}) ${r.hot_rows} 行` : "") +
        (failed ? ` · ${failed} 个源失败` : "");
      if (r.target_url) {
        $("openTarget").hidden = false;
        $("openTarget").href = r.target_url;
      }
      if (r.hot_url) {
        $("openHot").hidden = false;
        $("openHot").href = r.hot_url;
      }
    }
  } else {
    setState("idle", "待命");
  }
}

function beginJob(path) {
  setState("run", "运行中");
  setRunDisabled(true);
  if (timer) clearInterval(timer);
  timer = setInterval(poll, 700);
  poll();
}

async function startRun() {
  $("summary").hidden = true;
  $("openTarget").hidden = true;
  $("openHot").hidden = true;
  $("log").textContent = "";
  if (!readSources().length) {
    setState("bad", "缺数据源");
    $("summary").hidden = false;
    $("summary").textContent = "请填写至少一个源表链接";
    return;
  }
  if (!$("write_all").checked && !$("write_hot").checked) {
    setState("bad", "缺输出");
    $("summary").hidden = false;
    $("summary").textContent = "请至少勾选：全部结果，或高赞结果";
    return;
  }
  if ($("write_all").checked && !$("target_url").value.trim()) {
    setState("bad", "缺全部结果表");
    $("summary").hidden = false;
    $("summary").textContent = "请填写「全部结果」目标表链接";
    $("target_url").focus();
    return;
  }
  if ($("write_hot").checked && !$("hot_target_url").value.trim() && !$("target_url").value.trim()) {
    setState("bad", "缺高赞表");
    $("summary").hidden = false;
    $("summary").textContent = "请填写高赞目标表链接（或与全部结果用同一张表）";
    return;
  }
  if (!$("start_date").value || !$("end_date").value) {
    setState("bad", "缺日期");
    $("summary").hidden = false;
    $("summary").textContent = "请填写开始和结束日期";
    return;
  }
  if (!readFieldMaps().length) {
    setState("bad", "缺字段");
    $("summary").hidden = false;
    $("summary").textContent = "抓取字段不能为空";
    return;
  }
  const res = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload()),
  });
  const data = await res.json();
  if (!data.ok) {
    setState("bad", "无法开始");
    $("summary").hidden = false;
    $("summary").textContent = data.error || "未知错误";
    return;
  }
  beginJob();
}

async function startAlign() {
  $("summary").hidden = true;
  $("openAlign").hidden = true;
  $("log").textContent = "";
  if (!readAlignSources().length) {
    setState("bad", "缺数据源");
    $("summary").hidden = false;
    $("summary").textContent = "请填写至少一个源表链接";
    return;
  }
  if (!$("align_target_url").value.trim()) {
    setState("bad", "缺目标表");
    $("summary").hidden = false;
    $("summary").textContent = "请填写目标表链接";
    return;
  }
  if (!$("align_headers").value.trim()) {
    setState("bad", "缺表头");
    $("summary").hidden = false;
    $("summary").textContent = "请配置规范表头（一行一个）";
    return;
  }
  const res = await fetch("/api/run-align", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload()),
  });
  const data = await res.json();
  if (!data.ok) {
    setState("bad", "无法开始");
    $("summary").hidden = false;
    $("summary").textContent = data.error || "未知错误";
    return;
  }
  beginJob();
}

async function peekHeaders() {
  const first = readAlignSources()[0];
  if (!first) {
    setState("bad", "缺数据源");
    $("summary").hidden = false;
    $("summary").textContent = "请先填写至少一个源表链接";
    return;
  }
  $("peekHeaders").disabled = true;
  try {
    const res = await fetch("/api/peek-headers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...payload(),
        url: first.url,
        sheet: first.sheet || $("align_source_sheet").value.trim(),
        header_row: $("align_header_row").value,
      }),
    });
    const data = await res.json();
    if (!data.ok) {
      setState("bad", "读取失败");
      $("summary").hidden = false;
      $("summary").textContent = data.error || "读表头失败";
      return;
    }
    $("align_headers").value = (data.headers || []).join("\n");
    updateAlignHeaderCount();
    setState("ok", "已读表头");
    $("summary").hidden = false;
    $("summary").textContent = `从第一个源表读到 ${(data.headers || []).length} 个表头，可再增删调整`;
  } finally {
    $("peekHeaders").disabled = false;
  }
}

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("on"));
    btn.classList.add("on");
    const name = btn.getAttribute("data-tab");
    $("tab-filter").hidden = name !== "filter";
    $("tab-align").hidden = name !== "align";
  });
});

$("addSource").addEventListener("click", () => addSourceRow("", ""));
$("clearSources").addEventListener("click", () => setSources([]));
$("addAlignSource").addEventListener("click", () => addAlignSourceRow("", ""));
$("clearAlignSources").addEventListener("click", () => setAlignSources([]));
$("addField").addEventListener("click", () => addFieldRow({ sheet: "当月贴文库" }));
$("resetFields").addEventListener("click", () => setFieldMaps(defaultFields));
$("saveBtn").addEventListener("click", () => saveConfig(false));
$("saveAlignBtn").addEventListener("click", () => saveConfig(false));
$("runBtn").addEventListener("click", startRun);
$("runAlignBtn").addEventListener("click", startAlign);
$("peekHeaders").addEventListener("click", peekHeaders);
$("align_headers").addEventListener("input", updateAlignHeaderCount);
$("schedBtn").addEventListener("click", async () => {
  const enable = $("schedBtn").textContent.indexOf("停止") < 0;
  $("schedule_enabled").checked = enable;
  const res = await fetch("/api/schedule", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload()),
  });
  const data = await res.json();
  if (!data.ok) {
    setState("bad", "定时失败");
    $("summary").hidden = false;
    $("summary").textContent = data.error || "无法启动定时";
    $("schedule_enabled").checked = false;
    return;
  }
  renderSchedule(data.schedule);
});
$("alignSchedBtn").addEventListener("click", async () => {
  const enable = $("alignSchedBtn").textContent.indexOf("停止") < 0;
  $("align_schedule_enabled").checked = enable;
  const res = await fetch("/api/align-schedule", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload()),
  });
  const data = await res.json();
  if (!data.ok) {
    setState("bad", "定时失败");
    $("summary").hidden = false;
    $("summary").textContent = data.error || "无法启动定时";
    $("align_schedule_enabled").checked = false;
    return;
  }
  renderSchedule(data.align_schedule, "alignSchedInfo", "alignSchedBtn");
});
$("copySa").addEventListener("click", async () => {
  const t = $("saEmail").textContent;
  try {
    await navigator.clipboard.writeText(t);
    $("copySa").textContent = "已复制";
    setTimeout(() => ($("copySa").textContent = "复制"), 1200);
  } catch (e) {
    prompt("复制服务账号邮箱：", t);
  }
});
$("moreBtn").addEventListener("click", () => {
  const panel = $("morePanel");
  panel.hidden = !panel.hidden;
  $("moreBtn").textContent = panel.hidden ? "高级选项 ▾" : "高级选项 ▴";
});
$("fieldsBtn").addEventListener("click", () => {
  const panel = $("fieldsPanel");
  panel.hidden = !panel.hidden;
  $("fieldsBtn").textContent = panel.hidden
    ? "5. 抓取字段（默认按截图，可改） ▾"
    : "5. 抓取字段（默认按截图，可改） ▴";
});

loadConfig();
setInterval(async () => {
  try {
    const res = await fetch("/api/status");
    const st = await res.json();
    if (st.schedule) renderSchedule(st.schedule);
    if (st.align_schedule) renderSchedule(st.align_schedule, "alignSchedInfo", "alignSchedBtn");
    if (st.running && !timer) {
      timer = setInterval(poll, 700);
      poll();
    }
  } catch (e) {}
}, 4000);
