#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站课程导入器（精简版 v3 · 纯 API 路线）
流程：view API 解析课程结构 → wbi player API 批量抓字幕（登录 cookie 解锁 AI 字幕）
     → 无字幕时可选 faster-whisper 转录兜底 → 并发 DeepSeek 整理 → 自动检查 → 输出课时。

用法：
    python importer.py <bilibili-url> [--cookies cookies.txt] [--pages N] [--limit N]
                      [--concurrency 8] [--transcribe] [--no-ai] [--out DIR] [--course-name 名称]

API Key 来源（按优先级）：
    1. 环境变量 DEEPSEEK_API_KEY
    2. tutor-app-main/.env 里的 API_KEY
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
REFERER = "https://www.bilibili.com/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
MAX_AI_INPUT_CHARS = 40000

_WBI_TABLE = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52]

SYSTEM_PROMPT = """你是课程讲义整理专家。用户会给你一段从B站视频提取的字幕原文（可能含识别错字），
以及视频标题和时长。你的任务是把这段课程内容整理成结构化讲义。

要求：
1. 这是编程/技术类课程的概率很高，请结合上下文修正字幕中的术语、代码、英文错字（如 "Python" 被识别成 "拍森"）。
2. 只整理字幕里确实讲到的内容，不要编造、不要补充字幕没有的知识。
3. 小测验只出【本节视频实际讲到的】知识点题目（具体语法、概念辨析、代码输出/补全等），
   **禁止出泛泛的历史/常识/百科题**（如"XX语言是谁发明的"这类视频里没教、靠外部常识才能答的题）。
4. 若字幕过短（少于50字）或明显是乱码/片头，返回 {"error": "字幕内容过短或无效"}。

严格输出 JSON（不要输出任何其他文字），字段：
{
  "title": "整理后的课时标题（比原标题更贴近内容）",
  "summary": "150字以内的内容概述",
  "knowledge_points": [{"title": "知识点名", "desc": "2-4句讲解"}],
  "key_points": ["要点1", "要点2"],
  "examples": ["代码或示例说明，若是代码用 ``` 代码块"],
  "quiz": [{"question": "单选题题干", "options": ["A", "B", "C", "D"], "answer_index": 0, "explanation": "解析"}]
}
quiz 出 1-3 题，答案索引从 0 开始。题目必须直接出自本节字幕内容且能从中推出答案，
出不了就用代码补全或概念辨析题，宁缺毋滥（可只出 1 题）。"""

COURSE_STRUCT_PROMPT = """你是课程结构规划专家。用户会给你一门课程的全部分P标题和时长列表（按播放顺序）。
请把分P按语义归并成章，并为整门课生成总览、前置关系与学习顺序建议。

严格输出 JSON（不要输出任何其他文字），字段：
{
  "title": "课程名（比原标题更规范）",
  "overview_md": "200字以内的课程总览，面向学生，说明这门课学什么、学完能做什么",
  "chapters": [
    {"title": "章节名", "lesson_titles": ["分P标题", "..."]}
  ],
  "prerequisites": [
    {"lesson_title": "某节课标题", "requires": ["它依赖的课标题"]}
  ],
  "suggested_order": "学习顺序说明（如：按章节顺序；第X章需先于第Y章）"
}
要求：
- lesson_titles 必须与输入的分P标题【逐字一致】，不要改写
- 每节课必须且只能归入一个章节
- 章节 3-10 章为宜，按学习递进排列
- prerequisites 只列存在明显依赖关系的课（1-5 条即可），没有可给空数组"""


def emit_progress(stage, done, total, message):
    """进度事件（stdout JSON 行，供桌面 App 的导入向导解析，§6.6 协议）"""
    print(json.dumps(
        {"type": "stage", "stage": stage, "done": done, "total": total, "message": message},
        ensure_ascii=False,
    ), flush=True)


def ai_course_structure(lessons, api_key):
    """课程级结构化（§7.3）：一次 AI 调用生成课程总览/章节划分/前置关系/学习顺序。"""
    listing = "\n".join(f"{i+1}. {l['title']}（{l['duration']}秒）" for i, l in enumerate(lessons))
    user = f"课程分P清单（按播放顺序）：\n{listing}"
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": COURSE_STRUCT_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 2500,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(2):
        try:
            r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=300)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", content, re.S)
                if m:
                    return json.loads(m.group(0))
                raise
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"课程级结构化失败: {last_err}")


def get_api_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    for p in [Path("tutor-app-main/.env"), Path("../tutor-app-main/.env"), Path("D:/project/tutor-app-main/.env")]:
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def load_cookies(path):
    """解析 Netscape 格式 cookies.txt → 'name=value; ...' 字符串。"""
    if not path or not Path(path).exists():
        return ""
    pairs = []
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                pairs.append(f"{parts[5]}={parts[6]}")
    except OSError:
        pass
    return "; ".join(pairs)


def bili_headers(cookie_str=""):
    h = {"User-Agent": UA, "Referer": REFERER}
    if cookie_str:
        h["Cookie"] = cookie_str
    return h


# ---------------- wbi 签名（懒缓存） ----------------
_wbi_key = None

def _get_wbi_key(cookie_str):
    global _wbi_key
    if _wbi_key:
        return _wbi_key
    r = requests.get("https://api.bilibili.com/x/web-interface/nav",
                     headers=bili_headers(cookie_str), timeout=15)
    r.raise_for_status()
    wbi = (r.json().get("data") or {}).get("wbi_img") or {}
    img = wbi.get("img_url", "").split("/")[-1].split(".")[0]
    sub = wbi.get("sub_url", "").split("/")[-1].split(".")[0]
    _wbi_key = "".join((img + sub)[i] for i in _WBI_TABLE)[:32]
    return _wbi_key


def wbi_get(path, params, cookie_str):
    params = dict(sorted({**params, "wts": int(time.time())}.items()))
    qs = urllib.parse.urlencode(params)
    mk = _get_wbi_key(cookie_str)
    url = f"https://api.bilibili.com{path}?" + qs + "&w_rid=" + hashlib.md5((qs + mk).encode()).hexdigest()
    return requests.get(url, headers=bili_headers(cookie_str), timeout=15).json()


# ---------------- 阶段 A：解析课程结构（view API） ----------------
def extract_lessons(url, cookie_str, pages=0):
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        raise SystemExit(f"无法从 URL 解析 BV 号: {url}")
    bvid = m.group(1)
    r = requests.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                     headers=bili_headers(cookie_str), timeout=15)
    r.raise_for_status()
    data = r.json()["data"]
    pgs = data["pages"]
    if pages:
        pgs = pgs[:pages]
    lessons = []
    for p in pgs:
        lessons.append({
            "bvid": bvid,
            "cid": p["cid"],
            "title": p["part"] or data["title"],
            "duration": p["duration"],
            "url": f"https://www.bilibili.com/video/{bvid}?p={p['page']}",
        })
    return lessons


# ---------------- 阶段 B：抓字幕（wbi player API） ----------------
def parse_bili_subtitle(data):
    body = data.get("body")
    if isinstance(body, list) and body:
        lines = [seg.get("content", "") for seg in body if seg.get("content")]
        if lines:
            return "\n".join(lines)
    events = data.get("events")
    if isinstance(events, list) and events:
        lines = []
        for ev in events:
            for seg in ev.get("segs", []):
                t = seg.get("utf8", "")
                if t:
                    lines.append(t)
        if lines:
            return "\n".join(lines)
    return None


def fetch_subtitle(lesson, cookie_str):
    """wbi 签名后请求 player 接口取字幕（登录态下 AI 字幕可用）。"""
    try:
        d = wbi_get("/x/player/wbi/v2", {"bvid": lesson["bvid"], "cid": lesson["cid"], "fnval": "16"}, cookie_str)
        subs = (d.get("data") or {}).get("subtitle", {}).get("subtitles") or []
        best, best_src = None, ""
        for s in subs:
            if not s.get("subtitle_url"):
                continue
            u = s["subtitle_url"]
            if u.startswith("//"):
                u = "https:" + u
            # 注意：字幕 CDN 拒绝带 Referer/Cookie 的请求(HTTP 400)，只能带 UA
            r = requests.get(u, headers={"User-Agent": UA}, timeout=20)
            r.raise_for_status()
            text = parse_bili_subtitle(r.json())
            if text and (best is None or len(text) > len(best)):
                best = text
                best_src = "AI字幕" if ("ai_subtitle" in u or "ai_" in s.get("lan", "")) else "CC字幕"
        if best:
            return best, best_src
        return None, "无字幕(需转录兜底)"
    except Exception as e:
        return None, f"字幕接口失败({type(e).__name__})"


# ---------------- 阶段 B2：faster-whisper 转录兜底 ----------------
def transcribe(lesson, cookiefile):
    """yt-dlp 下载音频 → faster-whisper 转录。返回 (文本, 来源)。"""
    import tempfile
    try:
        import yt_dlp
        from faster_whisper import WhisperModel
    except ImportError:
        return None, "未安装 faster-whisper/yt-dlp(pip install faster-whisper yt-dlp)"
    tmpdir = Path(tempfile.mkdtemp(prefix="bili_audio_"))
    opts = {"quiet": True, "format": "bestaudio/best", "outtmpl": str(tmpdir / "audio.%(ext)s")}
    if cookiefile:
        opts["cookiefile"] = cookiefile
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([lesson["url"]])
    except Exception as e:
        return None, f"音频下载失败({type(e).__name__})"
    candidates = list(tmpdir.glob("audio.*"))
    if not candidates:
        return None, "音频文件未生成"
    model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(candidates[0]), language="zh", vad_filter=True)
    lines = [seg.text.strip() for seg in segments if seg.text.strip()]
    text = "\n".join(lines)
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass
    if len(text) < 50:
        return None, "转录文本过短"
    return text, "转录(faster-whisper)"


# ---------------- 阶段 C：DeepSeek 整理 ----------------
def ai_structure(lesson, subtitle_text, api_key):
    user = (f"视频标题：{lesson['title']}\n"
            f"时长：{lesson['duration']}秒\n\n"
            f"字幕原文：\n{subtitle_text[:MAX_AI_INPUT_CHARS]}")
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 3500,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(2):
        try:
            r = requests.post(DEEPSEEK_URL, headers=headers, json=payload, timeout=300)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", content, re.S)
                if m:
                    return json.loads(m.group(0))
                raise
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"DeepSeek 调用失败: {last_err}")


# ---------------- 阶段 D：渲染 + 自动检查 ----------------
def slugify(s, maxlen=30):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "lesson"


def render_lesson(lesson, sub_text, structured, ai_mode):
    dur_min = round(lesson["duration"] / 60, 1)
    fm = {
        "title": structured.get("title", lesson["title"]) if structured else lesson["title"],
        "source": {
            "bvid": lesson["bvid"],
            "url": lesson["url"],
            "duration_min": dur_min,
            "subtitle_chars": len(sub_text or ""),
        },
        "status": "draft",
        "mode": "ai" if ai_mode else "raw",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    fm_yaml = "---\n" + json.dumps(fm, ensure_ascii=False, indent=2) + "\n---\n"
    if not ai_mode or structured is None:
        return fm_yaml + f"# {lesson['title']}\n\n> 来源：[{lesson['url']}]({lesson['url']}) · 时长 {dur_min} 分钟 · 原始字幕\n\n" + (sub_text or "")

    body = [f"# {structured['title']}"]
    body.append(f"\n> 来源：[{lesson['url']}]({lesson['url']}) · 时长 {dur_min} 分钟")
    body.append(f"\n## 概述\n\n{structured.get('summary', '')}")
    body.append("\n## 知识点\n")
    for kp in structured.get("knowledge_points", []):
        body.append(f"### {kp.get('title', '')}\n\n{kp.get('desc', '')}\n")
    body.append("## 要点\n")
    for kp in structured.get("key_points", []):
        body.append(f"- {kp}")
    if structured.get("examples"):
        body.append("\n## 示例\n")
        for ex in structured["examples"]:
            body.append(ex + "\n")
    body.append("\n## 小测验\n")
    for i, q in enumerate(structured.get("quiz", []), 1):
        body.append(f"**{i}. {q.get('question', '')}**\n")
        for j, opt in enumerate(q.get("options", [])):
            # 去掉 AI 可能已带的前缀（"A. xxx" / "A、xxx"），避免双前缀
            opt = re.sub(r"^[A-D][.、)\s]+", "", str(opt))
            body.append(f"{chr(65 + j)}. {opt}")
        ans = q.get("answer_index")
        body.append(f"\n<details><summary>答案</summary>\n\n{chr(65 + ans) if isinstance(ans, int) else ''} · {q.get('explanation', '')}\n</details>\n")
    return fm_yaml + "\n".join(body)


def auto_check(path):
    errors = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [f"无法读取: {e}"]
    if not text.startswith("---"):
        errors.append("缺少 YAML front-matter")
    else:
        end = text.find("\n---", 4)
        if end == -1:
            errors.append("front-matter 未闭合")
        else:
            fm = text[4:end]
            for field in ["title", "source", "status"]:
                if f'"{field}"' not in fm and f"'{field}'" not in fm:
                    errors.append(f"front-matter 缺字段: {field}")
    if len(text) < 200:
        errors.append(f"正文过短({len(text)}字)")
    return errors


def fmt_sec(s):
    return f"{s:.1f}s" if s < 60 else f"{s / 60:.1f}min"


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser(description="B站课程导入器：抓字幕/转录 → 并发AI整理 → 自动检查 → 输出课时")
    ap.add_argument("url", help="B站视频或合集/分P链接")
    ap.add_argument("--cookies", default="cookies.txt", help="Netscape 格式 cookies.txt（默认当前目录 cookies.txt）")
    ap.add_argument("--pages", type=int, default=0, help="只解析前 N 个分P（合集很大时用）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 节（默认全部）")
    ap.add_argument("--concurrency", type=int, default=8, help="AI 并发数")
    ap.add_argument("--transcribe", action="store_true", help="无字幕时用 faster-whisper 转录兜底")
    ap.add_argument("--no-ai", action="store_true", help="跳过 AI 整理，字幕清洗后直接入库")
    ap.add_argument("--out", default="output", help="输出目录")
    ap.add_argument("--course-name", default="", help="课程名（默认取视频主标题）")
    ap.add_argument("--no-structure", action="store_true", help="跳过课程级结构化（默认生成 course.json）")
    args = ap.parse_args()

    api_key = None if args.no_ai else get_api_key()
    if not args.no_ai and not api_key:
        print("未找到 DeepSeek API Key（设 DEEPSEEK_API_KEY 或放 tutor-app-main/.env）")
        sys.exit(1)
    cookie_file = args.cookies if Path(args.cookies).exists() else None
    cookie_str = load_cookies(cookie_file)
    if cookie_file:
        print(f"已加载 cookie: {cookie_file} ({len(cookie_str)} 字符)")
    else:
        print("未找到 cookies.txt（无字幕视频将拿不到 AI 字幕）")

    t_all = time.time()
    print("\n== 阶段 A：解析课程结构（view API） ==")
    t0 = time.time()
    lessons = extract_lessons(args.url, cookie_str, args.pages)
    if args.limit:
        lessons = lessons[: args.limit]
    t_a = time.time() - t0
    print(f"  解析到 {len(lessons)} 节，耗时 {fmt_sec(t_a)}")
    emit_progress("fetch-structure", 1, 1, f"解析课程结构完成：{len(lessons)} 节")
    for i, l in enumerate(lessons, 1):
        print(f"    [{i:02d}] {l['title'][:40]} ({l['duration'] / 60:.1f}min)")

    # 阶段 A2：课程级结构化（v1.4 · §7.3）
    structure = None
    if not args.no_ai and not args.no_structure:
        print("\n== 阶段 A2：课程级结构化（AI 分章/总览/前置关系） ==")
        t0 = time.time()
        try:
            structure = ai_course_structure(lessons, api_key)
            chapters = structure.get("chapters", [])
            print(f"  ✓ 生成 {len(chapters)} 章，耗时 {fmt_sec(time.time() - t0)}")
            emit_progress("structure-course", 1, 1, f"课程结构生成：{len(chapters)} 章")
        except Exception as e:
            print(f"  课程级结构化失败（可稍后手工补 course.json）: {e}")
            emit_progress("structure-course", 1, 1, "课程结构生成失败，跳过")

    print("\n== 阶段 B：获取文字内容（字幕优先 / 转录兜底） ==")
    t0 = time.time()
    subs, reasons = {}, {}
    for i, l in enumerate(lessons, 1):
        t1 = time.time()
        text, src = fetch_subtitle(l, cookie_str)
        if text is None and args.transcribe:
            print(f"    [{i}/{len(lessons)}] {l['title'][:30]} 无字幕 → 转录中...")
            text, src = transcribe(l, cookie_file)
        subs[l["bvid"] + f"_{l['cid']}"] = text
        reasons[l["bvid"] + f"_{l['cid']}"] = src
        n = len(text) if text else 0
        print(f"    [{i}/{len(lessons)}] {src} | {l['title'][:30]} ({n}字, {fmt_sec(time.time() - t1)})")
        emit_progress("fetch-subtitles", i, len(lessons), f"抓取字幕 {i}/{len(lessons)}：{l['title'][:20]}")
    t_b = time.time() - t0
    ok = sum(1 for v in subs.values() if v)
    print(f"  获取 {ok}/{len(lessons)} 节，耗时 {fmt_sec(t_b)}")

    # 阶段 C：AI 整理（并发）
    structures = {}
    t_c = 0.0
    if not args.no_ai:
        todo = [l for l in lessons if subs[l["bvid"] + f"_{l['cid']}"]]
        print(f"\n== 阶段 C：DeepSeek 整理（并发 {args.concurrency}） ==")
        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(ai_structure, l, subs[l["bvid"] + f"_{l['cid']}"], api_key): l for l in todo}
            for f in as_completed(futs):
                l = futs[f]
                done += 1
                try:
                    structures[l["bvid"] + f"_{l['cid']}"] = f.result()
                    print(f"    [{done}/{len(todo)}] ✓ {l['title'][:30]}")
                except Exception as e:
                    print(f"    [{done}/{len(todo)}] ✗ {l['title'][:30]}: {e}")
                emit_progress("summarize-lessons", done, len(todo), f"AI 整理 {done}/{len(todo)}：{l['title'][:20]}")
        t_c = time.time() - t0
        print(f"  整理完成，耗时 {fmt_sec(t_c)}")

    # 阶段 D：渲染 + 自动检查 + 写文件
    print("\n== 阶段 D：渲染 + 自动检查 ==")
    t0 = time.time()
    course_name = args.course_name or "bilibili-course"
    out_dir = Path(args.out) / slugify(course_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for i, l in enumerate(lessons, 1):
        key = l["bvid"] + f"_{l['cid']}"
        st = structures.get(key)
        md = render_lesson(l, subs[key], st, not args.no_ai)
        fname = out_dir / f"{i:02d}-{slugify(st['title'] if st else l['title'])}.md"
        fname.write_text(md, encoding="utf-8")
        errs = auto_check(fname)
        mark = "✓" if not errs else f"✗ {errs}"
        report.append((fname.name, mark))
    t_d = time.time() - t0
    for name, mark in report:
        print(f"    {mark}  {name}")
    print(f"  写文件耗时 {fmt_sec(t_d)}")

    # 课程级结构化产物：course.json（§7.3）
    if structure:
        # 关键修正：lesson_titles 用整理后的实际课时标题（保持章节归属与顺序），
        # 否则 App 端按标题匹配章节会失败（front-matter title 是 AI 整理后的新标题）
        if "chapters" in structure and lessons:
            flat = [
                (ci, li)
                for ci, ch in enumerate(structure["chapters"])
                for li in range(len(ch.get("lesson_titles", [])))
            ]
            for pos, (ci, li) in enumerate(flat):
                if pos < len(lessons):
                    key = lessons[pos]["bvid"] + f"_{lessons[pos]['cid']}"
                    st = structures.get(key)
                    real_title = st.get("title") if st and st.get("title") else lessons[pos]["title"]
                    structure["chapters"][ci]["lesson_titles"][li] = real_title
        course_json = {
            "title": structure.get("title", course_name),
            "source": {
                "bvid": lessons[0]["bvid"],
                "url": f"https://www.bilibili.com/video/{lessons[0]['bvid']}",
            },
            "overview_md": structure.get("overview_md", ""),
            "chapters": structure.get("chapters", []),
            "prerequisites": structure.get("prerequisites", []),
            "suggested_order": structure.get("suggested_order", ""),
        }
        (out_dir / "course.json").write_text(
            json.dumps(course_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("  ✓ course.json（课程级结构化：总览/章节/前置关系）")

    emit_progress("done", len(lessons), len(lessons), "导入完成")
    print(f"\n== 总耗时 {fmt_sec(time.time() - t_all)} ==")
    print(f"输出目录: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
