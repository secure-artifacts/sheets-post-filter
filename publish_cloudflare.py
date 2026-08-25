# -*- coding: utf-8 -*-
"""把汇总结果打成 q-gallery 分片 JSON，直推 Cloudflare R2（替代 Apps Script → Drive）。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fetch_posts import load_sync_state, remember_cf_fingerprint, to_datetime, with_retry

LogFn = Callable[[str], None]

# 汇总字段名 → 前端 asset 字段
HEADER_TO_ASSET = {
    "名字": "name",
    "帖文id": "postId",
    "FB链接": "postLink",
    "引流": "lead",
    "缩略图链接": "thumbnailUrl",
    "帖文内容": "content",
    "改贴": "rewrite",
    "发布日期": "date",
    "图片类型": "category",
    "点赞": "likes",
    "评论": "comments",
    "分享": "shares",
    "帖文类型": "postType",
    "OCR": "ocr",
    "OCR翻译": "ocrTranslation",
    "音频文本": "audioText",
    "音频文本翻译": "audioTranslation",
    "专页id": "maker",
    "小组": "group",
    "美工": "designer",
    "水滴": "water",
    "水滴时间": "waterTime",
    "来源类型": "sourceType",
    "来源渠道": "sourceChannel",
}

# 表头别名 → 规范字段名（图片分析 可能没有中文表头）
HEADER_ALIASES = {
    "名字": "名字",
    "名称": "名字",
    "name": "名字",
    "帖文id": "帖文id",
    "帖子id": "帖文id",
    "postid": "帖文id",
    "fb链接": "FB链接",
    "帖文链接": "FB链接",
    "链接": "FB链接",
    "postlink": "FB链接",
    "引流": "引流",
    "lead": "引流",
    "缩略图链接": "缩略图链接",
    "缩略图": "缩略图链接",
    "图片": "缩略图链接",
    "图片链接": "缩略图链接",
    "thumbnailurl": "缩略图链接",
    "帖文内容": "帖文内容",
    "文案": "帖文内容",
    "content": "帖文内容",
    "改贴": "改贴",
    "改写": "改贴",
    "rewrite": "改贴",
    "发布日期": "发布日期",
    "日期": "发布日期",
    "date": "发布日期",
    "图片类型": "图片类型",
    "分类": "图片类型",
    "category": "图片类型",
    "点赞": "点赞",
    "likes": "点赞",
    "评论": "评论",
    "comments": "评论",
    "分享": "分享",
    "shares": "分享",
    "帖文类型": "帖文类型",
    "posttype": "帖文类型",
    "ocr": "OCR",
    "ocr翻译": "OCR翻译",
    "ocrtranslation": "OCR翻译",
    "音频文本": "音频文本",
    "audiotext": "音频文本",
    "音频文本翻译": "音频文本翻译",
    "音频翻译": "音频文本翻译",
    "专页id": "专页id",
    "作者": "专页id",
    "作者id": "专页id",
    "authorname": "专页id",
    "小组": "小组",
    "group": "小组",
    "美工": "美工",
    "美工名字": "美工",
    "designer": "美工",
    "水滴": "水滴",
    "water": "水滴",
    "水滴时间": "水滴时间",
    "watertime": "水滴时间",
    "头像": "头像",
    "avatarurl": "头像",
    "备用图片": "备用图片",
    "页面": "页面",
    "管理员专页": "名字",
    "管理员": "名字",
    "改贴/原创": "改贴",
    "发帖日期": "发布日期",
    "文本翻译": "音频文本翻译",
    "来源类型": "来源类型",
    "sourcetype": "来源类型",
    "来源渠道": "来源渠道",
    "sourcechannel": "来源渠道",
}

# 与 Apps Script mapRow_ / 本工具写入顺序一致：A=名字 … R=专页id，AC=小组
POSITIONAL = {
    "名字": 0,
    "帖文id": 1,
    "FB链接": 2,
    "引流": 3,
    "缩略图链接": 4,
    "帖文内容": 5,
    "改贴": 6,
    "发布日期": 7,
    "图片类型": 8,
    "点赞": 9,
    "评论": 10,
    "分享": 11,
    "帖文类型": 12,
    "OCR": 13,
    "OCR翻译": 14,
    "音频文本": 15,
    "音频文本翻译": 16,
    "专页id": 17,
    "小组": 18,
    "头像": 19,
    "来源类型": 20,
    "来源渠道": 21,
    "备用图片": 22,
    "页面": 24,
    "水滴": 25,
    "水滴时间": 26,
    "美工": 27,
}
GROUP_COL_AC = 28  # 本工具把「小组」写到 AC
URL_FIELDS = {"FB链接", "缩略图链接", "备用图片", "头像"}
_NOISE_RE = re.compile(
    r"[\U0001F300-\U0010ffff]|[\u2600-\u27bf]|[\ufe0e\ufe0f]|[|·•🎨📜]"
)

_FORMULA_URL_RE = re.compile(
    r"""(?:HYPERLINK|IMAGE)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"')>,]+", re.IGNORECASE)


def _norm_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = _NOISE_RE.sub("", text)
    return text.replace(" ", "").replace("_", "")


def _canonical_header(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in HEADER_TO_ASSET:
        return raw
    key = _norm_header(raw)
    return HEADER_ALIASES.get(raw) or HEADER_ALIASES.get(key) or raw


def _looks_like_header(row: list[Any] | None) -> bool:
    if not row:
        return False
    hits = 0
    for cell in row[:32]:
        raw = str(cell or "").strip()
        if not raw:
            continue
        if _canonical_header(raw) in HEADER_TO_ASSET or _norm_header(raw) in HEADER_ALIASES:
            hits += 1
            if hits >= 2:
                return True
    return False


def split_sheet_for_publish(
    values: list[list[Any]],
    start_row: int,
    include_headers: bool,
) -> tuple[list[str], list[list[Any]], str, int]:
    """起始行可能是表头，也可能直接是数据（图片分析从第 3 行起、不写表头）。"""
    start = max(1, int(start_row or 1))
    if len(values) < start:
        raise RuntimeError("目标表没有数据可发布")
    head = values[start - 1]
    if _looks_like_header(head):
        headers = [str(x).strip() for x in head]
        return headers, values[start:], f"第 {start} 行表头", start + 1
    if include_headers:
        headers = [str(x).strip() for x in head]
        return headers, values[start:], f"第 {start} 行当表头", start + 1
    for i in range(min(start - 1, 3)):
        if _looks_like_header(values[i]):
            headers = [str(x).strip() for x in values[i]]
            return headers, values[start - 1 :], f"第 {i + 1} 行表头，数据从第 {start} 行", start
    return [], values[start - 1 :], f"无表头，按列位置（数据从第 {start} 行）", start


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def extract_url_from_cell(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    m = _FORMULA_URL_RE.search(text)
    if m:
        return m.group(1).strip()
    if text.startswith("="):
        m = _URL_RE.search(text)
        return (m.group(0).rstrip(").,;]") if m else "").strip()
    if text.lower().startswith("http"):
        return text
    m = _URL_RE.search(text)
    return (m.group(0) if m else text).strip()


def _date_text(value: Any) -> str:
    dt = to_datetime(value)
    if not dt:
        text = _text(value)
        if text.isdigit():
            dt = to_datetime(int(text))
        elif len(text) >= 10 and text[4] in "-/":
            return text[:10].replace("/", "-")
    if dt:
        return dt.strftime("%Y-%m-%d")
    return _text(value)


def _cell(row: list[Any], idx: int | None) -> Any:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def _url_hits(rows: list[list[Any]], idx: int, limit: int = 40) -> int:
    hits = 0
    for row in rows[:limit]:
        text = str(_cell(row, idx) or "").lower()
        if "http" in text or text.strip().startswith("="):
            hits += 1
    return hits


def _header_index(headers: list[str], rows: list[list[Any]]) -> dict[str, int]:
    index: dict[str, int] = {}
    for i, h in enumerate(headers or []):
        key = _canonical_header(h)
        if not key:
            continue
        if key not in index:
            index[key] = i
            continue
        if key in URL_FIELDS and _url_hits(rows, i) > _url_hits(rows, index[key]):
            index[key] = i
    return index


def _first_http(*values: Any) -> str:
    for value in values:
        url = extract_url_from_cell(value)
        if url.lower().startswith("http"):
            return url
    return ""


def rows_to_assets(
    headers: list[str],
    rows: list[list[Any]],
    group_values: list[Any] | None = None,
    log: LogFn | None = None,
    start_row: int = 1,
) -> list[dict[str, Any]]:
    index = _header_index(headers, rows)
    named = sum(1 for k in HEADER_TO_ASSET if k in index)
    mode = f"表头命中 {named} 列" if named else "按 A=名字 / B=帖文id / C=链接 列位置"
    n = len(rows)
    if log:
        thumb_at = index.get("缩略图链接", POSITIONAL["缩略图链接"])
        log(f"正在转换 {n} 行（{mode}，缩略图列 {thumb_at + 1}）…")

    assets: list[dict[str, Any]] = []
    skipped = 0
    for r, row in enumerate(rows):
        if log and r and r % 10000 == 0:
            log(f"  已扫描 {r}/{n} 行，有效 {len(assets)} 条")

        def col(name: str) -> Any:
            i = index.get(name)
            if i is None:
                i = POSITIONAL.get(name)
            return _cell(row, i)

        name = _text(col("名字")) or _text(_cell(row, 0))
        post_id = _text(col("帖文id"))
        post_link = _first_http(col("FB链接"), _cell(row, 2), _cell(row, 18))
        thumb = _first_http(col("缩略图链接"), _cell(row, 4), _cell(row, 22))
        fallback = _first_http(col("备用图片"), _cell(row, 22), _cell(row, 4))
        post_type = _text(col("帖文类型"))
        category = _text(col("图片类型"))
        group = ""
        if group_values is not None and r < len(group_values):
            group = _text(group_values[r])
        if not group and "小组" in index:
            group = _text(col("小组"))
        if not group:
            group = _text(_cell(row, GROUP_COL_AC))
        author = _text(col("专页id"))
        date_s = _date_text(col("发布日期"))
        if not (name or post_id or post_link or thumb or fallback):
            skipped += 1
            continue
        preview = thumb or fallback
        sheet_row = int(start_row or 1) + r
        asset = {
            "id": f"{post_id or post_link or 'row'}-{sheet_row}",
            "rowNumber": sheet_row,
            "title": name or post_type or f"素材 {r + 1}",
            "name": name,
            "postId": post_id,
            "postLink": post_link,
            "lead": _text(col("引流")),
            "sourceUrl": post_link,
            "previewUrl": preview,
            "thumbnailUrl": preview,
            "thumbnailFallbackUrl": fallback or thumb,
            "viewUrl": post_link,
            "content": _text(col("帖文内容")),
            "rewrite": _text(col("改贴")),
            "date": date_s,
            "type": category or post_type,
            "category": category,
            "likes": _text(col("点赞")),
            "comments": _text(col("评论")),
            "shares": _text(col("分享")),
            "postType": post_type,
            "ocr": _text(col("OCR")),
            "ocrTranslation": _text(col("OCR翻译")),
            "audioText": _text(col("音频文本")),
            "audioTranslation": _text(col("音频文本翻译")),
            "maker": author,
            "authorName": author,
            "group": group,
            "sourceType": _text(col("来源类型")),
            "sourceChannel": _text(col("来源渠道")),
            "pageName": _text(col("页面")),
            "avatarUrl": extract_url_from_cell(col("头像")),
            "designer": _text(col("美工")),
            "water": _text(col("水滴")),
            "waterTime": _date_text(col("水滴时间")),
            "mediaMode": "video" if "视频" in (post_type + category) else "image",
        }
        assets.append(asset)
    if log:
        log(f"转换完成：有效 {len(assets)} 条，跳过空行 {skipped}")
    return assets


def overlay_formula_urls(
    ws,
    rows: list[list[Any]],
    start_row: int,
    log: LogFn = print,
) -> None:
    """IMAGE/HYPERLINK 单元格用 UNFORMATTED_VALUE 经常是空的，补读公式列。"""
    if not rows:
        return
    sample = rows[:120]

    def has_url(row: list[Any], idx: int) -> bool:
        if idx >= len(row):
            return False
        t = str(row[idx] or "")
        return "http" in t.lower() or t.strip().startswith("=")

    link_ok = sum(1 for r in sample if has_url(r, 2))
    thumb_ok = sum(1 for r in sample if has_url(r, 4))
    need_c = link_ok < max(8, len(sample) * 0.35)
    need_e = thumb_ok < max(8, len(sample) * 0.35)
    if not need_c and not need_e:
        return

    end = start_row + len(rows) - 1
    ranges: list[str] = []
    cols: list[int] = []
    if need_c:
        ranges.append(f"C{start_row}:C{end}")
        cols.append(2)
    if need_e:
        ranges.append(f"E{start_row}:E{end}")
        cols.append(4)
    ranges.extend([f"T{start_row}:T{end}", f"W{start_row}:W{end}"])
    cols.extend([19, 22])
    log("部分链接/图片单元格为空，正在补读公式列…")

    def _do():
        return ws.batch_get(ranges, value_render_option="FORMULA")

    batches = with_retry(_do, log=log, what="读取公式")
    filled = 0
    for col_vals, col_idx in zip(batches, cols):
        for i, cell_row in enumerate(col_vals):
            if i >= len(rows):
                break
            raw = cell_row[0] if cell_row else ""
            url = extract_url_from_cell(raw)
            if not url or not url.lower().startswith("http"):
                continue
            row = rows[i]
            while len(row) <= col_idx:
                row.append("")
            existing = str(row[col_idx] or "")
            if "http" not in existing.lower():
                row[col_idx] = url
                filled += 1
    log(f"从公式补到 {filled} 个链接/图片地址")


def _http_json(url: str, secret: str, payload: dict, log: LogFn) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _do():
        req = Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-publish-secret": secret,
                "Authorization": f"Bearer {secret}",
                "User-Agent": "Mozilla/5.0 sheets-post-filter",
            },
        )
        try:
            with urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"Cloudflare HTTP {e.code}: {body[:400]}") from e
        except URLError as e:
            raise RuntimeError(f"Cloudflare 连接失败: {e.reason}") from e

    return with_retry(_do, log=log, what="发布 Cloudflare")


def assets_fingerprint(assets: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(str(len(assets)).encode())
    for asset in assets:
        digest.update(b"\n")
        digest.update(str(asset.get("id") or "").encode())
        digest.update(b"|")
        digest.update(str(asset.get("likes") or "").encode())
        digest.update(b"|")
        digest.update(str(asset.get("date") or "").encode())
        digest.update(b"|")
        digest.update(str(asset.get("thumbnailUrl") or "").encode())
    return digest.hexdigest()[:32]


def publish_assets_to_cloudflare(
    cfg,
    headers: list[str],
    rows: list[list[Any]],
    group_values: list[Any] | None = None,
    log: LogFn = print,
    start_row: int = 1,
    skip_if_unchanged: bool = True,
) -> dict[str, Any]:
    url = (getattr(cfg, "cf_publish_url", "") or "").strip()
    secret = (getattr(cfg, "cf_publish_secret", "") or "").strip()
    if not url or not secret:
        raise RuntimeError("请先填写 Cloudflare 发布地址和 CACHE_PUBLISH_SECRET")

    assets = rows_to_assets(
        headers,
        rows,
        group_values,
        log=log,
        start_row=max(1, int(start_row or 1)),
    )
    if not assets:
        sample = " | ".join(str(x) for x in (headers or [])[:12] if str(x).strip())
        hint = sample or "（起始行不是表头，已按列位置读取仍为空）"
        raise RuntimeError(f"没有可发布的记录（名字/帖文id/链接都为空）。表头样例：{hint}")

    fingerprint = assets_fingerprint(assets)
    prev = str((load_sync_state() or {}).get("cf_fingerprint") or "")
    if skip_if_unchanged and prev and prev == fingerprint:
        log(f"数据和上次发布相同（{len(assets)} 条），跳过 Cloudflare，不占配额")
        return {
            "ok": True,
            "skipped": True,
            "totalRows": len(assets),
            "chunkCount": 0,
            "publicManifestUrl": "",
            "maxDate": "",
        }

    chunk_size = max(200, int(getattr(cfg, "cf_chunk_size", 800) or 800))
    build_id = str(uuid.uuid4())
    chunks = [assets[i : i + chunk_size] for i in range(0, len(assets), chunk_size)]
    max_date = ""
    for a in assets:
        d = str(a.get("date") or "")
        if d > max_date:
            max_date = d

    log(f"准备发布 Cloudflare：{len(assets)} 条，{len(chunks)} 个分片")
    for i, chunk in enumerate(chunks, 1):
        log(f"  正在上传分片 {i}/{len(chunks)}（{len(chunk)} 条）…")
        result = _http_json(
            url,
            secret,
            {
                "action": "put-chunk",
                "buildId": build_id,
                "index": i,
                "totalRows": len(assets),
                "maxDate": max_date,
                "assets": chunk,
            },
            log,
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or f"分片 {i} 发布失败")
        log(f"  已上传分片 {i}/{len(chunks)}")

    log("正在 finalize…")
    final = _http_json(
        url,
        secret,
        {
            "action": "finalize",
            "buildId": build_id,
            "totalRows": len(assets),
            "chunkCount": len(chunks),
            "maxDate": max_date,
        },
        log,
    )
    if not final.get("ok"):
        raise RuntimeError(final.get("error") or "finalize 失败")
    log(f"Cloudflare 发布完成：{final.get('publicManifestUrl') or url}")
    remember_cf_fingerprint(fingerprint, len(assets))
    return {
        "ok": True,
        "buildId": build_id,
        "totalRows": len(assets),
        "chunkCount": len(chunks),
        "publicManifestUrl": final.get("publicManifestUrl") or "",
        "maxDate": max_date,
    }
