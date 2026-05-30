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

# 强制UTF-8，避免Windows GBK终端/管道编码错误
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
if sys.stdin.encoding and sys.stdin.encoding.lower() != 'utf-8':
    sys.stdin.reconfigure(encoding='utf-8', errors='replace')

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
FAL_API_KEY       = os.getenv("FAL_API_KEY")
COMFYUI_HOST      = os.getenv("COMFYUI_HOST", "127.0.0.1:8188")
COMFYUI_WORKFLOW  = Path(__file__).parent / "comfyui_workflow.json"

OUTPUT_DIR    = Path("output/book")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

COVER_DURATION = 3          # 封面静帧展示秒数（这段内无字幕，只展示封面）

# ── 视觉风格素材库（与 shorts_maker 共享同一 used_videos.json）──
USED_VIDEOS_PATH = Path("used_videos.json")
ANGLES_PATH      = Path("output/book_angles.json")

VISUAL_MOOD_KEYWORDS = {
    "urban_night": ["city night lights", "night cityscape", "urban night street"],
    "rainy":       ["rain window night", "rainy city street", "rain drops dark"],
    "sunny":       ["morning sunlight", "golden hour city", "sunrise motivation"],
    "minimal":     ["minimal desk workspace", "clean office", "focused work space"],
    "nature":      ["calm nature forest", "peaceful lake", "morning nature walk"],
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


# ── 已用角度追踪 ─────────────────────────────────────────

def _load_angles() -> dict:
    if ANGLES_PATH.exists():
        try:
            return json.loads(ANGLES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_angle(book_title: str, angle: str):
    if not angle:
        return
    data = _load_angles()
    data.setdefault(book_title, [])
    if angle not in data[book_title]:
        data[book_title].append(angle)
    ANGLES_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANGLES_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Goodreads爬虫 ────────────────────────────────────────

def fetch_goodreads_reviews(book_title: str) -> list:
    """
    搜索Goodreads并爬取前15条高赞评论。
    返回：[{"text": "...", "likes": 0}, ...]
    失败时返回空列表，不中断程序。
    """
    try:
        import requests
        from bs4 import BeautifulSoup
    except ImportError:
        print("   ⚠️ 缺少依赖：pip install requests beautifulsoup4")
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Step 1: 搜索书名，取第一个结果
    search_url = "https://www.goodreads.com/search?q=" + urllib.parse.quote(book_title)
    try:
        resp = requests.get(search_url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"   ⚠️ Goodreads搜索失败：{e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    book_link = None
    for a in soup.select("a.bookTitle, a[href*='/book/show/']"):
        href = a.get("href", "")
        if "/book/show/" in href:
            book_link = "https://www.goodreads.com" + href.split("?")[0]
            break

    if not book_link:
        print(f"   ⚠️ Goodreads未找到《{book_title}》")
        return []

    print(f"   → 书籍页面：{book_link}")

    # Step 2: 进入详情页爬评论
    try:
        resp = requests.get(book_link, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"   ⚠️ Goodreads详情页获取失败：{e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    reviews = []

    # 新版页面结构
    for section in soup.select("section.ReviewText, div.reviewText"):
        text_el = section.select_one("span.Formatted, span.readable") or section
        text = text_el.get_text(" ", strip=True)
        if len(text) < 50:
            continue
        likes = 0
        article = section.find_parent("article")
        if article:
            like_btn = article.select_one(
                "button[data-testid='like-count'], span.likesCount"
            )
            if like_btn:
                m = re.search(r"\d+", like_btn.get_text())
                likes = int(m.group()) if m else 0
        reviews.append({"text": text[:1000], "likes": likes})
        if len(reviews) >= 15:
            break

    # 降级：旧版页面结构
    if not reviews:
        for div in soup.select("div.friendReviews, div.review"):
            text_el = div.select_one("span.readable, div.reviewText")
            if not text_el:
                continue
            text = text_el.get_text(" ", strip=True)
            if len(text) < 50:
                continue
            likes = 0
            like_el = div.select_one("span.likesCount")
            if like_el:
                m = re.search(r"\d+", like_el.get_text())
                likes = int(m.group()) if m else 0
            reviews.append({"text": text[:1000], "likes": likes})
            if len(reviews) >= 15:
                break

    if reviews:
        print(f"   ✅ 爬取到 {len(reviews)} 条Goodreads评论")
    else:
        print("   ⚠️ 未能解析评论，请手动粘贴素材")
    return reviews


def _reviews_to_text(reviews_list: list) -> str:
    """将Goodreads评论列表（按点赞排序）转为文本块"""
    sorted_r = sorted(reviews_list, key=lambda r: r.get("likes", 0), reverse=True)
    parts = []
    for i, r in enumerate(sorted_r, 1):
        parts.append(f"[评论{i}] {r['text'].strip()}")
    return "\n\n".join(parts)


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
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
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
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
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

def generate_book_script(book_title: str, reviews: str = "", angle: str = "",
                         lang: str = "zh", style: str = "story") -> dict:
    """
    两步生成：
      步骤一：Claude从评论中筛选最佳角度（可用 --angle 跳过）
      步骤二：Claude严格基于筛选内容写脚本，含质量评分自动重试（最多3次，取最高分）
    返回：{script, angle, hook_type, score, visual_mood, cover_title, cover_subtitle, music_keyword}
    """
    log(f"🤖 Claude生成《{book_title}》书单脚本...")

    reviews_block = reviews.strip()[:4000] if reviews.strip() else "（无评论素材，请根据书名发挥）"
    client = _get_client()

    # ── 步骤一：角度筛选（指定角度时跳过）──────────────────
    used_angles = _load_angles().get(book_title, [])
    used_hint = (f"\n已用过的角度（本次请避免重复）：{', '.join(used_angles)}"
                 if used_angles else "")

    if angle:
        selected_angle = angle
        material_block = reviews_block
        print(f"   📌 使用指定角度：{angle}")
    else:
        print("   🔍 步骤一：Claude筛选最佳角度...")
        angle_prompt = (
            f"你是书单短视频编辑，负责从评论素材中选出最适合拍短视频的角度。\n\n"
            f"书名：《{book_title}》\n"
            f"评论素材：\n{reviews_block}"
            f"{used_hint}\n\n"
            "任务：选出情绪共鸣最强、有具体细节、适合口语表达的1个主角度，"
            "最多保留1个辅助细节。\n"
            "判断标准：情绪共鸣强 / 有具体细节（人名/场景/数字）/ 适合口语表达\n\n"
            "只返回JSON：\n"
            '{"angle":"主角度名称5-10字","core_content":"选中的核心内容（直接引用评论原文）",'
            '"sub_detail":"辅助细节（无则留空）"}'
        )
        msg = _claude_call(client,
            model="claude-opus-4-8",
            max_tokens=400,
            messages=[{"role": "user", "content": angle_prompt}]
        )
        raw_a = re.sub(r"```json|```", "", msg.content[0].text.strip()).strip()
        angle_data = None
        try:
            angle_data = json.loads(raw_a)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', raw_a, re.DOTALL)
            if m:
                try:
                    angle_data = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        selected_angle = angle_data.get("angle", "核心主题") if angle_data else "核心主题"
        core_content   = (angle_data.get("core_content", reviews_block[:500])
                          if angle_data else reviews_block[:500])
        sub_detail     = angle_data.get("sub_detail", "") if angle_data else ""

        print(f"   ✅ 选定角度：{selected_angle}")
        if sub_detail:
            print(f"   辅助细节：{sub_detail[:60]}...")

        material_block = f"主角度：{selected_angle}\n核心内容：{core_content}"
        if sub_detail:
            material_block += f"\n辅助细节：{sub_detail}"

    # ── 步骤二：生成脚本 ─────────────────────────────────
    if style == "quotes":
        prompt = (
            f"你是抖音书单爆款文案编辑。\n"
            f"任务：从《{book_title}》中提取最有情绪共鸣的金句，组合成一段朗读脚本。\n\n"
            f"【爆款规律】\n"
            f"- 不分析，不解释，只朗读原文或高度还原原文的句子\n"
            f"- 每句话都要有独立的情绪冲击力\n"
            f"- 句子之间自然衔接，像在低声倾诉\n"
            f"- 第二人称'你'开头或穿插，制造共鸣感\n"
            f"- 结尾一句留白，意境悠远，不做总结\n\n"
            f"【格式要求】\n"
            f"- 总字数150-220字\n"
            f"- 不用'首先/其次/最后'等分析词\n"
            f"- 不出现书名\n"
            f"- 用，。……等标点制造自然停顿，不用空格断句\n"
            f"- 每句话独立成句，句末用句号或省略号\n\n"
            f"评分标准（1-10）：情绪共鸣度 / 朗读流畅度 / 金句密度 / 意境感\n"
            f"score = 四项平均值\n\n"
            f"【视觉风格，选其一】\n"
            f"urban_night / rainy / sunny / minimal / nature\n\n"
            f"只返回JSON，禁止其他内容：\n"
            f'{{"script":"脚本正文150-220字","angle":"本次金句主题3-6字",'
            f'"hook_type":"情感类型","score":9.0,'
            f'"visual_mood":"rainy","cover_title":"封面金句标题10字以内",'
            f'"cover_subtitle":"副标题15字以内，意境感强",'
            f'"music_keyword":"背景音乐英文关键词，轻柔治愈类",'
            f'"cover_keyword":"封面Pexels搜索词2-4个英文单词，温暖治愈意象，禁用book/reading",'
            f'"bg_scenes":["背景图1英文描述5-8词，只能是城市街景/自然风光/建筑外景/天空/海洋/森林/雨景/雾景/剪影远景，严禁任何具体物品（家具/器具等），必须wide shot或distant silhouette","背景图2","背景图3","背景图4","背景图5","背景图6","背景图7"],'
            f'"book_name_zh":"书的正式中文名",'
            f'"title":"发布标题，直接用书中最打动人的一句金句，20字以内",'
            f'"caption":"发布文案2-3句，金句风格，有共鸣感，结尾留白或反问"}}'
        )
    elif lang == "en":
        prompt = (
            f"You are a YouTube Shorts book video script editor.\n"
            f"Your job: turn the selected review material into a natural, spoken English script.\n\n"
            f"Book: {book_title}\n\n"
            f"Selected material:\n{material_block}\n\n"
            "HARD RULES — violating any = automatic retry:\n"
            "- Script content must come 100% from the material above. No invented details.\n"
            "- Natural spoken English. No stiff or academic tone.\n\n"
            "SCRIPT STRUCTURE:\n"
            "① Hook (15-25 words): Open with a specific, relatable daily-life moment "
            "directly tied to the book's core theme. Cold, precise, cinematic.\n"
            "   Don't start with 'This book is about' or 'The author says'.\n"
            "② Core (120-160 words): One central insight from the material, grounded in "
            "a specific scene or detail from the book. Must explain why the hook happens.\n"
            "③ Closing (10-20 words): One line that connects the book back to the viewer's life.\n"
            "   No 'go read this book' or 'highly recommend'.\n\n"
            "FORMAT:\n"
            "- Total: 150-220 words\n"
            "- Max 12 words per sentence\n"
            "- Conversational. Like a friend who read it.\n"
            "- No markdown symbols\n"
            "- No 'firstly/secondly/finally/in conclusion'\n\n"
            "Self-score each (1-10): relatability / source fidelity / natural speech / insight value\n"
            "score = average\n\n"
            "Visual mood (pick one): urban_night / rainy / sunny / minimal / nature\n\n"
            "Return JSON only:\n"
            '{"script":"150-220 word English script","angle":"angle in 3-6 words",'
            '"hook_type":"hook scene type","score":9.0,'
            '"visual_mood":"rainy","cover_title":"hook title max 6 words",'
            '"cover_subtitle":"curiosity subtitle max 10 words","music_keyword":"background music keywords",'
            '"cover_keyword":"2-4 English words for Pexels cover image, reflects book theme, no book/reading",'
            '"bg_scenes":["scene 1: 5-8 words, ONLY cityscape/nature/architecture exterior/sky/ocean/forest/rain/fog/distant silhouette, strictly NO specific objects (furniture/props/food etc), must be wide shot or distant silhouette","scene 2","scene 3","scene 4","scene 5","scene 6","scene 7"],'
            '"book_name_en":"official English title of the book",'
            '"caption":"2-3 sentence post caption for social media. Punchy, no hashtags, ends with a question or provocation.",'
            '"title":"video post title, max 20 characters, hook-style, creates curiosity or emotion"}'
        )
    else:
        prompt = (
        f"你是抖音书单短视频脚本编辑，不是作者。\n"
        f"任务是把选好的评论素材剪辑重组成流畅的中文口语脚本，禁止补充任何素材之外的内容。\n\n"
        f"【书名】《{book_title}》\n\n"
        f"【已筛选的素材】\n{material_block}\n\n"
        "【硬性规则——违反即视为失败，自动重试】\n"
        "- 脚本内容100%来自选中的素材，禁止使用素材之外的任何情节、细节、数据、评价\n"
        "- 翻译要是人话，口语化，不能有翻译腔\n\n"
        "【统一脚本结构：生活场景入口 → 书中核心 → 拉回现实认知】\n\n"
        "① 开头（20-30字）：从读者今天可能经历的一个具体生活场景切入。\n"
        "   场景必须和书的核心主题直接相关，不是随便找个场景。\n"
        "   风格：像纪录片旁白，冷静、精准、有画面感。\n"
        "   禁止用「这本书讲了」「作者说」「很多人」开头。\n"
        "   好的例子方向（不要照搬，根据这本书自己想）：\n"
        "   - 你有没有听过很多道理，但还是过不好这一生。\n"
        "   - 你发消息的时候，会不会先想一下，谁可能在看。\n"
        "   - 你有没有某一刻，突然觉得自己做的所有事，都不是自己真正想要的。\n\n"
        "② 中间（180-250字）：严格基于筛选的素材提炼1个核心观点，\n"
        "   用书中具体情节或细节承载，禁止补充素材外的内容。\n"
        "   观点要能解释为什么开头那个生活场景会发生。\n"
        "   每个细节必须有画面，不能说「改变了心态」，要说具体发生了什么。\n\n"
        "③ 结尾（15-25字）：把书的认知拉回读者现实，\n"
        "   一句话让人觉得这本书跟自己有关。\n"
        "   禁止说「这本书值得一读」「赶快去看」。\n\n"
        "【格式规则】\n"
        "- 总字数：230-350字（禁止为凑字数加废话）\n"
        "- 每句不超过15个字\n"
        "- 口语化，像读过这本书的朋友在说话\n"
        "- 禁止：首先/其次/最后/总结/其实/说真的/你想想\n"
        "- 禁止：99%的人/没人告诉你/你知道吗 类套路句\n"
        "- 禁止任何Markdown符号\n"
        "- 禁止出现「评论」「读者」等字样\n\n"
        "【写完后严格自评，不要放水】\n"
        "- 生活共鸣度（1-10）：开头场景是否真的击中日常\n"
        "- 素材忠实度（1-10）：内容是否100%来自素材，没有自己补充\n"
        "- 口语化程度（1-10）：是否真的像朋友聊天，没有书面语\n"
        "- 认知价值（1-10）：听完是否觉得学到了什么或想去看书\n"
        "score = 四项平均值\n\n"
        "【视觉风格，选其一】\n"
        "- urban_night：励志/突破/成就类\n"
        "- rainy：思考/哲学/文学/历史类\n"
        "- sunny：积极/能量/成长类\n"
        "- minimal：效率/商业/简约类\n"
        "- nature：平静/哲学/自然类\n\n"
        "只返回JSON，禁止其他任何内容：\n"
        '{"script":"脚本正文230-350字","angle":"本次使用的角度5-10字",'
        '"hook_type":"生活场景入口","score":9.0,'
        '"visual_mood":"rainy","cover_title":"封面钩子标题8字以内",'
        '"cover_subtitle":"引发好奇15字以内","music_keyword":"背景音乐英文关键词",'
        '"cover_keyword":"封面Pexels搜索词2-4个英文单词，体现书的核心主题或意象，禁用book/reading",'
        '"bg_scenes":["背景图1英文描述5-8词，只能是城市街景/自然风光/建筑外景/天空/海洋/森林/雨景/雾景/剪影远景，严禁任何具体物品（家具/器具等），必须wide shot或distant silhouette","背景图2","背景图3","背景图4","背景图5","背景图6","背景图7"],'
        '"book_name_zh":"书的正式中文名，如乌合之众/理想国/悉达多，非角度标题",'
        '"caption":"发布到抖音/小红书的配文，2-3句话，口语化，有共鸣感，结尾用疑问或反问收尾，不加话题标签",'
        '"title":"视频发布标题，20字以内，钩子感强，制造好奇或情绪共鸣，不加书名"}'
        )  # end else

    best = None
    for attempt in range(3):
        msg = _claude_call(client,
            model="claude-opus-4-8",
            max_tokens=1200,
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

        score     = float(data.get("score", 0))
        hook_type = data.get("hook_type", "?")
        print(f"   第{attempt+1}次 → 评分{score:.1f}/10  hook:{hook_type}  字数:{len(data['script'])}")

        if best is None or score > float(best.get("score", 0)):
            best = data

        if score >= 8.0:
            print(f"   ✅ 达标（≥8分），采用此版本")
            break

    if not best:
        raise RuntimeError("Claude脚本生成失败，3次均无法解析")

    for key in ("script", "cover_title", "cover_subtitle", "music_keyword"):
        if key not in best:
            raise RuntimeError(f"Claude返回缺少字段：{key}")
    # cover_keyword 可选，缺失时用 visual_mood 关键词兜底
    if not best.get("cover_keyword"):
        best["cover_keyword"] = best.get("visual_mood", "contemplative")
    # 统一书名字段：英文用 book_name_en，中文用 book_name_zh
    if lang == "en":
        best["book_name_display"] = best.get("book_name_en") or book_title
    else:
        best["book_name_display"] = best.get("book_name_zh") or best.get("cover_title", book_title)
    best["lang"] = lang

    visual_mood = best.get("visual_mood", "rainy")
    if visual_mood not in VISUAL_MOOD_KEYWORDS:
        visual_mood = "rainy"
    best["visual_mood"] = visual_mood

    # 字数截断（520字上限，向前找句末）
    script = best["script"]
    if len(script) > 520:
        cut = None
        for i in range(519, max(230, 0) - 1, -1):
            if script[i] in "。！？":
                cut = i + 1
                break
        if cut is None:
            for i in range(520, min(560, len(script))):
                if script[i] in "。！？":
                    cut = i + 1
                    break
        best["script"] = script[:cut] if cut else script[:520]

    # 记录已用角度
    final_angle = best.get("angle") or selected_angle
    best["angle"] = final_angle
    _save_angle(book_title, final_angle)

    best_score = float(best.get("score", 0))
    print(f"✅ 脚本完成（最高分{best_score:.1f}/10），共{len(best['script'])}字")
    print(f"   角度：{final_angle}")
    print(f"   Hook类型：{best.get('hook_type', '?')}")
    print(f"   封面标题：{best['cover_title']}")
    print(f"   封面副标题：{best['cover_subtitle']}")
    print(f"   音乐关键词：{best['music_keyword']}")
    print(f"   视觉风格：{best['visual_mood']}")
    print(f"   脚本预览：{best['script'][:80]}...")
    return best


# ── ComfyUI 本地生图 ────────────────────────────────────────

def _comfyui_is_running() -> bool:
    """检查 ComfyUI 是否在跑"""
    try:
        import urllib.request as _ur
        _ur.urlopen(f"http://{COMFYUI_HOST}/system_stats", timeout=2)
        return True
    except Exception:
        return False


def generate_comfyui_image(prompt_text: str, output_path: str) -> bool:
    """
    调 ComfyUI API 生成图片。
    需要 comfyui_workflow.json（从 ComfyUI UI 里 Ctrl+Shift+S 导出的 API 格式）。
    workflow 里用 __PROMPT__ 占位符标记正向提示词节点的 text 字段。
    """
    if not COMFYUI_WORKFLOW.exists():
        return False
    if not _comfyui_is_running():
        return False

    import urllib.request as _ur
    import time, uuid, json as _json

    workflow = _json.loads(COMFYUI_WORKFLOW.read_text(encoding="utf-8"))

    # 过滤掉注释 key（_comment, _setup 等），ComfyUI 只认纯数字节点 ID
    workflow = {k: v for k, v in workflow.items() if not k.startswith("_")}

    # 替换 prompt 占位符
    wf_str = _json.dumps(workflow).replace("__PROMPT__", prompt_text.replace('"', '\\"'))
    workflow = _json.loads(wf_str)

    # 每次随机 seed，避免所有图片相同
    import random as _random
    for node in workflow.values():
        if isinstance(node, dict) and "inputs" in node:
            if "seed" in node["inputs"]:
                node["inputs"]["seed"] = _random.randint(0, 2**32 - 1)

    client_id = str(uuid.uuid4())
    payload = _json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"http://{COMFYUI_HOST}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        prompt_id = _json.loads(r.read())["prompt_id"]

    # 轮询直到完成（最多等 120 秒）
    for _ in range(120):
        time.sleep(1)
        try:
            with urllib.request.urlopen(
                f"http://{COMFYUI_HOST}/history/{prompt_id}", timeout=5
            ) as r:
                history = _json.loads(r.read())
        except Exception:
            continue
        if prompt_id not in history:
            continue
        outputs = history[prompt_id].get("outputs", {})
        for node_output in outputs.values():
            for img in node_output.get("images", []):
                params = urllib.parse.urlencode({
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                })
                with urllib.request.urlopen(
                    f"http://{COMFYUI_HOST}/view?{params}", timeout=30
                ) as r2, open(output_path, "wb") as f:
                    f.write(r2.read())
                if os.path.getsize(output_path) > 5000:
                    return True
    return False


# ── Step 2: 获取书籍封面图 ────────────────────────────────

def download_book_cover_image(book_title: str, output_path: str,
                              cover_keyword: str = "") -> bool:
    """优先 Google Books API，失败则 Pexels 用 cover_keyword 主题图"""
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

    # 第二步：ComfyUI 本地生成（零成本，优先于 fal.ai）
    if COMFYUI_WORKFLOW.exists():
        try:
            prompt_text = (
                f"{cover_keyword or book_title}, "
                "cinematic photography, dramatic moody lighting, "
                "emotional atmosphere, dark aesthetic, "
                "sharp focus, professional color grading, "
                "no text, no watermark, vertical 9:16 portrait"
            )
            if generate_comfyui_image(prompt_text, output_path):
                if os.path.getsize(output_path) > 5000:
                    print(f"   ✅ ComfyUI 本地封面生成完成")
                    return True
        except Exception as e:
            print(f"   ⚠️ ComfyUI 失败：{e}")

    # 第三步：fal.ai 生成油画风插画底图
    if FAL_API_KEY:
        try:
            import fal_client
            os.environ["FAL_KEY"] = FAL_API_KEY
            prompt = (
                f"{cover_keyword or book_title}, "
                "cinematic photography, dramatic moody lighting, "
                "emotional atmosphere, dark aesthetic, "
                "sharp focus, professional color grading, "
                "no text, no watermark, vertical 9:16 portrait"
            )
            result = fal_client.run(
                "fal-ai/flux-pro/v1.1",
                arguments={
                    "prompt": prompt,
                    "image_size": {"width": 1080, "height": 1920},
                    "num_images": 1,
                },
            )
            img_url = result["images"][0]["url"]
            dl = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(dl, timeout=60) as r2, \
                 open(output_path, "wb") as f:
                f.write(r2.read())
            if os.path.getsize(output_path) > 5000:
                print(f"   ✅ fal.ai 插画封面生成完成")
                return True
        except Exception as e:
            print(f"   ⚠️ fal.ai 失败：{e}")

    # 第三步降级：Pexels 主题图片搜索
    if PEXELS_API_KEY:
        try:
            import urllib.parse as _up
            pexels_q = _up.quote(cover_keyword or "contemplative dark mood")
            url = (f"https://api.pexels.com/v1/search"
                   f"?query={pexels_q}&per_page=10&orientation=portrait")
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
                       cover_subtitle: str, output_path: str,
                       book_title: str = "", lang: str = "zh"):
    """
    1080x1920 竖屏封面：
    - 居中裁剪原图，调暗60%
    - 底部渐变黑色遮罩
    - 大标题（自动换行+自适应字号）
    - 副标题
    - 书名（底部金色小字）
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

    # 底部渐变遮罩
    overlay = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    grad_start = 880
    for y in range(grad_start, th):
        alpha = int(240 * (y - grad_start) / (th - grad_start))
        draw_ov.line([(0, y), (tw, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # 字体加载
    def load_font(path: str, size: int):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()

    sub_font      = load_font(r"C:\Windows\Fonts\msyh.ttc",   48)
    bookname_font = load_font(r"C:\Windows\Fonts\msyhbd.ttc", 44)
    draw = ImageDraw.Draw(img)

    # ── 大标题：自动换行 + 自适应字号 ──────────────────────
    def wrap_title(text: str, font_path: str, max_w: int, start_size: int, min_size: int):
        """返回 (lines, font)，保证每行宽度 ≤ max_w"""
        for size in range(start_size, min_size - 1, -5):
            fnt = load_font(font_path, size)
            # 按空格（英文）或每字（中文）拆词
            words = text.split(" ") if " " in text else list(text)
            lines, cur = [], ""
            for w in words:
                sep = " " if " " in text else ""
                test = (cur + sep + w).strip()
                bbox = draw.textbbox((0, 0), test, font=fnt)
                if bbox[2] <= max_w:
                    cur = test
                else:
                    if cur:
                        lines.append(cur)
                    cur = w
            if cur:
                lines.append(cur)
            # 最多 3 行，且最长行不超宽
            if len(lines) <= 3:
                return lines, fnt
        # 兜底：最小字号强制返回
        fnt = load_font(font_path, min_size)
        return [text], fnt

    max_title_w = tw - 120  # 左右各留 60px
    lines, title_font = wrap_title(cover_title,
                                   r"C:\Windows\Fonts\msyhbd.ttc",
                                   max_title_w, 100, 60)
    line_h = title_font.size + 12
    ty = 1030
    for line in lines:
        draw.text((63, ty + 3), line, font=title_font, fill=(0, 0, 0))
        draw.text((60, ty),     line, font=title_font, fill=(255, 255, 255))
        ty += line_h

    # 副标题（标题占多行时向下偏移；自动换行避免右侧截断）
    sy = max(ty + 20, 1195)
    sub_lines, sub_fnt = wrap_title(cover_subtitle,
                                    r"C:\Windows\Fonts\msyh.ttc",
                                    max_title_w, 48, 36)
    sub_lh = sub_fnt.size + 10
    for sline in sub_lines:
        draw.text((64, sy + 2), sline, font=sub_fnt, fill=(0, 0, 0))
        draw.text((62, sy),     sline, font=sub_fnt, fill=(200, 200, 200))
        sy += sub_lh

    # 书名（金色）：英文用引号，中文用书名号
    if book_title:
        label = f'"{book_title}"' if lang == "en" else f"《{book_title}》"
        by = max(sy + 80, 1380)
        draw.text((64, by + 2), label, font=bookname_font, fill=(0, 0, 0))
        draw.text((62, by),     label, font=bookname_font, fill=(212, 175, 55))

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
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError(f"封面视频生成失败：{r.stderr[-300:]}")


def make_kenburns_video(img_path: str, duration: float, output_path: str):
    """单张图片 + Ken Burns 慢放大效果 → 背景视频（替代 Pexels 拼接）"""
    fps    = 30
    frames = max(1, int(duration * fps))
    # 先 4x 放大图片（4320x7680），给 zoompan 足够的像素缓冲
    # 避免 zoompan 整数舍入导致的每帧颤动（jitter）
    filter_v = (
        f"scale=4320:7680,"
        f"zoompan=z='min(zoom+0.0006,1.25)':d={frames}:fps={fps}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920"
    )
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", img_path,
        "-vf", filter_v,
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError(f"Ken Burns 生成失败：{r.stderr[-300:]}")


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


_cosyvoice_model = None  # 全局缓存，避免重复加载模型


def _preprocess_tts_text(text: str) -> str:
    """把4位年份数字（1000-2099）转成逐字中文读法，避免读成"一千九百..."."""
    digit_map = str.maketrans('0123456789', '零一二三四五六七八九')
    return re.sub(r'\b[12][0-9]{3}\b', lambda m: m.group(0).translate(digit_map), text)


def _load_cosyvoice_model(zero_shot: bool = False):
    """按需加载 SFT 或 zero-shot 模型（各自缓存）"""
    global _cosyvoice_model
    cosyvoice_dir = r"C:\ai-tools\CosyVoice"
    matcha_dir    = os.path.join(cosyvoice_dir, "third_party", "Matcha-TTS")
    if cosyvoice_dir not in sys.path:
        sys.path.insert(0, matcha_dir)
        sys.path.insert(0, cosyvoice_dir)
    if _cosyvoice_model is None:
        from cosyvoice.cli.cosyvoice import CosyVoice2, CosyVoice
        cosyvoice2_dir = os.path.join(cosyvoice_dir, "pretrained_models", "CosyVoice2-0.5B")
        sft_dir        = os.path.join(cosyvoice_dir, "pretrained_models", "CosyVoice-300M-SFT")
        if zero_shot and os.path.isdir(cosyvoice2_dir):
            print("   首次加载 CosyVoice2-0.5B 模型（零样本克隆）...")
            _cosyvoice_model = CosyVoice2(cosyvoice2_dir)
        else:
            print("   首次加载 CosyVoice-300M-SFT 模型...")
            _cosyvoice_model = CosyVoice(sft_dir)
    return _cosyvoice_model


def generate_cosyvoice_tts(text: str, audio_path: str, srt_path: str,
                            spk: str = "中文女", speed: float = 1.0,
                            voice_profile: dict = None):
    """
    CosyVoice TTS：
      voice_profile=None  → SFT 内置音色（spk参数）
      voice_profile={...} → zero-shot 声音克隆（用户自录音）
    """
    global _cosyvoice_model
    text = _preprocess_tts_text(text)

    import torchaudio, torch

    tmp_wav = audio_path.replace(".mp3", "_cv.wav")
    chunks  = []

    if voice_profile:
        # ── Zero-shot 声音克隆（CosyVoice2 需要传文件路径）──
        _cosyvoice_model = None          # 清除 SFT 缓存，重新加载
        model = _load_cosyvoice_model(zero_shot=True)
        prompt_text = voice_profile["prompt_text"]
        ref_wav     = voice_profile["ref_wav"]
        for chunk in model.inference_zero_shot(
                text, prompt_text, ref_wav, stream=False):
            chunks.append(chunk["tts_speech"])
    else:
        # ── SFT 内置音色 ────────────────────────────────
        model = _load_cosyvoice_model(zero_shot=False)
        for chunk in model.inference_sft(text, spk, stream=False):
            chunks.append(chunk["tts_speech"])

    combined = torch.cat(chunks, dim=1)
    torchaudio.save(tmp_wav, combined, model.sample_rate)

    # 转换为 MP3（speed<1.0 时用 atempo 减速，制造呼吸感）
    if abs(speed - 1.0) > 0.01:
        af = f"atempo={speed:.2f}"
        cmd = ["ffmpeg", "-y", "-i", tmp_wav, "-af", af, "-ar", "24000", "-ab", "128k", audio_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", tmp_wav, "-ar", "24000", "-ab", "128k", audio_path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    os.unlink(tmp_wav)
    if r.returncode != 0:
        raise RuntimeError(f"CosyVoice WAV→MP3 失败: {r.stderr[-300:]}")

    # 生成 SRT：单条覆盖全文，由后续 split_long_srt 切割
    # 把脚本内的换行压成空格，防止 \n\n 被误解析为多条 SRT 条目
    clean_text = re.sub(r'\s*\n\s*', ' ', text).strip()
    dur_ms = int(get_audio_duration(audio_path) * 1000)
    srt_content = f"1\n{_ms_srt(0)} --> {_ms_srt(dur_ms)}\n{clean_text}\n"
    with open(srt_path, "w", encoding="utf-8") as sf:
        sf.write(srt_content)
    print("✅ CosyVoice2 TTS音频和字幕生成完成")


def generate_tts_audio(text: str, audio_path: str, srt_path: str,
                       voice: str = "zh-CN-XiaoxiaoNeural", lang: str = "zh",
                       speed: float = 1.0, voice_profile: dict = None):
    if lang == "en":
        log("🎙️ Edge TTS生成英文配音（Guy）...")
        asyncio.run(_tts_generate(text, audio_path, srt_path, "en-US-GuyNeural"))
    else:
        try:
            if voice_profile:
                log(f"🎙️ CosyVoice2 零样本克隆（{voice_profile['name']}）...")
            else:
                log("🎙️ CosyVoice2生成中文配音...")
            generate_cosyvoice_tts(text, audio_path, srt_path,
                                   speed=speed, voice_profile=voice_profile)
        except Exception as e:
            print(f"   ⚠️ CosyVoice2失败（{e}），降级到Edge TTS")
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
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
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
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
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
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
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


def mix_audio_with_music(tts_path: str, music_path: str, output_path: str,
                         tail: float = 1.5):
    """TTS 100% + 背景音乐 30%；末尾加 tail 秒静音 + 音乐淡出，避免戛然而止"""
    tts_dur = get_audio_duration(tts_path)
    fade_start = tts_dur  # 人声结束时开始淡出
    filter_complex = (
        f"[0:a]apad=pad_dur={tail}[tts];"
        f"[1:a]volume=0.30[bg];"
        f"[tts][bg]amix=inputs=2:duration=first:normalize=0[mix];"
        f"[mix]afade=t=out:st={fade_start:.2f}:d={tail:.2f}[out]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", tts_path,
        "-stream_loop", "-1", "-i", music_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError(f"音频混合失败：{r.stderr[-400:]}")
    print(f"   ✅ 音频混合完成（TTS 100% + 音乐 30%，末尾 {tail}s 淡出）")


# ── Step 7: 字幕烧录（与 shorts_maker 完全一致）─────────

def _srt_to_ass(srt_path: str, ass_path: str, time_offset_ms: int = 0,
                lang: str = "zh"):
    """
    SRT → ASS 转换。
    time_offset_ms: 所有时间戳整体后移的毫秒数（用于封面无字幕效果）。
    lang: zh=中文按字切，en=英文按词切不断词。
    """
    with open(srt_path, encoding="utf-8") as f:
        srt_content = f.read()

    # 英文用 Arial，中文用微软雅黑
    fontname = "Arial" if lang == "en" else "Microsoft YaHei"
    fontsize = 52    if lang == "en" else 56

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 1\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
        "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,"
        "ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
        "Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,{fontname},{fontsize},&H00FFFFFF,&H000000FF,"
        "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,1,2,60,60,260,1\n\n"
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

    def _wrap(text: str) -> str:
        if lang == "en":
            # 按空格切词，每行最多 ~32 字符，最多 2 行，不断词
            words = text.split()
            lines, cur = [], ""
            for word in words:
                test = (cur + " " + word).strip()
                if len(test) > 32 and cur:
                    lines.append(cur)
                    cur = word
                else:
                    cur = test
            if cur:
                lines.append(cur)
            return r"\N".join(lines[:2])
        else:
            # 中文按字数切，每行 10 字
            parts, i = [], 0
            while i < len(text):
                parts.append(text[i:i + 10])
                i += 10
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
                     subtitle_offset_ms: int = 0, lang: str = "zh"):
    """合成：预处理好的背景视频 + 混音 + 字幕烧录"""
    log("🎬 合成最终视频...")
    tmp_ass = "tmp_book_subs.ass"
    _srt_to_ass(srt_path, tmp_ass, time_offset_ms=subtitle_offset_ms, lang=lang)
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
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
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

def run_book_mode(book_title: str, reviews: str = "", angle: str = "",
                  lang: str = "zh", style: str = "story",
                  voice_profile: dict = None) -> Path:
    log(f"📚 开始生成《{book_title}》书单视频")
    with tempfile.TemporaryDirectory() as tmp:

        # 1. Claude生成脚本
        data         = generate_book_script(book_title, reviews, angle, lang=lang, style=style)
        script       = data["script"]
        cover_title  = data["cover_title"]
        cover_sub    = data["cover_subtitle"]
        visual_mood  = data["visual_mood"]
        cover_kw     = data["cover_keyword"]
        book_name_display = data["book_name_display"]

        # 3. 获取书籍封面图（主题关键词搜索，底部显示 cover_title 而非搜索用书名）
        log("📖 获取书籍封面...")
        raw_cover  = os.path.join(tmp, "cover_raw.jpg")
        cover_img  = os.path.join(tmp, "cover.jpg")
        cover_ok   = download_book_cover_image(book_title, raw_cover, cover_keyword=cover_kw)
        if cover_ok:
            create_cover_image(raw_cover, cover_title, cover_sub, cover_img,
                               book_title=book_name_display, lang=lang)
        else:
            print("   ⚠️ 无封面图，封面段落使用纯黑背景")
            cover_img = None

        # 4. TTS配音 + 字幕
        tts_path = os.path.join(tmp, "tts.mp3")
        srt_path = os.path.join(tmp, "subtitles.srt")
        tts_speed = 0.80 if style == "quotes" else 1.0
        generate_tts_audio(script, tts_path, srt_path, lang=lang,
                           speed=tts_speed, voice_profile=voice_profile)

        # Fix 3：拆分超长字幕条目（edge-tts 中文常只生成 1 条）
        with open(srt_path, encoding="utf-8") as f:
            raw_srt = f.read()
        split_srt = split_long_srt(raw_srt, max_chars=40 if lang == "en" else 10)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(split_srt)

        tts_duration = get_audio_duration(tts_path)

        # Fix 1：在 TTS 前插入 COVER_DURATION 秒静音，封面展示期间无语音无字幕
        delayed_tts = os.path.join(tmp, "tts_delayed.mp3")
        prepend_silence(tts_path, COVER_DURATION, delayed_tts)

        # 5. 最终音频（纯TTS，无背景音乐，由平台配乐）
        final_audio  = delayed_tts

        total_duration = get_audio_duration(final_audio)
        print(f"   TTS时长：{tts_duration:.1f}s  总时长（含封面）：{total_duration:.1f}s")

        # 6. 组合背景视频
        log("🎨 组合背景视频...")
        segments = []

        # 封面静帧（前 COVER_DURATION 秒）
        cover_seg = os.path.join(tmp, "seg_cover.mp4")
        if cover_img:
            make_cover_segment(cover_img, COVER_DURATION, cover_seg)
        else:
            make_black_segment(COVER_DURATION, cover_seg)
        segments.append(cover_seg)

        # 正文背景：优先多张 Ken Burns（每~9s 一张 AI 插画），降级 Pexels
        body_dur     = max(3.0, tts_duration + 1.5)  # +1.5 对齐音频淡出
        body_seg     = os.path.join(tmp, "seg_body.mp4")
        use_comfyui_bg = COMFYUI_WORKFLOW.exists() and _comfyui_is_running()
        use_kenburns   = use_comfyui_bg or bool(FAL_API_KEY)
        n_imgs         = 0

        if use_kenburns:
            log("🎨 Ken Burns 背景（生成多张 AI 油画）...")
            try:
                # 每张图用不同的场景描述，Claude 在脚本里已提供 bg_scenes
                bg_scenes_raw = data.get("bg_scenes", [])
                style_suffix  = (
                    "cinematic photography, dramatic moody lighting, "
                    "wide establishing shot, atmospheric depth, "
                    "dark muted color palette, professional color grading, "
                    "sharp focus, no text, no watermark, "
                    "no close-up faces, vertical 9:16 portrait"
                )

                n_imgs  = max(1, round(body_dur / 9))  # 每~9秒一张
                seg_dur = body_dur / n_imgs
                kb_segs = []
                for i in range(n_imgs):
                    print(f"\n── 插画 {i+1}/{n_imgs} ──")
                    img_path = os.path.join(tmp, f"bg_{i}.jpg")

                    # 轮流使用不同场景描述；不足时用 cover_kw 兜底
                    if bg_scenes_raw and i < len(bg_scenes_raw):
                        scene_kw = bg_scenes_raw[i]
                    else:
                        scene_kw = cover_kw or book_title
                    bg_prompt = f"{scene_kw}, {style_suffix}"
                    print(f"   画面：{scene_kw}")

                    # 优先 ComfyUI（本地免费），降级 fal.ai
                    generated = False
                    if use_comfyui_bg:
                        generated = generate_comfyui_image(bg_prompt, img_path)
                        if generated:
                            print(f"   ✅ ComfyUI 生成")
                    if not generated and FAL_API_KEY:
                        import fal_client
                        os.environ["FAL_KEY"] = FAL_API_KEY
                        result = fal_client.run(
                            "fal-ai/flux-pro/v1.1",
                            arguments={
                                "prompt": bg_prompt,
                                "image_size": {"width": 1080, "height": 1920},
                                "num_images": 1,
                            }
                        )
                        img_url = result["images"][0]["url"]
                        dl = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(dl, timeout=60) as r2, \
                             open(img_path, "wb") as f:
                            f.write(r2.read())
                        generated = True
                        print(f"   ✅ fal.ai 生成")

                    if not generated:
                        raise RuntimeError("ComfyUI 和 fal.ai 均失败")
                    kb_path = os.path.join(tmp, f"seg_kb_{i}.mp4")
                    make_kenburns_video(img_path, seg_dur, kb_path)
                    kb_segs.append(kb_path)
                    print(f"   ✅ 插画 {i+1}/{n_imgs} 完成（{seg_dur:.1f}s）")
                concatenate_segments(kb_segs, body_seg)
                segments.append(body_seg)
                print(f"\n   ✅ Ken Burns 背景完成（{n_imgs}张 × {seg_dur:.1f}s）")
            except Exception as e:
                print(f"   ⚠️ Ken Burns 失败：{e}，降级到 Pexels")
                use_kenburns = False

        if not use_kenburns:
            n_pexels     = 4
            seg_dur_each = body_dur / n_pexels
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
        bg_mode = f"Ken Burns {n_imgs}张" if use_kenburns else "Pexels 4段"
        print(f"\n   ✅ 背景拼接（封面{COVER_DURATION}s + {bg_mode} {body_dur:.1f}s）")

        # 7. 合成最终视频（字幕整体后移 COVER_DURATION 秒，封面段落无字幕）
        safe_name = re.sub(r'[\\/:*?"<>|《》]', '', book_title)[:40]
        idx = 1
        while (OUTPUT_DIR / f"book_{safe_name}_{idx:02d}.mp4").exists():
            idx += 1
        output_file = OUTPUT_DIR / f"book_{safe_name}_{idx:02d}.mp4"

        build_book_video(
            bg_full, final_audio, srt_path, str(output_file),
            duration=total_duration,
            subtitle_offset_ms=int(COVER_DURATION * 1000),
            lang=lang,
        )

        # 8. 保存发布标题+文案
        title   = data.get("title", "")
        caption = data.get("caption", "")
        if title or caption:
            caption_file = output_file.with_suffix(".txt")
            with open(caption_file, "w", encoding="utf-8") as cf:
                if title:
                    cf.write(f"【标题】{title}\n\n")
                if caption:
                    cf.write(f"【文案】{caption}\n")
            if title:
                print(f"   🏷️  发布标题：{title}")
            if caption:
                print(f"   📝 发布文案：{caption}")

    log(f"🎉 《{book_title}》完成！输出：{output_file.absolute()}")
    return output_file


# ── 交互式评论收集 ───────────────────────────────────────

def collect_reviews(book_title: str) -> tuple:
    """提示用户输入作者名和豆瓣评论素材，返回 (author, reviews)"""
    print(f"\n{'─'*60}")
    try:
        author = input(f"  《{book_title}》作者名（可选，直接回车跳过）：").strip()
    except EOFError:
        author = ""
    print(f"  请粘贴豆瓣评论素材，输入 END 结束：")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    reviews = "\n".join(lines).strip()
    char_count = len(reviews)
    print(f"  ✅ 收到评论素材 {char_count} 字{'（将使用前3000字）' if char_count > 3000 else ''}")
    print(f"{'─'*60}")
    return author, reviews


# ── 主程序 ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Book Maker - 书单短视频生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python book_maker.py \"活着\"                  # 自动爬Goodreads评论\n"
            "  python book_maker.py \"活着\" --manual          # 手动粘贴评论素材\n"
            "  python book_maker.py \"活着\" --angle \"有庆之死\" # 指定角度生成\n"
            "  python book_maker.py \"活着\" \"万历十五年\"       # 批量生成"
        )
    )
    parser.add_argument("books", nargs="+", help="书名，支持同时输入多本")
    parser.add_argument("--manual", action="store_true",
                        help="跳过Goodreads爬虫，手动粘贴评论素材")
    parser.add_argument("--angle", default="",
                        help="指定脚本角度，跳过自动角度筛选")
    parser.add_argument("--lang", default="zh", choices=["zh", "en"],
                        help="输出语言：zh=中文（默认），en=英文（YouTube）")
    parser.add_argument("--style", default="story", choices=["story", "quotes"],
                        help="脚本风格：story=分析角度（默认），quotes=情感金句朗读（爆款风）")
    parser.add_argument("--voice", default="",
                        help="自定义声音名称（用 setup_voice.py 注册后使用）")
    args = parser.parse_args()

    # 加载自定义声音 profile
    voice_profile = None
    if args.voice:
        import json as _json
        profile_path = Path(__file__).parent / "voices" / args.voice / "profile.json"
        if not profile_path.exists():
            print(f"❌ 未找到声音：{args.voice}，请先运行 setup_voice.py 注册")
            sys.exit(1)
        with open(profile_path, encoding="utf-8") as f:
            voice_profile = _json.load(f)
        print(f"🎤 使用自定义声音：{args.voice}")

    total   = len(args.books)
    results = []

    for i, title in enumerate(args.books, 1):
        if total > 1:
            print(f"\n{'#'*60}")
            print(f"# 进度 {i}/{total}  ·  《{title}》")
            print(f"{'#'*60}")

        reviews = ""

        if args.manual:
            # 手动模式：交互式粘贴
            _, reviews = collect_reviews(title)
        else:
            # 自动模式：先爬Goodreads
            log(f"🌐 Goodreads爬取《{title}》评论...")
            gr_reviews = fetch_goodreads_reviews(title)
            if gr_reviews:
                reviews = _reviews_to_text(gr_reviews)
            elif sys.stdin.isatty():
                # 爬取失败，终端可交互时才提示手动输入
                print(f"   ℹ️ 自动爬取失败，请手动粘贴评论素材（输入END结束，直接回车跳过）")
                _, reviews = collect_reviews(title)
            else:
                # 非交互模式（管道/后台），跳过手动输入，以空评论继续
                print("   ℹ️ 自动爬取失败且为非交互模式，以空评论继续...")

        try:
            out = run_book_mode(title, reviews=reviews, angle=args.angle,
                                lang=args.lang, style=args.style,
                                voice_profile=voice_profile)
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
