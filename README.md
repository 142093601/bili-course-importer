# B站课程导入器（精简版 v3 · 纯 API 路线）

把 B 站课程批量变成结构化课时讲义：**view API 解析课程 → wbi 接口抓 AI 字幕 → 并发 DeepSeek 整理 → 自动检查 → 输出课时 Markdown**。全程不下载视频文件。

## 实测数据（2026-08，鹏哥C语言课，登录态）

| 环节 | 单节耗时 | 说明 |
|---|---|---|
| 解析课程结构 | 0.8s | 一次 view API 拿到全部分P（cid/时长/标题） |
| 抓 AI 字幕 | 2~3s/节 | wbi player 接口 + 字幕 CDN，登录态解锁 |
| DeepSeek 整理 | 5~15s/节 | 10~30 分钟课时实测 6~7s，越长越慢 |
| 渲染+检查 | <0.1s | 自动校验 front-matter / 正文字数 |

- **单节（30 分钟视频）全程约 10~20 秒**
- **整门 200 节课约 12~20 分钟全自动**（抓字幕串行约 8 分钟 + AI 并发 8 路约 3~4 分钟）
- 实测 4 节课（共 78 分钟视频）全程 **17.8 秒**

## 快速开始

```bash
pip install -r requirements.txt
python importer.py "https://www.bilibili.com/video/BVxxxx" --cookies cookies.txt
python importer.py "https://www.bilibili.com/video/BVxxxx" --cookies cookies.txt --pages 5   # 只处理前5集
```

## 关键前提：登录 cookie（必须）

**B 站 AI 字幕需要登录态**，匿名接口返回空。用浏览器扩展 **Get cookies.txt LOCALLY** 导出（会导出所有网站 cookie，脚本会自动过滤出 bilibili 域名并规范格式），保存为 `cookies.txt` 放本目录。

无字幕的视频（约 20%）可用 `--transcribe` 走 faster-whisper 转录兜底。

## 常用参数

| 参数 | 说明 |
|---|---|
| `--cookies PATH` | cookies.txt 路径（默认当前目录 cookies.txt） |
| `--pages N` | 只解析前 N 个分P（200 集大合集必用） |
| `--limit N` | 只处理前 N 节 |
| `--concurrency N` | AI 并发数（默认 8，纯 API 调用可开更大） |
| `--transcribe` | 无字幕时用 faster-whisper 转录兜底 |
| `--no-ai` | 跳过 AI 整理，字幕清洗后直接入库（最快） |
| `--course-name 名称` | 输出目录名（默认 bilibili-course） |

## API Key

按优先级读取：环境变量 `DEEPSEEK_API_KEY` → `tutor-app-main/.env` 的 `API_KEY`。

## 输出结构

```
output/<课程名>/
├── 01-课时标题.md     # YAML front-matter(来源/时长/状态) + 概述/知识点/要点/示例/小测验
└── 02-...
```

产出为 `draft` 状态，入库前建议人工抽查（重点看代码、公式、术语）。

## 技术要点（踩坑记录）

- 字幕 CDN（hdslb.com）**拒绝带 Referer/Cookie 的请求**（HTTP 400），只能带 UA
- **多语言 AI 字幕轨按「语言优先序（中文在前）」选，同语言内才比长度** —— 不能只取最长文本：
  B 站多语言视频常给 8 条轨，翻译轨往往比中文原稿长得多（实测 `BV1hkYc6uEg6`：中文 2508 字 vs 西语 12567 字，
  只比长度会把中文视频读成西语稿）。产出里会把选中的轨道标出来，如 `AI字幕(ai-zh)`
- 多 P 视频的 cid 必须从 view API 拿（yt-dlp 分 P 条目不返回 cid）
- cookies.txt 若含全站 cookie，需过滤出 bilibili 域名并规范 Netscape 格式（带点域名要求 include_subdomains=TRUE）
- 版权提醒：整理成个人学习笔记没问题；对外分发/商用前请确认 UP 主授权或选择开放许可内容
