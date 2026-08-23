# 数据汇总工具

从多张 Google 表格按字段抓取贴文、按日期/点赞筛选，并支持按表头对齐同步。

## 环境

- Python 3.10+
- Google 服务账号 `credentials.json`（需对源表和目标表有编辑权限）

```bash
pip install -r requirements.txt
```

将服务账号 JSON 放到本目录 `credentials.json`，或在界面「高级选项」填写路径。

## 启动

双击 `启动界面.bat`，或：

```bash
python app.py
```

浏览器会打开 `http://127.0.0.1:8765`。

## 功能

- **数据汇总**：按配置列抓取，日期过滤，点赞阈值拆成全部/高赞两张表；默认按 B 列帖文 id 增量更新。
- **表头对齐同步**：只填源表链接和工作表名，按规范表头对齐拷贝，缺列留空。
- 两套功能各自支持定时同步（需保持本程序运行）。

## 打包发布

推送 `v*` 格式的 tag 后，GitHub Actions 会：

1. 使用 PyInstaller 构建 `data-summary-tool-windows.exe`；
2. 为最终 EXE 生成 GitHub Artifact Attestation；
3. 使用默认 `GITHUB_TOKEN` 创建 Release 并上传同一个 EXE。

请勿在 GitHub 网页中手工上传或替换 Release 产物，否则上传者和构件摘要将无法通过 L2 Attestation 校验。
