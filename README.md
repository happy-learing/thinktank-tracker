# 智库研究追踪

面向 Windows 10/11 x64 的本地 Web 应用。程序解析 Word 中的智库清单，采集研究栏目，执行确定性日期/体裁/篇幅过滤，调用 OpenAI 兼容模型进行主题判断，并在人工复核后导出 Word 报告和 Excel 审计表。

## 开发运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
.\.venv\Scripts\thinktank-tracker.exe
```

运行数据只写入项目或便携包旁边的 `workspace/`。另一台电脑使用时复制完整发布目录并重新录入 API 密钥；两台电脑的数据互不合并。

## 发布目录

```text
ThinkTankTracker/
  启动智库追踪.exe
  app/                 # PyInstaller 运行时与依赖
  runtime/browsers/    # 与 Playwright 版本匹配的 Chromium
  workspace/           # 数据库、配置、缓存、日志、导出、备份
  VERSION
  SHA256SUMS.txt
```

构建前运行 `scripts/install-browser.ps1`，再运行 `scripts/build-portable.ps1`。构建产物为 `onedir` 目录及完整 ZIP，不要求目标电脑安装 Python、Node、数据库或浏览器组件。
