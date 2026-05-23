"""
YouTube Shorts Maker
--------------------
输入YouTube链接 → 自动下载 → 转录 → Claude AI选片 → 裁剪9:16竖屏 → 输出MP4

模式：
  （默认）clips   : 下载 → Whisper转录 → Claude选片 → 裁剪9:16竖屏
  --mode faceless : 下载 → Whisper转录 → Claude改写中文脚本 → Edge TTS配音 → 黑底+中文字幕 → 9:16竖屏

依赖：pip install anthropic yt-dlp faster-whisper opencv-python moviepy python-dotenv edge-tts
需要：ffmpeg 已安装并在PATH中
"""

import os
import sys
import time

# 强制UTF-8输出，避免Windows GBK终端编码错误
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import json
import asyncio
import argparse
import shutil
import subprocess
import tempfile
import re
import anthropic
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("❌ 错误：找不到 ANTHROPIC_API_KEY")
    print("请在脚本同目录下创建 .env 文件，内容为：")
    print("ANTHROPIC_API_KEY=你的API Key")
    sys.exit(1)

PEXELS_API_KEY    = os.getenv("PEXELS_API_KEY")
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY")

# ── 配置 ──────────────────────────────────────────────
NUM_CLIPS = 3          # 生成几个短视频
CLIP_DURATION = 60     # 每个片段最长多少秒（建议45-90）
ASPECT_RATIO = "9:16"  # 竖屏格式
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# ── 视觉风格素材库 ─────────────────────────────────────
USED_VIDEOS_PATH = Path("used_videos.json")

VISUAL_MOOD_KEYWORDS = {
    "urban_night": ["city night lights", "night cityscape", "urban night street"],
    "rainy":       ["rain window night", "rainy city street", "rain drops dark"],
    "sunny":       ["morning sunlight", "golden hour city", "sunrise motivation"],
    "minimal":     ["minimal desk workspace", "clean office", "focused work space"],
    "nature":      ["calm nature forest", "peaceful lake", "morning nature walk"],
}
# ──────────────────────────────────────────────────────


def log(msg):
    print(f"\n{'='*60}\n{msg}\n{'='*60}")


# 模块级单例，避免每次 API 调用重复构建
_client: anthropic.Anthropic = None

def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _yt_cookies_args() -> list:
    """自动寻找 cookies 文件，找到则返回 --cookies 参数"""
    candidates = [
        Path("cookies.txt"),
        Path("output/www.youtube.com_cookies.txt"),
        Path("www.youtube.com_cookies.txt"),
    ]
    for p in candidates:
        if p.exists():
            print(f"   🍪 使用 cookies：{p}")
            return ["--cookies", str(p)]
    return []


def _claude_call(client, **kwargs):
    """带 529（过载）和 429（限速）自动重试的 Claude API 调用"""
    for wait in (0, 30, 60, 120, 180):
        if wait:
            print(f"   ⏳ API限速/过载，{wait}秒后重试...")
            time.sleep(wait)
        try:
            return client.messages.create(**kwargs)
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 529):
                continue
            raise
    raise RuntimeError("Claude API 持续过载，请稍后再试")


def _load_used_videos() -> dict:
    """加载已使用的视频ID记录"""
    if USED_VIDEOS_PATH.exists():
        try:
            return json.loads(USED_VIDEOS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_used_video(mood: str, video_id: int):
    """记录某 mood 下已用过的视频ID，防止重复"""
    data = _load_used_videos()
    data.setdefault(mood, [])
    if video_id not in data[mood]:
        data[mood].append(video_id)
    USED_VIDEOS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Step 1: 下载YouTube视频 ───────────────────────────
def download_video(url: str, output_path: str) -> str:
    log(f"📥 下载视频：{url}")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_path,
        "--no-playlist",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
    ] + _yt_cookies_args() + [
        url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stderr)
        raise RuntimeError(f"下载失败：{result.stderr}")
    
    # yt-dlp有时会改扩展名，找实际文件
    base = output_path.replace(".mp4", "")
    for ext in [".mp4", ".mkv", ".webm"]:
        if os.path.exists(base + ext):
            return base + ext
    raise RuntimeError("下载完成但找不到文件")


# ── Step 2: 提取音频 ──────────────────────────────────
def extract_audio(video_path: str, audio_path: str):
    log("🎵 提取音频...")
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", "16000", "-ac", "1",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"音频提取失败：{result.stderr}")
    print("✅ 音频提取完成")


# ── Step 3: 语音转文字（faster-whisper，本地免费）────
def transcribe_audio(audio_path: str) -> list:
    log("📝 语音转文字（本地Whisper，首次运行需下载模型约1GB）...")
    from faster_whisper import WhisperModel
    
    # 使用base模型，速度和准确率平衡；如电脑好可改"small"或"medium"
    model = WhisperModel("base", device="cpu", compute_type="int8")
    
    segments, info = model.transcribe(
        audio_path,
        language=None,      # 自动检测语言（支持中文/英文/等）
        beam_size=5,
        word_timestamps=True
    )

    result = []
    for seg in segments:
        result.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip()
        })

    total_duration = info.duration
    detected_lang = info.language
    print(f"✅ 转录完成，语言：{detected_lang}，共 {len(result)} 段，视频时长 {total_duration:.0f}秒")
    return result, total_duration, detected_lang


# ── Step 4: Claude AI选出精彩片段 ────────────────────
def select_highlights(segments: list, total_duration: float, num_clips: int, clip_duration: int) -> list:
    log("🤖 Claude AI分析选片中...")

    client = _get_client()
    
    # 构建转录文本
    transcript_text = "\n".join([
        f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}"
        for s in segments
    ])
    
    prompt = f"""You are an expert social media video editor specializing in TikTok and Instagram Reels.

Analyze this video transcript and select the {num_clips} most viral-worthy segments.

Selection criteria (rank by these factors):
1. Strong hook in the opening line (surprising fact, bold claim, question, controversy)
2. Emotional peaks (funny, shocking, inspiring, relatable moments)
3. Self-contained story or point (makes sense without context)
4. Quotable or shareable lines
5. Practical value (tip, insight, how-to)

Rules:
- Each clip must be between 30-{clip_duration} seconds long
- Clips must NOT overlap
- Start each clip slightly before the key moment for context
- Video total duration: {total_duration:.0f} seconds

Return ONLY a valid JSON array, no other text:
[
  {{
    "clip_number": 1,
    "start_time": 45.2,
    "end_time": 98.5,
    "title": "Short catchy title for this clip",
    "hook": "The opening line that will hook viewers",
    "reason": "Why this will perform well on TikTok/Reels",
    "viral_score": 85
  }},
  ...
]

Transcript:
{transcript_text[:8000]}"""

    message = _claude_call(client,
        model="claude-sonnet-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    
    # 清理JSON
    raw = re.sub(r"```json|```", "", raw).strip()
    
    try:
        highlights = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试提取JSON数组
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            highlights = json.loads(match.group())
        else:
            raise RuntimeError(f"Claude返回的格式无法解析：{raw}")
    
    print(f"✅ 选出 {len(highlights)} 个片段：")
    for h in highlights:
        print(f"  #{h['clip_number']} [{h['start_time']:.1f}s-{h['end_time']:.1f}s] 评分:{h.get('viral_score','?')} - {h['title']}")
    
    return highlights


# ── Step 5: 裁剪视频 + 转换9:16竖屏 ─────────────────
def crop_to_vertical(video_path: str, start: float, end: float, output_path: str):
    """裁剪片段并转换为9:16竖屏格式，保留完整画面加黑边"""
    
    duration = end - start
    
    # 保留完整画面，等比缩放后上下/左右加黑边
    crop_filter = (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
    )
    
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", video_path,
        "-t", str(duration),
        "-vf", crop_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"视频裁剪失败：{result.stderr[-500:]}")


# ── Faceless Mode: 无人脸中文配音短视频 ──────────────

def search_and_download_pexels_by_mood(mood: str, duration: float,
                                        output_path: str, api_key: str) -> bool:
    """
    按视觉情绪从固定素材库搜索 Pexels 视频，跳过已用过的素材。
    mood 对应 VISUAL_MOOD_KEYWORDS 中的键。
    """
    import random, urllib.request, urllib.parse

    kw_list = VISUAL_MOOD_KEYWORDS.get(mood, VISUAL_MOOD_KEYWORDS["minimal"])
    used_ids = _load_used_videos().get(mood, [])

    # 随机打乱关键词顺序，每次换不同词搜索
    for keyword in random.sample(kw_list, len(kw_list)):
        print(f"   搜索[{mood}]：{keyword} ({duration:.1f}s)")
        query = urllib.parse.quote(keyword)
        url = (
            "https://api.pexels.com/videos/search"
            f"?query={query}&orientation=portrait&size=medium&per_page=15"
        )
        req = urllib.request.Request(url, headers={
            "Authorization": api_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            print(f"   ⚠️ Pexels搜索失败：{e}")
            continue

        videos = data.get("videos", [])
        if not videos:
            print("   ⚠️ 无结果，换下一个关键词")
            continue

        # 过滤已使用的视频；若全部用过则允许复用（防止无素材）
        fresh = [v for v in videos if v["id"] not in used_ids]
        pool  = fresh if fresh else videos
        if not pool:
            continue

        long_enough = [v for v in pool if v["duration"] >= duration]
        candidates  = long_enough if long_enough else pool
        best        = min(candidates, key=lambda v: abs(v["duration"] - duration))

        mp4_files = [f for f in best["video_files"] if f["file_type"] == "video/mp4"]
        if not mp4_files:
            continue
        best_file = max(mp4_files, key=lambda f: f.get("width", 0) * f.get("height", 0))

        reuse_note = " [复用]" if best["id"] in used_ids else ""
        print(f"   视频ID:{best['id']}{reuse_note}  {best['duration']}s  "
              f"{best_file.get('width')}x{best_file.get('height')}")
        try:
            dl_req = urllib.request.Request(best_file["link"], headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(dl_req, timeout=60) as resp, \
                 open(output_path, "wb") as out:
                out.write(resp.read())
            _save_used_video(mood, best["id"])
            print("   ✅ 背景视频下载完成")
            return True
        except Exception as e:
            print(f"   ⚠️ 下载失败：{e}")
            continue

    print(f"   ⚠️ [{mood}] 所有关键词均无结果，降级黑色背景")
    return False


# ── 背景音乐 ──────────────────────────────────────────

# Freesound API 搜索词映射（情绪 → 搜索关键词）
_MUSIC_QUERIES = {
    "motivational lofi"   : "lofi hip hop background music",
    "calm ambient"        : "calm ambient background music relaxing",
    "corporate background": "upbeat corporate background music",
}


def get_music_keyword(script: str) -> str:
    """Claude 根据脚本情绪返回三类音乐标签之一；API过载时降级返回默认值"""
    client = _get_client()
    try:
        message = _claude_call(client,
            model="claude-haiku-4-5-20251001",
            max_tokens=30,
            messages=[{
                "role": "user",
                "content": (
                    "根据脚本情绪选择背景音乐，只返回以下之一，不加其他内容：\n"
                    "motivational lofi\ncalm ambient\ncorporate background\n\n"
                    "脚本：" + script[:300]
                )
            }]
        )
        raw = message.content[0].text.strip().lower()
        for key in _MUSIC_QUERIES:
            if key in raw:
                return key
    except RuntimeError:
        print("   ⚠️ 音乐关键词API失败，使用默认值")
    return "motivational lofi"


def download_music_from_freesound(keyword: str, output_path: str, api_key: str) -> bool:
    """
    通过 Freesound API 搜索匹配的背景音乐，下载 HQ 预览 MP3（无需 OAuth）。
    失败返回 False，调用方降级为无背景音乐。
    """
    import urllib.request, urllib.parse

    query = _MUSIC_QUERIES.get(keyword, "ambient background music")
    log(f"🎵 背景音乐（{keyword}）via Freesound...")

    search_url = ("https://freesound.org/apiv2/search/text/?" + urllib.parse.urlencode({
        "query"   : query,
        "token"   : api_key,
        "fields"  : "id,name,duration,previews",
        "filter"  : "duration:[30 TO 300]",  # 30秒~5分钟
        "sort"    : "rating_desc",
        "page_size": "10",
    }))
    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"   ⚠️ Freesound搜索失败：{e}")
        return False

    results = data.get("results", [])
    if not results:
        print("   ⚠️ Freesound无结果")
        return False

    print(f"   找到 {len(results)} 条结果")
    for i, sound in enumerate(results):
        preview_url = sound.get("previews", {}).get("preview-hq-mp3", "")
        if not preview_url:
            continue
        dur = sound.get("duration", 0)
        print(f"   [{i+1}] {sound.get('name','?')}  {dur:.0f}s")
        try:
            dl_req = urllib.request.Request(preview_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(dl_req, timeout=60) as r, \
                 open(output_path, "wb") as out:
                out.write(r.read())
            print(f"   ✅ 背景音乐下载完成：{os.path.getsize(output_path)//1024}KB")
            return True
        except Exception as e:
            print(f"   ⚠️ 下载失败，跳过：{e}")
            continue

    print("   ⚠️ 所有结果下载失败，跳过背景音乐")
    return False


def mix_audio_with_music(tts_path: str, music_path: str, output_path: str):
    """将背景音乐以 40% 音量混入 TTS 配音，以 TTS 时长为准"""
    cmd = [
        "ffmpeg", "-y",
        "-i", tts_path,
        "-stream_loop", "-1", "-i", music_path,
        "-filter_complex",
        "[1:a]volume=0.40[bg];[0:a][bg]amix=inputs=2:duration=first:normalize=0[out]",
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"音频混合失败：{r.stderr[-400:]}")
    print("   ✅ 音频混合完成（TTS 100% + 音乐 25%）")


def split_script_segments(script: str, max_chars: int = 90) -> list:
    """将脚本按段落/句子拆分为若干段，每段约 max_chars 字以内"""
    # 去掉 Markdown 加粗标记
    clean = re.sub(r"\*\*", "", script)

    # 先按换行分段落
    paragraphs = [p.strip() for p in re.split(r"\n+", clean) if p.strip()]

    segments = []
    for para in paragraphs:
        if len(para) <= max_chars:
            segments.append(para)
        else:
            # 按句号/叹号/问号继续拆
            parts = re.split(r"(?<=[。！？])", para)
            buf = ""
            for part in parts:
                if not part.strip():
                    continue
                if len(buf) + len(part) <= max_chars:
                    buf += part
                else:
                    if buf:
                        segments.append(buf.strip())
                    buf = part
            if buf.strip():
                segments.append(buf.strip())

    # 合并极短段（< 10 字）到前一段
    merged = []
    for seg in segments:
        if len(seg) < 10 and merged:
            merged[-1] += seg
        else:
            merged.append(seg)

    return merged


def prepare_bg_segment(src_path: str, duration: float, output_path: str):
    """把 Pexels 视频缩放裁剪到 1080x1920，精确截取到指定时长，无音频"""
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", src_path,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
        "-t", f"{duration:.3f}",
        "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-an",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"背景片段处理失败：{r.stderr[-500:]}")


def make_black_segment(duration: float, output_path: str):
    """生成纯黑色背景片段（Pexels 失败时降级用）"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s=1080x1920:r=30:d={duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-an",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"黑色片段生成失败：{r.stderr[-300:]}")


def concatenate_segments(segment_paths: list, output_path: str):
    """用 ffmpeg concat 协议无损拼接所有背景片段"""
    list_file = output_path + ".txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(list_file)
    if r.returncode != 0:
        raise RuntimeError(f"视频拼接失败：{r.stderr[-500:]}")


def identify_topics(segments: list, num_parts: int) -> list:
    """
    用 Claude Haiku 从完整转录中识别 num_parts 个独立话题及其片段范围。
    返回：[{"topic": str, "start_idx": int, "end_idx": int}, ...]
    失败时降级为等分。
    """
    client = _get_client()
    log(f"🔍 Claude 分析话题结构（目标 {num_parts} 段）...")

    # 构建带索引的简洁转录，控制在 20000 字符以内
    lines = [f"[{i}] {s['start']:.0f}s: {s['text']}" for i, s in enumerate(segments)]
    transcript = "\n".join(lines)[:20000]
    total = len(segments)

    prompt = (
        f"分析以下视频转录，识别出 {num_parts} 个内容独立、可以单独发布为短视频的话题段落。\n\n"
        "要求：\n"
        "- 每个话题必须内容完整、自成一体，不依赖其他段落\n"
        "- 话题之间不重叠，合起来覆盖整个视频\n"
        f"- 段落序号范围：0 ~ {total - 1}\n"
        "- 只返回 JSON 数组，不要任何其他文字\n\n"
        "格式：\n"
        '[{"topic":"话题描述（中文10字内）","start_idx":0,"end_idx":47},...]\n\n'
        f"转录（格式 [序号] 秒数: 内容）：\n{transcript}"
    )

    topics = None
    try:
        message = _claude_call(client,
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r"```json|```", "", message.content[0].text.strip()).strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        topics = json.loads(match.group() if match else raw)
    except RuntimeError:
        print(f"   ⚠️ API过载，话题识别降级为等分")
    except (json.JSONDecodeError, AttributeError):
        print(f"   ⚠️ 话题识别解析失败，降级为等分")

    # 降级：等分
    if not topics or len(topics) != num_parts:
        print(f"   ⚠️ 话题数量不符，降级为等分")
        step = total // num_parts
        topics = [
            {
                "topic": f"第{i+1}部分",
                "start_idx": i * step,
                "end_idx": (i + 1) * step - 1 if i < num_parts - 1 else total - 1,
            }
            for i in range(num_parts)
        ]

    # 修正边界，防止 Claude 返回越界值
    for t in topics:
        t["start_idx"] = max(0, min(int(t["start_idx"]), total - 1))
        t["end_idx"]   = max(t["start_idx"], min(int(t["end_idx"]), total - 1))

    for i, t in enumerate(topics):
        start_t = segments[t["start_idx"]]["start"]
        end_t   = segments[t["end_idx"]]["end"]
        print(f"   话题{i+1}「{t['topic']}」{start_t:.0f}s ~ {end_t:.0f}s "
              f"（片段 {t['start_idx']}~{t['end_idx']}）")

    return topics


def rewrite_to_chinese_script(segments: list, source_lang: str = "en") -> str:
    """将转录内容改写为中文抖音脚本，含内容类型分级、质量评分、最多3次重试"""
    log("🤖 Claude改写中文抖音脚本...")

    client = _get_client()

    transcript_text = " ".join([s["text"] for s in segments])

    if source_lang == "zh":
        task_desc = "提炼以下中文视频内容，改写为抖音短视频脚本。"
        input_label = "原视频内容"
    else:
        task_desc = "将以下英文内容翻译改写为抖音短视频脚本。"
        input_label = "英文原文"

    prompt = (
        f"你是顶级抖音内容创作者。{task_desc}\n\n"
        "【第一步】判断内容类型，选其一：\n"
        "- quote（金句观点）→ 目标80-100字\n"
        "- method（方法技巧）→ 目标150-180字\n"
        "- story（叙事案例）→ 目标180-220字\n\n"
        "【写作规则，全部强制执行】\n"
        "1. 第一句必须让人产生疑问、震惊或强烈共鸣\n"
        "2. 开头必须用「你知道吗」「没人告诉你」「99%的人都错了」类型句式\n"
        "3. 每句话不超过15个字\n"
        "4. 必须含至少1个反直觉观点\n"
        "5. 像朋友说话，不像老师讲课\n"
        "6. 禁止任何Markdown符号\n"
        "7. 每100字至少含1个具体数字或案例\n"
        "8. 禁用词：首先/其次/最后/总结/其实/说真的/你想想\n"
        "9. 禁止过渡句，每句必须有实质信息\n\n"
        "【写完后对自己严格评分，不要放水】\n"
        "- hook强度（1-10）：第一句是否足够抓人\n"
        "- 口语化程度（1-10）：是否真的像朋友聊天\n"
        "- 信息密度（1-10）：有无废话\n"
        "- 行动驱动力（1-10）：是否让人想点赞评论\n"
        "总分 = 四项平均值\n\n"
        "【第三步】根据脚本整体情绪选择视觉风格（选其一）：\n"
        "- urban_night：励志/突破/成就类\n"
        "- rainy：思考/哲学/内敛类\n"
        "- sunny：积极/能量/早晨类\n"
        "- minimal：效率/专注/简约类\n"
        "- nature：平静/习惯/自然类\n\n"
        "只返回JSON，禁止其他任何内容：\n"
        '{"script":"脚本正文","content_type_detail":"quote/method/story",'
        '"target_words":100,"actual_words":95,"visual_mood":"urban_night",'
        '"scores":{"hook":8,"colloquial":9,"density":8,"action":7},"total_score":8.0}\n\n'
        f"【{input_label}】\n{transcript_text[:6000]}"
    )

    best = None
    for attempt in range(3):
        msg = _claude_call(client,
            model="claude-sonnet-4-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r"```json|```", "", msg.content[0].text.strip()).strip()
        data = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if not data or "script" not in data:
            print(f"   ⚠️ 第{attempt+1}次解析失败，重试")
            print(f"   原始返回（前200字）：{raw[:200]}")
            continue

        score = float(data.get("total_score", 0))
        ctype = data.get("content_type_detail", "?")
        words = data.get("actual_words", len(data["script"]))
        print(f"   第{attempt+1}次 → 评分{score:.1f}/10  类型:{ctype}  字数:{words}")

        if best is None or score > float(best.get("total_score", 0)):
            best = data

        if score >= 8.0:
            print(f"   ✅ 达标（≥8分），采用此版本")
            break

    if not best:
        raise RuntimeError("Claude脚本生成失败，3次均无法解析")

    script = best["script"]
    # 按内容类型确定字数上限，向前向后各留缓冲寻找句末
    limit = {"quote": 100, "method": 180, "story": 220}.get(
        best.get("content_type_detail", "method"), 180
    )
    if len(script) > limit:
        cut = None
        for i in range(limit - 1, max(limit - 40, 0) - 1, -1):
            if script[i] in "。！？":
                cut = i + 1
                break
        if cut is None:
            for i in range(limit, min(limit + 30, len(script))):
                if script[i] in "。！？":
                    cut = i + 1
                    break
        script = script[:cut] if cut else script[:limit]

    visual_mood = best.get("visual_mood", "minimal")
    if visual_mood not in VISUAL_MOOD_KEYWORDS:
        visual_mood = "minimal"

    print(f"✅ 脚本完成（最高分{float(best.get('total_score',0)):.1f}/10），共{len(script)}字")
    print(f"   视觉风格：{visual_mood}")
    print(f"   预览：{script[:80]}...")
    return script, visual_mood


async def _tts_generate(text: str, audio_path: str, srt_path: str, voice: str):
    """异步生成TTS音频和字幕（兼容 edge-tts v6/v7）"""
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    submaker = edge_tts.SubMaker()

    # v7: feed(chunk); v6: create_sub((offset, duration), text)
    use_feed = hasattr(submaker, "feed")

    with open(audio_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                if use_feed:
                    submaker.feed(chunk)
                else:
                    submaker.create_sub(
                        (chunk["offset"], chunk["duration"]),
                        chunk["text"]
                    )

    # v7: get_srt(); v6: generate_subs()
    if hasattr(submaker, "get_srt"):
        srt_content = submaker.get_srt()
    else:
        srt_content = submaker.generate_subs(words_in_cue=6)

    with open(srt_path, "w", encoding="utf-8") as srt_file:
        srt_file.write(srt_content)

    print("✅ TTS音频和字幕生成完成")



def generate_tts_audio(text: str, audio_path: str, srt_path: str,
                        voice: str = "zh-CN-XiaoxiaoNeural"):
    """生成Edge TTS中文配音和SRT字幕（保留原始时间戳，视觉换行在ASS阶段处理）"""
    log(f"🎙️ Edge TTS生成中文配音（{voice}）...")
    asyncio.run(_tts_generate(text, audio_path, srt_path, voice))


def get_audio_duration(audio_path: str) -> float:
    """用ffprobe获取音频文件时长（秒）"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe失败：{result.stderr}")
    return float(json.loads(result.stdout)["format"]["duration"])


def _srt_to_ass(srt_path: str, ass_path: str):
    """将 SRT 转成 ASS，PlayResX/Y=1080x1920，字幕样式直接嵌入"""
    with open(srt_path, encoding="utf-8") as f:
        srt_content = f.read()

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        # PrimaryColour=米黄色 #FFFDE7 → ABGR &H00E7FDFF
        "Style: Default,Microsoft YaHei,56,&H00E7FDFF,&H000000FF,"
        "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,2,30,30,350,1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    def _srt_t_to_ass_t(t: str) -> str:
        """SRT 时间 HH:MM:SS,mmm → ASS 时间 H:MM:SS.cc（毫秒转厘秒）"""
        t = t.strip()
        main, ms = t.split(",")
        h, m, s = main.split(":")
        cs = int(ms) // 10          # 毫秒 → 厘秒（ASS 只用2位）
        return f"{int(h)}:{m}:{s}.{cs:02d}"

    def _wrap(text: str, n: int = 10) -> str:
        """每 n 个字符插入 ASS 硬换行 \\N，保持视觉可读性"""
        parts, i = [], 0
        while i < len(text):
            parts.append(text[i:i+n])
            i += n
        return r"\N".join(parts)

    entries = re.split(r"\n{2,}", srt_content.strip())
    dialogues = []
    for entry in entries:
        lines = entry.strip().splitlines()
        if len(lines) < 3:
            continue
        timing = lines[1]
        text = "".join(lines[2:]).strip()
        start = _srt_t_to_ass_t(timing.split("-->")[0])
        end   = _srt_t_to_ass_t(timing.split("-->")[1])
        dialogues.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{_wrap(text)}")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(dialogues) + "\n")


def build_faceless_video(audio_path: str, srt_path: str, output_path: str,
                          duration: float, bg_video_path: str = None,
                          bg_is_ready: bool = False):
    """
    合成最终视频。
    bg_is_ready=True  : bg 已是 1080x1920，直接叠字幕+音频（分段拼接后使用）
    bg_is_ready=False : bg 是原始 Pexels 单视频，需 scale/crop + stream_loop
    bg_video_path=None: 纯黑背景
    """
    mode = "分段Pexels背景" if (bg_video_path and bg_is_ready) else \
           ("Pexels背景" if bg_video_path else "黑色背景")
    log(f"🎬 合成最终视频（{mode}+字幕+配音）...")

    # SRT → ASS，直接写入 PlayResX/Y=1080x1920，所有坐标是实际像素
    tmp_ass = "tmp_faceless_subs.ass"
    _srt_to_ass(srt_path, tmp_ass)

    try:
        sub_filter = f"ass={tmp_ass}"

        if bg_video_path and bg_is_ready:
            # 已预处理好的拼接背景，只需叠遮罩+字幕+音频
            vf = f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:t=fill,{sub_filter}"
            cmd = [
                "ffmpeg", "-y",
                "-i", bg_video_path,
                "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-vf", vf,
                "-t", str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                output_path
            ]
        elif bg_video_path:
            # 单个 Pexels 原始视频，需 scale/crop + 循环
            vf = (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,drawbox=x=0:y=0:w=iw:h=ih:color=black@0.45:t=fill,{sub_filter}"
            )
            cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1",
                "-i", bg_video_path,
                "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-vf", vf,
                "-t", str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                output_path
            ]
        else:
            # 纯黑背景（无需 drawbox，本身就是黑色）
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c=black:s=1080x1920:r=30:d={duration + 0.5}",
                "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-vf", sub_filter,
                "-t", str(duration),
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                output_path
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[ffmpeg stderr]\n{result.stderr[-2000:]}")
            raise RuntimeError(f"视频合成失败：{result.stderr[-500:]}")
        # 打印 ffmpeg 警告（帮助诊断字幕/音频问题）
        for line in result.stderr.splitlines():
            if any(k in line for k in ("subtitle", "Sub", "Error", "Invalid", "No such", "Unable")):
                print(f"   [ffmpeg] {line}")
        print(f"✅ 视频生成完成：{output_path}")
    finally:
        if os.path.exists(tmp_ass):
            os.unlink(tmp_ass)


def _generate_one_video(whisper_segs: list, part_label: str, tmp_dir: str,
                        source_lang: str = "en") -> Path:
    """
    从一批 Whisper 片段生成一个完整的 faceless 短视频。
    返回输出文件路径。
    """
    p = part_label  # 用于临时文件名前缀，避免多段并发冲突
    tts_audio_path  = os.path.join(tmp_dir, f"tts_{p}.mp3")
    mixed_audio_path= os.path.join(tmp_dir, f"mixed_{p}.mp3")
    srt_path        = os.path.join(tmp_dir, f"subtitles_{p}.srt")
    music_path      = os.path.join(tmp_dir, f"bgmusic_{p}.mp3")

    # A. 改写中文脚本（返回脚本文本 + 视觉风格）
    chinese_script, visual_mood = rewrite_to_chinese_script(whisper_segs, source_lang=source_lang)

    # B. 背景音乐
    final_audio_path = tts_audio_path
    music_ok = False
    if FREESOUND_API_KEY:
        music_keyword = get_music_keyword(chinese_script)
        print(f"   音乐情绪：{music_keyword}")
        music_ok = download_music_from_freesound(music_keyword, music_path, FREESOUND_API_KEY)
    else:
        print("   ℹ️ 未设置 FREESOUND_API_KEY，跳过背景音乐")

    # C. Edge TTS 配音 + 字幕
    generate_tts_audio(chinese_script, tts_audio_path, srt_path)

    # D. 混合 TTS + 背景音乐
    if music_ok:
        try:
            mix_audio_with_music(tts_audio_path, music_path, mixed_audio_path)
            final_audio_path = mixed_audio_path
        except Exception as e:
            print(f"   ⚠️ 音频混合失败：{e}，使用纯TTS")

    # E. 获取最终音频时长
    tts_duration = get_audio_duration(final_audio_path)
    print(f"   最终音频时长：{tts_duration:.1f}秒")

    # F. 脚本分段 + Pexels 背景（mood-based）
    bg_path, bg_ready = None, False
    if PEXELS_API_KEY:
        log(f"🎨 下载Pexels背景视频（mood: {visual_mood}）...")
        script_segs   = split_script_segments(chinese_script)
        total_chars   = sum(len(s) for s in script_segs)
        seg_durations = [tts_duration * len(s) / total_chars for s in script_segs]
        print(f"   共 {len(script_segs)} 段")

        bg_segments = []
        for i, seg_dur in enumerate(seg_durations):
            print(f"\n── 段落 {i+1}/{len(script_segs)} ──")
            raw_path = os.path.join(tmp_dir, f"pexels_{p}_{i}.mp4")
            seg_path = os.path.join(tmp_dir, f"seg_bg_{p}_{i:02d}.mp4")
            ok = search_and_download_pexels_by_mood(visual_mood, seg_dur, raw_path, PEXELS_API_KEY)
            try:
                if ok:
                    prepare_bg_segment(raw_path, seg_dur, seg_path)
                    print(f"   ✅ 背景片段 #{i+1} 处理完成")
                else:
                    make_black_segment(seg_dur, seg_path)
                    print(f"   ⬛ 背景片段 #{i+1} 使用黑色")
            except Exception as e:
                print(f"   ⚠️ 片段 #{i+1} 失败：{e}，降级黑色")
                make_black_segment(seg_dur, seg_path)
            bg_segments.append(seg_path)

        log("🔗 拼接背景片段...")
        bg_concat = os.path.join(tmp_dir, f"bg_concat_{p}.mp4")
        concatenate_segments(bg_segments, bg_concat)
        print(f"   ✅ 拼接完成（{len(bg_segments)} 段）")
        bg_path, bg_ready = bg_concat, True
    else:
        print("   ℹ️ 未设置 PEXELS_API_KEY，使用黑色背景")

    # G. 输出文件（自动递增序号）
    idx = 1
    while (OUTPUT_DIR / f"faceless_{idx:02d}.mp4").exists():
        idx += 1
    output_file = OUTPUT_DIR / f"faceless_{idx:02d}.mp4"

    build_faceless_video(
        final_audio_path, srt_path, str(output_file),
        tts_duration, bg_path, bg_is_ready=bg_ready
    )
    return output_file


def run_faceless_mode(url: str, parts: int = 1):
    """无人脸中文配音模式主流程。parts=1 单视频；parts>1 按话题生成多个视频"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # ── 一次性：下载 → 提取音频 → 转录 ──────────────
        video_path = download_video(url, os.path.join(tmp_dir, "source.mp4"))
        extract_audio(video_path, os.path.join(tmp_dir, "audio.wav"))
        whisper_segs, _, detected_lang = transcribe_audio(os.path.join(tmp_dir, "audio.wav"))
        if not whisper_segs:
            print("❌ 转录结果为空，可能是无声视频或语言不是英语")
            sys.exit(1)

        # ── 话题切分 ──────────────────────────────────────
        if parts > 1:
            topics = identify_topics(whisper_segs, parts)
        else:
            topics = [{"topic": "完整视频", "start_idx": 0, "end_idx": len(whisper_segs) - 1}]

        # ── 逐话题生成视频 ────────────────────────────────
        output_files = []
        for i, topic in enumerate(topics):
            if parts > 1:
                log(f"🎬 生成第 {i+1}/{len(topics)} 段：「{topic['topic']}」")
            seg_segs = whisper_segs[topic["start_idx"]: topic["end_idx"] + 1]
            try:
                out = _generate_one_video(seg_segs, str(i), tmp_dir, source_lang=detected_lang)
                output_files.append(out)
            except Exception as e:
                print(f"❌ 第 {i+1} 段生成失败，跳过继续：{e}")

        # ── 完成汇报 ──────────────────────────────────────
        if len(output_files) == 1:
            log(f"🎉 完成！输出：{output_files[0].absolute()}")
        else:
            log(f"🎉 完成！共生成 {len(output_files)} 个视频：")
            for f in output_files:
                print(f"   📄 {f.absolute()}")


# ── Clips Mode（原有逻辑） ────────────────────────────

def run_clips_mode(url: str):
    """精彩片段剪辑模式主流程"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        video_path_template = os.path.join(tmp_dir, "source.mp4")
        audio_path = os.path.join(tmp_dir, "audio.wav")

        video_path = download_video(url, video_path_template)
        extract_audio(video_path, audio_path)

        segments, total_duration, _ = transcribe_audio(audio_path)
        if not segments:
            print("❌ 转录结果为空，可能是无声视频或语言不是英语")
            sys.exit(1)

        highlights = select_highlights(segments, total_duration, NUM_CLIPS, CLIP_DURATION)

        log("✂️ 开始生成短视频...")
        success_count = 0

        for h in highlights:
            clip_num = h.get("clip_number", success_count + 1)
            start = float(h["start_time"])
            end = float(h["end_time"])
            title = re.sub(r'[^\w\s-]', '', h.get("title", f"clip_{clip_num}"))
            title = title.strip().replace(" ", "_")[:40]

            output_file = OUTPUT_DIR / f"short_{clip_num:02d}_{title}.mp4"

            print(f"\n✂️ 生成片段 #{clip_num}: {start:.1f}s → {end:.1f}s")
            print(f"   Hook: {h.get('hook', '')}")
            print(f"   原因: {h.get('reason', '')}")

            try:
                crop_to_vertical(video_path, start, end, str(output_file))
                print(f"   ✅ 保存到：{output_file}")
                success_count += 1
            except Exception as e:
                print(f"   ❌ 失败：{e}")

        log(f"🎉 完成！成功生成 {success_count}/{len(highlights)} 个短视频")
        print(f"📁 输出目录：{OUTPUT_DIR.absolute()}")


# ── 主程序 ────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="YouTube Shorts Maker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python shorts_maker.py https://youtube.com/watch?v=xxxxx\n"
            "  python shorts_maker.py https://youtube.com/watch?v=xxxxx --mode faceless"
        )
    )
    parser.add_argument("url", help="YouTube视频链接")
    parser.add_argument(
        "--mode",
        choices=["clips", "faceless"],
        default="clips",
        help="clips（默认）：剪辑精彩片段；faceless：无人脸中文配音"
    )
    parser.add_argument(
        "--parts",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="faceless 模式：将视频按话题拆成 1/2/3 个独立短视频（默认 1）"
    )
    args = parser.parse_args()

    if args.mode == "faceless":
        run_faceless_mode(args.url, parts=args.parts)
    else:
        if args.parts > 1:
            print("⚠️ --parts 仅对 --mode faceless 有效，已忽略")
        run_clips_mode(args.url)


if __name__ == "__main__":
    main()
