"""
Book Maker - 一分钟读一本书短视频生成器
----------------------------------------
输入书名 → 自动生成60秒书单抖音短视频

用法：
  python book_maker.py "思考快与慢"
  python book_maker.py "原则" "穷查理宝典" "纳瓦尔宝典"

依赖：pip install anthropic python-dotenv edge-tts pillow
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
import subprocess
import tempfile
import re
import random
import urllib.request
import urllib.parse
import anthropic
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    print("❌ 错误：找不到 ANTHROPIC_API_KEY，请检查 .env 文件")
    sys.exit(1)

PEXELS_API_KEY    = os.getenv("PEXELS_API_KEY")
FREESOUND_API_KEY = os.getenv("FREESOUND_API_KEY")
TTS_VOICE         = os.getenv("TTS_VOICE", "zh-CN-XiaoyiNeural")

OUTPUT_DIR    = Path("output/book")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

COVER_DURATION = 3          # 封面静帧展示秒数（这段内无字幕，只展示封面）

# ── 视觉风格素材库（与 shorts_maker 共享同一 used_videos.json）──
USED_VIDEOS_PATH = Path("used_videos.json")

VISUAL_MOOD_KEYWORDS = {
    "urban_night": ["city lights bokeh", "neon street night", "urban night glow"],
    "rainy":       ["rain window light", "cafe rainy day", "cozy indoor rain"],
    "sunny":       ["golden hour sunlight", "morning light window", "warm sunlight leaves"],
    "minimal":     ["minimal white desk", "clean aesthetic room", "soft light interior"],
    "nature":      ["forest sunlight ray", "peaceful garden morning", "green nature light"],
}


# ── 工具函数 ─────────────────────────────────────────────

def log(msg: str):
    print(f"\n{'='*60}\n{msg}\n{'='*60}")


# 模块级单例
_client: anthropic.Anthropic = None

def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


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
    if USED_VIDEOS_PATH.exists():
        try:
            return json.loads(USED_VIDEOS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_used_video(mood: str, video_id: int):
    data = _load_used_videos()
    data.setdefault(mood, [])
    if video_id not in data[mood]:
        data[mood].append(video_id)
    USED_VIDEOS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def search_and_download_pexels_by_mood(mood: str, duration: float,
                                        output_path: str, api_key: str) -> bool:
    """按视觉情绪从固定素材库搜索，跳过已用过的视频"""
    used_ids = _load_used_videos().get(mood, [])
    kw_list  = VISUAL_MOOD_KEYWORDS.get(mood, VISUAL_MOOD_KEYWORDS["minimal"])

    for keyword in random.sample(kw_list, len(kw_list)):
        print(f"   搜索[{mood}]：{keyword} ({duration:.1f}s)")
        query = urllib.parse.quote(keyword)
        url   = (
            "https://api.pexels.com/videos/search"
            f"?query={query}&orientation=portrait&size=medium&per_page=15"
        )
        req = urllib.request.Request(url, headers={
            "Authorization": api_key, "User-Agent": "Mozilla/5.0"
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            print(f"   ⚠️ 搜索失败：{e}")
            continue

        videos = data.get("videos", [])
        if not videos:
            print("   ⚠️ 无结果，换下一个关键词")
            continue

        fresh      = [v for v in videos if v["id"] not in used_ids]
        pool       = fresh if fresh else videos
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
            dl = urllib.request.Request(best_file["link"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(dl, timeout=60) as r, open(output_path, "wb") as out:
                out.write(r.read())
            _save_used_video(mood, best["id"])
            print("   ✅ 背景视频下载完成")
            return True
        except Exception as e:
            print(f"   ⚠️ 下载失败：{e}")
            continue

    print(f"   ⚠️ [{mood}] 所有关键词无结果，降级黑色背景")
    return False


def get_audio_duration(audio_path: str) -> float:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe失败：{r.stderr}")
    return float(json.loads(r.stdout)["format"]["duration"])


def prepend_silence(audio_path: str, silence_sec: float, output_path: str):
    """在音频前插入静音，使 TTS 语音从 silence_sec 秒处才开始"""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-t", str(silence_sec),
        "-i", "anullsrc=r=44100:cl=stereo",
        "-i", audio_path,
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"静音插入失败：{r.stderr[-300:]}")


def _srt_ms(t: str) -> int:
    """SRT 时间字符串 → 毫秒整数"""
    h, m, rest = t.strip().split(":")
    s, ms = rest.split(",")
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def _ms_srt(ms: int) -> str:
    """毫秒整数 → SRT 时间字符串"""
    ms = max(0, ms)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def split_long_srt(srt_content: str, max_chars: int = 10) -> str:
    """
    把超长 SRT 条目按 max_chars 个字切割，时间按字数比例分配。
    解决 edge-tts 对中文只生成一条巨型字幕的问题。
    """
    entries = re.split(r"\n{2,}", srt_content.strip())
    new_entries, counter = [], 1

    for entry in entries:
        lines = entry.strip().splitlines()
        if len(lines) < 3:
            continue
        timing = lines[1]
        text   = "".join(lines[2:]).strip()
        start_ms = _srt_ms(timing.split("-->")[0])
        end_ms   = _srt_ms(timing.split("-->")[1])

        if len(text) <= max_chars:
            new_entries.append(f"{counter}\n{timing}\n{text}")
            counter += 1
            continue

        # 在标点处切割，否则强制按 max_chars 切
        PUNCTS = "，。！？、："
        chunks, i = [], 0
        while i < len(text):
            if i + max_chars >= len(text):
                chunks.append(text[i:])
                break
            best = None
            for j in range(min(i + max_chars + 3, len(text)),
                           max(i, i + max_chars - 3) - 1, -1):
                if j < len(text) and text[j] in PUNCTS:
                    best = j + 1
                    break
            cut = best if best else i + max_chars
            chunks.append(text[i:cut])
            i = cut

        total_chars = sum(len(c) for c in chunks)
        duration    = end_ms - start_ms
        cur_ms      = start_ms
        for idx, chunk in enumerate(chunks):
            chunk_dur = int(duration * len(chunk) / total_chars)
            chunk_end = cur_ms + chunk_dur if idx < len(chunks) - 1 else end_ms
            new_entries.append(
                f"{counter}\n{_ms_srt(cur_ms)} --> {_ms_srt(chunk_end)}\n{chunk}"
            )
            counter += 1
            cur_ms = chunk_end

    return "\n\n".join(new_entries) + "\n"


# ── Step 1: Claude生成书单脚本 ───────────────────────────

def generate_book_script(book_title: str) -> dict:
    """
    调用 Claude 生成书单脚本，含质量评分自动重试（最多3次，取最高分版本）。
    返回：{script, content_type_detail, target_words, actual_words,
           scores, total_score, cover_title, cover_subtitle, music_keyword, bg_keywords}
    """
    log(f"🤖 Claude生成《{book_title}》书单脚本...")
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = (
        f"你是顶级抖音书单创作者，为《{book_title}》生成短视频脚本。\n\n"
        "【写作规则，全部强制执行】\n"
        "1. 第一句必须点出这本书解决什么具体痛点，让人立刻共鸣\n"
        "2. 必须含至少1个反直觉观点\n"
        "3. 必须有一句作者核心思想的改写金句（非直接引用）\n"
        "4. 最后一句让人想立刻去读这本书\n"
        "5. 每句话不超过15个字\n"
        "6. 像朋友说话，不像老师讲课\n"
        "7. 禁止任何Markdown符号\n"
        "8. 每100字至少含1个具体数字或案例\n"
        "9. 禁用词：首先/其次/最后/总结/其实/说真的/你想想\n"
        "10. 禁止过渡句，每句必须有实质信息\n"
        "11. 目标字数：180-220字\n\n"
        "【写完后对自己严格评分，不要放水】\n"
        "- hook强度（1-10）：第一句是否足够抓人\n"
        "- 口语化程度（1-10）：是否真的像朋友聊天\n"
        "- 信息密度（1-10）：有无废话\n"
        "- 行动驱动力（1-10）：是否让人想去买书\n"
        "总分 = 四项平均值\n\n"
        "【第二步】根据书的整体情绪选择视觉风格（选其一）：\n"
        "- urban_night：励志/突破/成就类\n"
        "- rainy：思考/哲学/内敛/历史类\n"
        "- sunny：积极/能量/成长类\n"
        "- minimal：效率/商业/简约类\n"
        "- nature：平静/哲学/自然类\n\n"
        "只返回JSON，禁止其他任何内容：\n"
        "{\n"
        '  "script": "脚本正文（180-220字纯文本）",\n'
        '  "content_type_detail": "book",\n'
        '  "target_words": 200,\n'
        '  "actual_words": 195,\n'
        '  "visual_mood": "rainy",\n'
        '  "scores": {"hook": 8, "colloquial": 9, "density": 8, "action": 7},\n'
        '  "total_score": 8.0,\n'
        '  "cover_title": "封面大标题，8字以内，点出书的核心价值",\n'
        '  "cover_subtitle": "封面副标题，引发好奇的疑问，15字以内",\n'
        '  "music_keyword": "背景音乐英文搜索词，如：calm ambient reading"\n'
        "}"
    )

    client = _get_client()
    best = None
    for attempt in range(3):
        msg = _claude_call(client,
            model="claude-sonnet-4-5",
            max_tokens=900,
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
        words = data.get("actual_words", len(data["script"]))
        print(f"   第{attempt+1}次 → 评分{score:.1f}/10  字数:{words}")

        if best is None or score > float(best.get("total_score", 0)):
            best = data

        if score >= 8.0:
            print(f"   ✅ 达标（≥8分），采用此版本")
            break

    if not best:
        raise RuntimeError("Claude脚本生成失败，3次均无法解析")

    for key in ("script", "cover_title", "cover_subtitle", "music_keyword"):
        if key not in best:
            raise RuntimeError(f"Claude返回缺少字段：{key}")

    visual_mood = best.get("visual_mood", "rainy")
    if visual_mood not in VISUAL_MOOD_KEYWORDS:
        visual_mood = "rainy"
    best["visual_mood"] = visual_mood

    script = best["script"]
    # 超出220字：先向前找句末（220→180），再向后找（220→260），最后才硬截
    if len(script) > 220:
        cut = None
        for i in range(219, max(180, 0) - 1, -1):
            if script[i] in "。！？":
                cut = i + 1
                break
        if cut is None:
            for i in range(220, min(260, len(script))):
                if script[i] in "。！？":
                    cut = i + 1
                    break
        best["script"] = script[:cut] if cut else script[:220]

    best_score = float(best.get("total_score", 0))
    print(f"✅ 脚本完成（最高分{best_score:.1f}/10），共{len(best['script'])}字")
    print(f"   封面标题：{best['cover_title']}")
    print(f"   封面副标题：{best['cover_subtitle']}")
    print(f"   音乐关键词：{best['music_keyword']}")
    print(f"   视觉风格：{best['visual_mood']}")
    print(f"   脚本预览：{best['script'][:80]}...")
    return best


# ── Step 2: 获取书籍封面图 ────────────────────────────────

def download_book_cover_image(book_title: str, output_path: str) -> bool:
    """优先 Google Books API，失败则 Pexels 'open book reading'"""
    # Google Books（免费，无需 API Key）
    search_url = "https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode({
        "q": f"intitle:{book_title}",
        "maxResults": "5",
    })
    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        for item in data.get("items", []):
            thumb = item.get("volumeInfo", {}).get("imageLinks", {}).get("thumbnail", "")
            if not thumb:
                continue
            # 升级分辨率：zoom=1 → zoom=0，http → https
            thumb = re.sub(r"zoom=\d", "zoom=0", thumb).replace("http://", "https://")
            try:
                dl = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl, timeout=15) as r2, \
                     open(output_path, "wb") as f:
                    f.write(r2.read())
                if os.path.getsize(output_path) > 5000:
                    print(f"   ✅ Google Books封面下载完成")
                    return True
            except Exception:
                continue
    except Exception as e:
        print(f"   ⚠️ Google Books失败：{e}")

    # 降级：Pexels 图片搜索
    if PEXELS_API_KEY:
        try:
            url = ("https://api.pexels.com/v1/search"
                   "?query=open+book+reading&per_page=5&orientation=portrait")
            req = urllib.request.Request(url, headers={
                "Authorization": PEXELS_API_KEY,
                "User-Agent": "Mozilla/5.0"
            })
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            photos = data.get("photos", [])
            if photos:
                img_url = photos[0]["src"]["large2x"]
                dl = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(dl, timeout=30) as r2, \
                     open(output_path, "wb") as f:
                    f.write(r2.read())
                print(f"   ✅ Pexels替代封面下载完成")
                return True
        except Exception as e:
            print(f"   ⚠️ Pexels封面也失败：{e}")

    return False


# ── Step 3: Pillow封面渲染 ────────────────────────────────

def create_cover_image(source_img_path: str, cover_title: str,
                       cover_subtitle: str, output_path: str):
    """
    1080x1920 竖屏封面：
    - 居中裁剪原图
    - 调暗60%
    - 底部渐变黑色遮罩
    - 大标题（y≈1100）+ 副标题（y≈1250）
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(source_img_path).convert("RGB")
    tw, th = 1080, 1920

    # 等比缩放后居中裁剪
    scale = max(tw / img.width, th / img.height)
    nw, nh = int(img.width * scale), int(img.height * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top  = (nh - th) // 2
    img  = img.crop((left, top, left + tw, top + th))

    # 调暗60%
    img = img.point(lambda p: int(p * 0.4))

    # 底部渐变遮罩（从 y=960 开始渐变到纯黑）
    overlay = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    grad_start = 960
    for y in range(grad_start, th):
        alpha = int(230 * (y - grad_start) / (th - grad_start))
        draw_ov.line([(0, y), (tw, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # 字体加载（微软雅黑粗体 / 普通，失败则系统默认）
    def load_font(path: str, size: int):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()

    title_font = load_font(r"C:\Windows\Fonts\msyhbd.ttc", 110)
    sub_font   = load_font(r"C:\Windows\Fonts\msyh.ttc",   52)

    draw = ImageDraw.Draw(img)

    # 大标题（左下1/3处，阴影+白色）
    tx, ty = 60, 1100
    draw.text((tx + 3, ty + 3), cover_title, font=title_font, fill=(0, 0, 0))
    draw.text((tx, ty),         cover_title, font=title_font, fill=(255, 255, 255))

    # 副标题
    sx, sy = 62, 1250
    draw.text((sx + 2, sy + 2), cover_subtitle, font=sub_font, fill=(0, 0, 0))
    draw.text((sx, sy),         cover_subtitle, font=sub_font, fill=(210, 210, 210))

    img.save(output_path, quality=95)
    print(f"   ✅ 封面图生成完成")


def make_cover_segment(cover_img_path: str, duration: float, output_path: str):
    """封面图 → 静态视频片段"""
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", cover_img_path,
        "-t", f"{duration:.3f}",
        "-vf", "scale=1080:1920",
        "-r", "30",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-an",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"封面视频生成失败：{r.stderr[-300:]}")


# ── Step 4: TTS 配音 + 字幕（复用 shorts_maker 逻辑）────

async def _tts_generate(text: str, audio_path: str, srt_path: str, voice: str):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    submaker    = edge_tts.SubMaker()
    use_feed    = hasattr(submaker, "feed")

    with open(audio_path, "wb") as af:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                af.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                if use_feed:
                    submaker.feed(chunk)
                else:
                    submaker.create_sub((chunk["offset"], chunk["duration"]), chunk["text"])

    srt_content = (submaker.get_srt() if hasattr(submaker, "get_srt")
                   else submaker.generate_subs(words_in_cue=6))
    with open(srt_path, "w", encoding="utf-8") as sf:
        sf.write(srt_content)
    print("✅ TTS音频和字幕生成完成")


def generate_tts_audio(text: str, audio_path: str, srt_path: str,
                       voice: str = "zh-CN-XiaoyiNeural"):
    log("🎙️ Edge TTS生成中文配音...")
    asyncio.run(_tts_generate(text, audio_path, srt_path, voice))


# ── Step 5: Pexels背景视频（复用 shorts_maker 逻辑）────

def prepare_bg_segment(src_path: str, duration: float, output_path: str):
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", src_path,
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
    list_file = output_path + ".txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in segment_paths:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(list_file)
    if r.returncode != 0:
        raise RuntimeError(f"视频拼接失败：{r.stderr[-500:]}")


# ── Step 6: 背景音乐（Freesound，复用 shorts_maker 逻辑）

def download_music(keyword: str, output_path: str) -> bool:
    if not FREESOUND_API_KEY:
        print("   ℹ️ 未设置 FREESOUND_API_KEY，跳过背景音乐")
        return False

    log(f"🎵 背景音乐：{keyword}")
    search_url = "https://freesound.org/apiv2/search/text/?" + urllib.parse.urlencode({
        "query":     keyword,
        "token":     FREESOUND_API_KEY,
        "fields":    "id,name,duration,previews",
        "filter":    "duration:[30 TO 300]",
        "sort":      "rating_desc",
        "page_size": "10",
    })
    try:
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"   ⚠️ Freesound搜索失败：{e}")
        return False

    results = data.get("results", [])
    if not results:
        print("   ⚠️ 无搜索结果")
        return False

    print(f"   找到 {len(results)} 条结果")
    for sound in results:
        preview_url = sound.get("previews", {}).get("preview-hq-mp3", "")
        if not preview_url:
            continue
        try:
            dl = urllib.request.Request(preview_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(dl, timeout=60) as r, \
                 open(output_path, "wb") as out:
                out.write(r.read())
            print(f"   ✅ 音乐下载完成：{os.path.getsize(output_path)//1024}KB  "
                  f"《{sound.get('name','?')}》")
            return True
        except Exception as e:
            print(f"   ⚠️ 下载失败，跳过：{e}")
    return False


def mix_audio_with_music(tts_path: str, music_path: str, output_path: str):
    """TTS 100% + 背景音乐 40%，以TTS时长为准"""
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
    print("   ✅ 音频混合完成（TTS 100% + 音乐 40%）")


# ── Step 7: 字幕烧录（与 shorts_maker 完全一致）─────────

def _srt_to_ass(srt_path: str, ass_path: str, time_offset_ms: int = 0):
    """
    SRT → ASS 转换。
    time_offset_ms: 所有时间戳整体后移的毫秒数（用于封面无字幕效果）。
    """
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
        "Style: Default,Microsoft YaHei,56,&H00E7FDFF,&H000000FF,"
        "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,0,2,30,30,350,1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    def _t(t: str) -> str:
        """SRT HH:MM:SS,mmm → ASS H:MM:SS.cc，含时间偏移"""
        total_ms = _srt_ms(t) + time_offset_ms
        total_ms = max(0, total_ms)
        h2 = total_ms // 3600000
        m2 = (total_ms % 3600000) // 60000
        s2 = (total_ms % 60000) // 1000
        cs = (total_ms % 1000) // 10
        return f"{h2}:{m2:02d}:{s2:02d}.{cs:02d}"

    def _wrap(text: str, n: int = 10) -> str:
        parts, i = [], 0
        while i < len(text):
            parts.append(text[i:i + n])
            i += n
        return r"\N".join(parts)

    dialogues = []
    for entry in re.split(r"\n{2,}", srt_content.strip()):
        lines = entry.strip().splitlines()
        if len(lines) < 3:
            continue
        timing = lines[1]
        text   = "".join(lines[2:]).strip()
        start  = _t(timing.split("-->")[0])
        end    = _t(timing.split("-->")[1])
        dialogues.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{_wrap(text)}")

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(dialogues) + "\n")


# ── Step 8: 最终视频合成 ─────────────────────────────────

def build_book_video(bg_video_path: str, audio_path: str, srt_path: str,
                     output_path: str, duration: float,
                     subtitle_offset_ms: int = 0):
    """合成：预处理好的背景视频 + 混音 + 字幕烧录"""
    log("🎬 合成最终视频...")
    tmp_ass = "tmp_book_subs.ass"
    _srt_to_ass(srt_path, tmp_ass, time_offset_ms=subtitle_offset_ms)
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", bg_video_path,
            "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-vf", f"ass={tmp_ass}",
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
        for line in result.stderr.splitlines():
            if any(k in line for k in ("subtitle", "Sub", "Error", "Invalid", "No such")):
                print(f"   [ffmpeg] {line}")
        print(f"✅ 视频生成完成：{output_path}")
    finally:
        if os.path.exists(tmp_ass):
            os.unlink(tmp_ass)


# ── 单本书主流程 ─────────────────────────────────────────

def run_book_mode(book_title: str) -> Path:
    log(f"📚 开始生成《{book_title}》书单视频")
    with tempfile.TemporaryDirectory() as tmp:

        # 1. Claude生成脚本
        data         = generate_book_script(book_title)
        script       = data["script"]
        cover_title  = data["cover_title"]
        cover_sub    = data["cover_subtitle"]
        music_kw     = data["music_keyword"]
        visual_mood  = data["visual_mood"]

        # 2. 背景音乐（先下载，不阻塞后续）
        music_path = os.path.join(tmp, "bgmusic.mp3")
        music_ok   = download_music(music_kw, music_path)

        # 3. 获取书籍封面图
        log("📖 获取书籍封面...")
        raw_cover  = os.path.join(tmp, "cover_raw.jpg")
        cover_img  = os.path.join(tmp, "cover.jpg")
        cover_ok   = download_book_cover_image(book_title, raw_cover)
        if cover_ok:
            create_cover_image(raw_cover, cover_title, cover_sub, cover_img)
        else:
            print("   ⚠️ 无封面图，封面段落使用纯黑背景")
            cover_img = None

        # 4. TTS配音 + 字幕
        tts_path = os.path.join(tmp, "tts.mp3")
        srt_path = os.path.join(tmp, "subtitles.srt")
        generate_tts_audio(script, tts_path, srt_path, voice=TTS_VOICE)

        # Fix 3：拆分超长字幕条目（edge-tts 中文常只生成 1 条）
        with open(srt_path, encoding="utf-8") as f:
            raw_srt = f.read()
        split_srt = split_long_srt(raw_srt, max_chars=10)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(split_srt)

        tts_duration = get_audio_duration(tts_path)

        # Fix 1：在 TTS 前插入 COVER_DURATION 秒静音，封面展示期间无语音无字幕
        delayed_tts = os.path.join(tmp, "tts_delayed.mp3")
        prepend_silence(tts_path, COVER_DURATION, delayed_tts)

        # 5. 混合音频（以延迟后的 TTS 为基准）
        final_audio  = delayed_tts
        mixed_path   = os.path.join(tmp, "mixed.mp3")
        if music_ok:
            try:
                mix_audio_with_music(delayed_tts, music_path, mixed_path)
                final_audio = mixed_path
            except Exception as e:
                print(f"   ⚠️ 音频混合失败：{e}，使用纯TTS")

        total_duration = get_audio_duration(final_audio)
        print(f"   TTS时长：{tts_duration:.1f}s  总时长（含封面）：{total_duration:.1f}s")

        # 6. 组合背景视频（Fix 2：3 段不同 Pexels 画面）
        log("🎨 组合背景视频...")
        segments = []

        # 封面静帧（前 COVER_DURATION 秒）
        cover_seg = os.path.join(tmp, "seg_cover.mp4")
        if cover_img:
            make_cover_segment(cover_img, COVER_DURATION, cover_seg)
        else:
            make_black_segment(COVER_DURATION, cover_seg)
        segments.append(cover_seg)

        # Pexels 4 段，按 visual_mood 从固定素材库随机取，自动去重
        pexels_total = max(3.0, tts_duration)
        n_pexels     = 4
        seg_dur_each = pexels_total / n_pexels
        log(f"🎨 下载Pexels背景视频（mood: {visual_mood}）...")

        for i in range(n_pexels):
            raw = os.path.join(tmp, f"pexels_raw_{i}.mp4")
            seg = os.path.join(tmp, f"seg_pexels_{i}.mp4")
            print(f"\n── Pexels 段落 {i+1}/{n_pexels} ──")
            if PEXELS_API_KEY and search_and_download_pexels_by_mood(
                    visual_mood, seg_dur_each, raw, PEXELS_API_KEY):
                prepare_bg_segment(raw, seg_dur_each, seg)
            else:
                make_black_segment(seg_dur_each, seg)
            segments.append(seg)

        bg_full = os.path.join(tmp, "bg_full.mp4")
        concatenate_segments(segments, bg_full)
        print(f"\n   ✅ 背景拼接（封面{COVER_DURATION}s + Pexels {n_pexels}×{seg_dur_each:.1f}s）")

        # 7. 合成最终视频（字幕整体后移 COVER_DURATION 秒，封面段落无字幕）
        safe_name = re.sub(r'[\\/:*?"<>|《》]', '', book_title)[:20]
        idx = 1
        while (OUTPUT_DIR / f"book_{safe_name}_{idx:02d}.mp4").exists():
            idx += 1
        output_file = OUTPUT_DIR / f"book_{safe_name}_{idx:02d}.mp4"

        build_book_video(
            bg_full, final_audio, srt_path, str(output_file),
            duration=total_duration,
            subtitle_offset_ms=int(COVER_DURATION * 1000),
        )

    log(f"🎉 《{book_title}》完成！输出：{output_file.absolute()}")
    return output_file


# ── 主程序 ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Book Maker - 一分钟读一本书短视频生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python book_maker.py \"思考快与慢\"\n"
            "  python book_maker.py \"原则\" \"穷查理宝典\" \"纳瓦尔宝典\""
        )
    )
    parser.add_argument("books", nargs="+", help="书名，支持同时输入多本")
    args = parser.parse_args()

    total   = len(args.books)
    results = []

    for i, title in enumerate(args.books, 1):
        if total > 1:
            print(f"\n{'#'*60}")
            print(f"# 进度 {i}/{total}  ·  《{title}》")
            print(f"{'#'*60}")
        try:
            out = run_book_mode(title)
            results.append((title, str(out), None))
        except Exception as e:
            print(f"❌ 《{title}》生成失败：{e}")
            results.append((title, None, str(e)))

    if total > 1:
        log("📊 批量完成汇总")
        for title, path, err in results:
            if path:
                print(f"   ✅ 《{title}》→ {path}")
            else:
                print(f"   ❌ 《{title}》→ {err}")


if __name__ == "__main__":
    main()
