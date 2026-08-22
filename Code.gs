/**
 * 贴文筛选（替代 LET + 18 次 IMPORTRANGE + FILTER + SORT）
 *
 * 用法：
 *  1. 打开「含数据库 sheet」的表格 → 扩展程序 → Apps Script
 *  2. 把本文件内容粘贴进去，保存
 *  3. 刷新表格，菜单「贴文筛选」→「按日期拉取数据」
 *  4. 在「筛选结果」A1 填开始日期、B1 填结束日期
 *
 * 逻辑与原公式一致：
 *  发布日期 >= A1  且  发布日期 <= B1+1  且  帖文id <> "未找到"
 *  再按「点赞」降序
 */

var CONFIG = {
  databaseSheet: "数据库",
  outputSheet: "筛选结果",
  dateSheet: "筛选结果",
  dateStartCell: "A1",
  dateEndCell: "B1",
  outputStartRow: 3,
  includeHeaders: true,
  excludeIdValue: "未找到",
  dateField: "发布日期",
  idField: "帖文id",
  sortField: "点赞",
  dateCol1Based: 8,
  idCol1Based: 2,
  sortCol1Based: 10
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("贴文筛选")
    .addItem("按日期拉取数据", "fetchAndFilterPosts")
    .addToUi();
}

function fetchAndFilterPosts() {
  var ss = SpreadsheetApp.getActive();
  var dbSheet = ss.getSheetByName(CONFIG.databaseSheet);
  if (!dbSheet) {
    throw new Error("找不到工作表「" + CONFIG.databaseSheet + "」");
  }

  var db = readDatabaseSheet_(dbSheet);
  var dateIdx = findFieldIndex_(db.fields, CONFIG.dateField, CONFIG.dateCol1Based);
  var idIdx = findFieldIndex_(db.fields, CONFIG.idField, CONFIG.idCol1Based);
  var sortIdx = findFieldIndex_(db.fields, CONFIG.sortField, CONFIG.sortCol1Based);

  var dateSheet = ss.getSheetByName(CONFIG.dateSheet) || getOrCreateSheet_(ss, CONFIG.outputSheet);
  var start = dateSheet.getRange(CONFIG.dateStartCell).getValue();
  var end = dateSheet.getRange(CONFIG.dateEndCell).getValue();
  if (!(start instanceof Date) || !(end instanceof Date)) {
    throw new Error(
      "请在「" + CONFIG.dateSheet + "」的 " +
      CONFIG.dateStartCell + " / " + CONFIG.dateEndCell + " 填入起止日期"
    );
  }
  var endLimit = new Date(end.getTime() + 24 * 60 * 60 * 1000);

  ss.toast("正在读取数据源…", "贴文筛选", 5);
  var sourceSs = SpreadsheetApp.openById(db.sourceId);
  var rows = buildDatasource_(sourceSs, db.fields);

  var kept = [];
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var idVal = String(row[idIdx] == null ? "" : row[idIdx]);
    if (idVal === CONFIG.excludeIdValue) continue;
    var d = toDate_(row[dateIdx]);
    if (!d) continue;
    if (d < start || d > endLimit) continue;
    kept.push(row);
  }

  kept.sort(function (a, b) {
    return toNumber_(b[sortIdx]) - toNumber_(a[sortIdx]);
  });

  writeOutput_(ss, db.fields, kept);
  ss.toast("完成，写入 " + kept.length + " 行", "贴文筛选", 8);
}

function readDatabaseSheet_(sheet) {
  var values = sheet.getDataRange().getDisplayValues();
  var sourceId = "";
  var limit = Math.min(5, values.length);
  for (var i = 0; i < limit; i++) {
    var id = extractId_(values[i][0] || "");
    if (id) {
      sourceId = id;
      break;
    }
  }
  if (!sourceId) {
    throw new Error("在「数据库」A 列前几行找不到数据源表格链接 / ID");
  }

  var fields = [];
  for (var r = 0; r < values.length; r++) {
    var name = String(values[r][0] || "").trim();
    var sheetName = String(values[r][1] || "").trim();
    var colRange = String(values[r][2] || "").trim();
    var fullRange = String(values[r][3] || "").trim();
    if (!name || extractId_(name)) continue;
    var spec = fullRange || (sheetName && colRange ? sheetName + "!" + colRange : colRange);
    var parsed = parseRange_(spec, sheetName);
    if (!parsed) continue;
    fields.push({
      name: name,
      sheet: parsed.sheet,
      col: parsed.col,
      startRow: parsed.startRow
    });
  }
  if (!fields.length) {
    throw new Error("「数据库」里没有解析到任何字段范围");
  }
  return { sourceId: sourceId, fields: fields };
}

function buildDatasource_(sourceSs, fields) {
  var cache = {};
  var columns = [];
  var maxLen = 0;

  for (var i = 0; i < fields.length; i++) {
    var f = fields[i];
    if (!cache[f.sheet]) {
      var ws = sourceSs.getSheetByName(f.sheet);
      if (!ws) throw new Error("数据源找不到工作表「" + f.sheet + "」");
      cache[f.sheet] = ws.getDataRange().getValues();
    }
    var all = cache[f.sheet];
    var colIdx = colLetterToIndex_(f.col);
    var start = f.startRow - 1;
    var col = [];
    for (var r = start; r < all.length; r++) {
      col.push(all[r][colIdx] == null ? "" : all[r][colIdx]);
    }
    columns.push(col);
    if (col.length > maxLen) maxLen = col.length;
  }

  var last = -1;
  for (var r2 = 0; r2 < maxLen; r2++) {
    for (var c = 0; c < columns.length; c++) {
      if (r2 < columns[c].length && columns[c][r2] !== "" && columns[c][r2] != null) {
        last = r2;
        break;
      }
    }
  }
  maxLen = last + 1;

  var rows = [];
  for (var r3 = 0; r3 < maxLen; r3++) {
    var row = [];
    for (var c2 = 0; c2 < columns.length; c2++) {
      row.push(r3 < columns[c2].length ? columns[c2][r3] : "");
    }
    rows.push(row);
  }
  return rows;
}

function writeOutput_(ss, fields, rows) {
  var ws = getOrCreateSheet_(ss, CONFIG.outputSheet);
  var headers = fields.map(function (f) { return f.name; });
  var startRow = CONFIG.outputStartRow;
  var lastRow = Math.max(ws.getLastRow(), startRow);
  var lastCol = Math.max(headers.length, 1);
  if (lastRow >= startRow) {
    ws.getRange(startRow, 1, lastRow - startRow + 1, lastCol).clearContent();
  }

  var payload = [];
  if (CONFIG.includeHeaders) payload.push(headers);
  for (var i = 0; i < rows.length; i++) payload.push(rows[i]);
  if (!payload.length) return;

  var neededRows = startRow + payload.length - 1;
  var maxRows = ws.getMaxRows();
  var maxCols = ws.getMaxColumns();
  if (neededRows > maxRows) ws.insertRowsAfter(maxRows, neededRows - maxRows + 10);
  if (lastCol > maxCols) ws.insertColumnsAfter(maxCols, lastCol - maxCols);

  ws.getRange(startRow, 1, payload.length, lastCol).setValues(payload);
}

function getOrCreateSheet_(ss, name) {
  var ws = ss.getSheetByName(name);
  if (ws) return ws;
  return ss.insertSheet(name);
}

function findFieldIndex_(fields, name, fallback1Based) {
  for (var i = 0; i < fields.length; i++) {
    if (fields[i].name === name) return i;
  }
  var idx = fallback1Based - 1;
  if (idx < 0 || idx >= fields.length) {
    throw new Error("找不到字段「" + name + "」");
  }
  return idx;
}

function extractId_(text) {
  var s = String(text || "");
  var m = s.match(/\/spreadsheets\/d\/([a-zA-Z0-9-_]+)/);
  if (m) return m[1];
  if (/^[a-zA-Z0-9-_]{20,}$/.test(s.trim())) return s.trim();
  return "";
}

function parseRange_(spec, defaultSheet) {
  var text = String(spec || "").trim().replace(/^"+|"+$/g, "");
  if (!text) return null;
  var sheet = defaultSheet;
  var cellPart = text;
  var bang = text.lastIndexOf("!");
  if (bang >= 0) {
    sheet = text.substring(0, bang).replace(/^'+|'+$/g, "");
    cellPart = text.substring(bang + 1);
  }
  var start = cellPart.split(":")[0].trim();
  var m = start.match(/^([A-Za-z]+)(\d+)$/);
  if (!m || !sheet) return null;
  return { sheet: sheet, col: m[1].toUpperCase(), startRow: parseInt(m[2], 10) };
}

function colLetterToIndex_(letter) {
  var n = 0;
  var s = String(letter).toUpperCase();
  for (var i = 0; i < s.length; i++) {
    n = n * 26 + (s.charCodeAt(i) - 64);
  }
  return n - 1;
}

function toDate_(value) {
  if (value instanceof Date && !isNaN(value.getTime())) return value;
  if (typeof value === "number" && value > 20000) {
    return new Date(Date.UTC(1899, 11, 30) + value * 86400000);
  }
  return null;
}

function toNumber_(value) {
  if (typeof value === "number") return value;
  var n = Number(String(value == null ? "" : value).replace(/,/g, ""));
  return isNaN(n) ? Number.NEGATIVE_INFINITY : n;
}
