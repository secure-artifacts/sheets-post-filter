# 数据汇总工具

当前版本：v1.1.1。Release 同时提供安装版 `setup.exe` 和免安装便携版 ZIP。

从多张 Google 表格按字段抓取贴文、按日期/点赞筛选，并支持按表头对齐同步。

## 环境

- Python 3.10+
- Google 服务账号 `credentials.json`（需对源表和目标表有编辑权限）

```bash
pip install -r requirements.txt
```

在软件顶部点击「选择服务账号」，可从电脑任意位置选择 Google 服务账号 JSON 文件。

## 启动

**本地软件（推荐）：** 双击桌面「数据汇总工具」，或 `dist\数据汇总工具\数据汇总工具.exe`。重新打包：`pack.bat`。

源码启动：双击 `启动界面.bat`，或：

```bash
python app.py
```

浏览器会打开 `http://127.0.0.1:8765`。每天打开一次即可；若距上次同步已超过间隔，打开后约 12 秒会自动跑一轮。谷歌表格里的 Apps Script 定时器可以停掉。

## 功能

- **贴文筛选汇总**：按配置列抓取，日期过滤，点赞阈值拆成全部/高赞两张表；默认按 B 列帖文 id 增量更新。
- **表头对齐同步**：只填源表链接和工作表名，按规范表头对齐拷贝，缺列留空。
- 两套功能各自支持定时同步（需保持本程序运行）。

## 发布到 Cloudflare

原来由 Google Apps Script 导出 JSON 再推 CDN。现在可以在本工具里直推：

1. 部署站点（只需一次，更新 Worker 后才能直推分片）：

```bash
cd promo-site
npx wrangler pages deploy . --project-name=q-gallery-promo --commit-dirty=true --branch=production
```

2. 在界面第 4 步填写 `CACHE_PUBLISH_SECRET`，点 **立即发布到 Cloudflare**，或勾选「汇总后自动发布」。

站点：https://q-gallery-promo.pages.dev/  （promo.zhixianglife.com）

## 审核类型

本仓库为本地 Python 脚本（无独立构建产物），按平台规范以 **源码审核** 提交。
