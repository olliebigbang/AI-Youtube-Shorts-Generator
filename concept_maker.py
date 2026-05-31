"""
Concept Maker - 概念寓言短视频生成器
--------------------------------------
输入一个概念 → Claude生成寓言故事分镜 → 每场景配音+生图 → 合成短视频，结尾引出书籍推荐

用法：
  python concept_maker.py "NPD"
  python concept_maker.py "原生家庭" --voice Eddie

依赖：pip install anthropic python-dotenv edge-tts pillow
需要：ffmpeg 已安装并在PATH中，ComfyUI 运行中
"""

import os
import sys

# 强制UTF-8，避免Windows GBK终端/管道编码错误
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
if sys.stdin.encoding and sys.stdin.encoding.lower() != 'utf-8':
    sys.stdin.reconfigure(encoding='utf-8', errors='replace')

import json
import re
import subprocess
import argparse
import tempfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

# ── 从 book_maker 复用函数，不重复实现 ───────────────────────
from book_maker import (
    # 用户指定复用
    _get_client,
    _claude_call,
    generate_comfyui_image,
    generate_tts_audio,
    generate_cosyvoice_tts,
    prepare_bg_segment,
    make_black_segment,
    log,
    # 内部依赖
    get_audio_duration,
    concatenate_segments,
    split_long_srt,
    _srt_to_ass,
    _srt_ms,
    _ms_srt,
)

OUTPUT_DIR = Path("output/concept")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

COMFYUI_VIDEO_WORKFLOW = Path(__file__).parent / "comfyui_video_workflow.json"
COMFYUI_IMAGE_WORKFLOW = Path(__file__).parent / "comfyui_concept_image_workflow.json"


# ── 核心：Claude生成分镜脚本 ─────────────────────────────────

def generate_concept_script(concept: str) -> dict:
    """
    调用Claude生成寓言故事分镜脚本。
    返回：{
        "book_title": "书名",
        "book_author": "作者",
        "scenes": [{"id", "narration", "visual", "duration"}, ...]
    }
    """
    log(f"🤖 Claude生成「{concept}」概念分镜脚本...")
    client = _get_client()

    prompt = (
        f"你是一位抖音爆款短视频编剧，擅长用寓言故事讲透一个心理/社会概念，结尾自然带出书籍推荐。\n\n"
        f"【任务】\n"
        f"围绕概念「{concept}」完成以下工作：\n"
        f"1. 推荐一本国内抖音橱窗可上架的书（必须有中文版，正规出版，适合大众阅读）\n"
        f"2. 编写8-12个场景的寓言故事分镜脚本\n\n"
        f"【故事结构】\n"
        f"- 前6个场景：以寓言/故事铺垫，不直接点明概念，制造悬念和情绪共鸣\n"
        f"- 第7个场景：揭晓——原来这就是「{concept}」，概念首次出现，制造恍然大悟感\n"
        f"- 后续场景：自然过渡到书籍，说明这本书能帮助理解或走出这个困境\n\n"
        f"【各字段要求】\n"
        f"- narration：口语化中文旁白，适合TTS朗读，每段30-60字，有节奏感，不要书面语\n"
        f"- visual：英文场景描述，不超过18个词，描述【人物表情+动作+所处环境】，"
        f"表情必须用以下安全词之一：with a subtle closed-mouth smile / with a calm neutral expression / "
        f"with a soft thoughtful gaze / looking down sadly / with a worried frown / with a gentle kind expression，"
        f"【严禁使用】smiling, smirking, happy, grinning, laughing, beaming 这类词，"
        f"要有情节感（如：young woman sitting alone at cafe table looking down sadly, "
        f"young man standing with a calm neutral expression adjusting jacket in mirror, "
        f"old man with a gentle kind expression handing a book in park），"
        f"禁止光线/风格词，每个场景只写一个主要人物\n"
        f"- keywords：2-3个纯情绪词，英文，逗号分隔（如：sad, lonely, proud），禁止写人物名称\n"
        f"- duration：每个场景4-6秒的整数\n\n"
        f"【角色设定】\n"
        f"- 主角：一个普通年轻女性（young woman）\n"
        f"- 反派：一个自大的年轻男性（arrogant young man）\n"
        f"- 其他配角：老人（old man）\n"
        f"- 禁止使用动物，禁止使用儿童角色\n\n"
        f"【禁止事项】\n"
        f"- narration前6个场景禁止出现「{concept}」这个词\n"
        f"- visual和keywords禁止出现中文\n"
        f"- visual每场景只能有一个人物主体\n"
        f"- 禁止说教感和说明书语气\n\n"
        f"严格返回JSON，不含任何其他文字：\n"
        f'{{"book_title":"推荐书名","book_author":"作者名","scenes":['
        f'{{"id":1,"narration":"旁白文本","visual":"girl smiling happily","keywords":"happy, hopeful","duration":5}}'
        f']}}'
    )

    msg = _claude_call(client,
        model="claude-opus-4-8",
        max_tokens=2500,
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

    if not data or "scenes" not in data:
        raise RuntimeError(f"Claude脚本解析失败，原始返回：{raw[:300]}")

    n = len(data["scenes"])
    print(f"✅ 脚本生成完成：{n}个场景，推荐书籍《{data.get('book_title', '?')}》（{data.get('book_author', '?')}）")
    for s in data["scenes"]:
        print(f"   场景{s['id']}（{s['duration']}s）：{s['narration'][:30]}...")

    return data


# ── 辅助函数（本文件内部使用）────────────────────────────────

IMAGE_STYLE_PRESETS = {
    "cartoon": (
        "Korean manhwa webtoon style, clean line art, soft cel shading, "
        "natural almond-shaped eyes, symmetrical eyes, calm gaze, "
        "relaxed lips, closed mouth, subtle gentle smile, no teeth, "
        "soft natural face, consistent character design, "
        "flat colors, elegant comic panel, young adult, "
        "narrative scene with background, "
    ),
    "simple": (
        "Korean manhwa webtoon style, clean line art, flat colors, "
        "natural almond-shaped eyes, calm gaze, closed mouth, no teeth, "
        "soft natural face, consistent character design, young adult, "
        "narrative scene with simple background, "
    ),
    "sketch": (
        "black and white manhwa comic panel, clean ink line art, bold outlines, "
        "natural almond-shaped eyes, symmetrical eyes, calm gaze, "
        "relaxed lips, closed mouth, no teeth, soft natural face, "
        "consistent character design, narrative scene with background, "
        "young adult woman or man, no fill color, no gray shading, "
    ),
}


def generate_scene_image(prompt_text: str, duration: float, output_path: str,
                          style: str = "sketch", tmp_dir: str = None) -> bool:
    """
    用ComfyUI SDXL生成场景图片，再用Ken Burns效果转成视频。
    output_path: 最终.mp4路径
    """
    import urllib.request as _ur
    import urllib.parse
    import time, uuid, json as _json, random as _random
    from book_maker import COMFYUI_HOST, _comfyui_is_running

    if not COMFYUI_IMAGE_WORKFLOW.exists():
        return False
    if not _comfyui_is_running():
        return False

    style_prefix = IMAGE_STYLE_PRESETS.get(style, IMAGE_STYLE_PRESETS["sketch"])
    full_prompt = style_prefix + prompt_text

    workflow = _json.loads(COMFYUI_IMAGE_WORKFLOW.read_text(encoding="utf-8"))
    workflow = {k: v for k, v in workflow.items() if not k.startswith("_")}

    wf_str = _json.dumps(workflow).replace("__PROMPT__", full_prompt.replace('"', '\\"'))
    workflow = _json.loads(wf_str)

    for node in workflow.values():
        if isinstance(node, dict) and "inputs" in node:
            if "seed" in node["inputs"]:
                node["inputs"]["seed"] = _random.randint(0, 2**32 - 1)

    client_id = str(uuid.uuid4())
    payload = _json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = _ur.Request(
        f"http://{COMFYUI_HOST}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _ur.urlopen(req, timeout=10) as r:
            resp = _json.loads(r.read())
    except Exception as e:
        print(f"   ⚠️ ComfyUI提交失败：{e}")
        return False

    if "error" in resp:
        print(f"   ⚠️ ComfyUI错误：{resp['error']}")
        return False

    prompt_id = resp["prompt_id"]

    # 等待图片生成（SDXL 20步约15-30秒）
    img_path = None
    for _ in range(120):
        time.sleep(1)
        try:
            with _ur.urlopen(f"http://{COMFYUI_HOST}/history/{prompt_id}", timeout=5) as r:
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
                tmp_img = os.path.join(tmp_dir or os.path.dirname(output_path),
                                       f"scene_img_{uuid.uuid4().hex[:8]}.png")
                with _ur.urlopen(
                    f"http://{COMFYUI_HOST}/view?{params}", timeout=30
                ) as r2, open(tmp_img, "wb") as f:
                    f.write(r2.read())
                if os.path.getsize(tmp_img) > 5000:
                    img_path = tmp_img
                    break
            if img_path:
                break
        if img_path:
            break

    if not img_path:
        return False

    # 图片 → Ken Burns 视频
    _ken_burns_video(img_path, duration, output_path)
    return os.path.exists(output_path) and os.path.getsize(output_path) > 10000


def _wan_frames(duration: float) -> int:
    """计算Wan2.1兼容的帧数（必须是4k+1，最大81帧）"""
    target = int(duration * 24)
    k = max(1, (target - 1) // 4)
    return min(81, 4 * k + 1)


def generate_comfyui_video(prompt_text: str, duration: float, output_path: str,
                           style: str = "cartoon") -> bool:
    """
    调用ComfyUI Wan2.1生成视频片段。
    需要 comfyui_video_workflow.json，workflow中用：
      __PROMPT__  占位正向提示词
      __FRAMES__  占位帧数
    """
    from book_maker import COMFYUI_HOST, _comfyui_is_running
    import urllib.request as _ur
    import urllib.parse
    import time, uuid, json as _json, random as _random

    if not COMFYUI_VIDEO_WORKFLOW.exists():
        return False
    if not _comfyui_is_running():
        return False

    STYLE_PRESETS = {
        "cartoon": (
            "2D flat animation, simple cartoon illustration, clean line art, "
            "minimal detail, bold outlines, pastel colors, storybook style, "
        ),
        "simple": (
            "simple flat 2D cartoon, solid color shapes, thick outlines, "
            "children's picture book style, no shading, no texture, minimal detail, "
        ),
        "sketch": (
            "minimalist black line drawing, simple stick figure style, "
            "white background, no fill color, very few lines, children's doodle, "
        ),
    }
    style_prefix = STYLE_PRESETS.get(style, STYLE_PRESETS["cartoon"])
    prompt_text = style_prefix + prompt_text

    frames = _wan_frames(duration)
    workflow = _json.loads(COMFYUI_VIDEO_WORKFLOW.read_text(encoding="utf-8"))
    workflow = {k: v for k, v in workflow.items() if not k.startswith("_")}

    wf_str = (_json.dumps(workflow)
              .replace("__PROMPT__", prompt_text.replace('"', '\\"'))
              .replace('"__FRAMES__"', str(frames))
              .replace("__FRAMES__", str(frames)))
    workflow = _json.loads(wf_str)

    for node in workflow.values():
        if isinstance(node, dict) and "inputs" in node:
            if "seed" in node["inputs"]:
                node["inputs"]["seed"] = _random.randint(0, 2**32 - 1)
            if "noise_seed" in node["inputs"]:
                node["inputs"]["noise_seed"] = _random.randint(0, 2**32 - 1)

    client_id = str(uuid.uuid4())
    payload = _json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = _ur.Request(
        f"http://{COMFYUI_HOST}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _ur.urlopen(req, timeout=10) as r:
        prompt_id = _json.loads(r.read())["prompt_id"]

    # Wan2.1生成较慢，最多等360秒（首次冷启动需要加载模型）
    for _ in range(360):
        time.sleep(1)
        try:
            with _ur.urlopen(f"http://{COMFYUI_HOST}/history/{prompt_id}", timeout=5) as r:
                history = _json.loads(r.read())
        except Exception:
            continue
        if prompt_id not in history:
            continue
        outputs = history[prompt_id].get("outputs", {})
        for node_output in outputs.values():
            # VHS_VideoCombine 输出在 gifs 或 videos 键
            for vid in node_output.get("gifs", []) + node_output.get("videos", []):
                params = urllib.parse.urlencode({
                    "filename": vid["filename"],
                    "subfolder": vid.get("subfolder", ""),
                    "type": vid.get("type", "output"),
                })
                with _ur.urlopen(
                    f"http://{COMFYUI_HOST}/view?{params}", timeout=60
                ) as r2, open(output_path, "wb") as f:
                    f.write(r2.read())
                if os.path.getsize(output_path) > 10000:
                    return True
    return False


def _ken_burns_video(image_path: str, duration: float, output_path: str):
    """将单张图片用 Ken Burns 缓慢缩放效果生成指定时长视频（480x832）
    输入图片可以是 768x1344（生成分辨率），输出统一为 480x832。
    """
    fps = 30
    total_frames = max(1, int(duration * fps))
    # 先缩到 960x1664（2× 输出分辨率），给 zoompan 留缩放空间，最后缩到 480x832
    vf = (
        f"scale=960:1664:force_original_aspect_ratio=increase,crop=960:1664,"
        f"zoompan=z='min(zoom+0.0006,1.25)':d={total_frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
        f"scale=480:832"
    )
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", image_path,
        "-vf", vf,
        "-t", f"{duration:.3f}",
        "-r", str(fps),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-an",
        output_path
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError(f"Ken Burns视频生成失败：{r.stderr[-400:]}")


def _concat_audio(audio_paths: list, output_path: str):
    """拼接多段音频文件"""
    list_file = output_path + ".txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in audio_paths:
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
        raise RuntimeError(f"音频拼接失败：{r.stderr[-400:]}")


def _shift_srt(srt_content: str, offset_ms: int) -> str:
    """将SRT所有时间戳整体偏移 offset_ms 毫秒"""
    def shift_time(t: str) -> str:
        return _ms_srt(max(0, _srt_ms(t) + offset_ms))

    result = []
    for entry in re.split(r"\n{2,}", srt_content.strip()):
        lines = entry.strip().splitlines()
        if len(lines) < 3:
            continue
        start_t, end_t = lines[1].split("-->")
        new_timing = f"{shift_time(start_t.strip())} --> {shift_time(end_t.strip())}"
        result.append("\n".join([lines[0], new_timing] + lines[2:]))
    return "\n\n".join(result) + "\n"


# ── 主流程 ───────────────────────────────────────────────────

def run_concept_mode(concept: str, voice_profile: dict = None,
                     style: str = "cartoon", mode: str = "image") -> Path:
    log(f"💡 开始生成「{concept}」概念视频（模式：{mode}）")

    # 1. 生成分镜脚本
    script_data = generate_concept_script(concept)
    scenes      = script_data["scenes"]
    book_title  = script_data.get("book_title", "")
    book_author = script_data.get("book_author", "")

    # 2. 创建输出目录
    safe_name = re.sub(r'[\\/:*?"<>|《》]', '', concept)[:40]
    out_dir   = OUTPUT_DIR / safe_name
    out_dir.mkdir(parents=True, exist_ok=True)

    video_segs    = []
    audio_segs    = []
    srt_parts     = []
    cumulative_ms = 0

    with tempfile.TemporaryDirectory() as tmp:

        # 3. 逐场景：配音 → 生图 → 视频片段
        for scene in scenes:
            sid      = scene["id"]
            narr     = scene["narration"]
            visual   = scene["visual"]
            keywords = scene.get("keywords", "")
            # 合并 visual + keywords 作为最终提示词
            video_prompt = visual
            if keywords:
                video_prompt = f"{visual}, {keywords}"
            duration = float(scene.get("duration", 5))

            log(f"🎬 场景 {sid}/{len(scenes)}")
            print(f"   旁白：{narr[:50]}...")
            print(f"   画面：{video_prompt}")

            # 3a. TTS配音 + 字幕
            tts_path = os.path.join(tmp, f"scene_{sid}.mp3")
            srt_path = os.path.join(tmp, f"scene_{sid}.srt")
            generate_tts_audio(narr, tts_path, srt_path,
                               lang="zh", voice_profile=voice_profile)

            # 拆分超长字幕条目
            with open(srt_path, encoding="utf-8") as f:
                raw_srt = f.read()
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(split_long_srt(raw_srt, max_chars=10))

            # 以实际TTS时长为准，加0.3s留白
            actual_dur = get_audio_duration(tts_path) + 0.3

            # 收集偏移后的字幕，用于最终合并
            with open(srt_path, encoding="utf-8") as f:
                srt_parts.append(_shift_srt(f.read(), cumulative_ms))
            cumulative_ms += int(actual_dur * 1000)

            audio_segs.append(tts_path)

            # 3b. 生成场景视频
            seg_path = os.path.join(tmp, f"scene_{sid}.mp4")
            vid_ok   = False
            if mode == "video":
                print(f"   🎬 Wan2.1 14B生成场景视频（{_wan_frames(actual_dur)}帧）...")
                vid_raw = os.path.join(tmp, f"scene_{sid}_raw.mp4")
                try:
                    vid_ok = generate_comfyui_video(video_prompt, actual_dur, vid_raw, style=style)
                except Exception as e:
                    print(f"   ⚠️ 视频生成失败：{e}")
                if vid_ok and os.path.exists(vid_raw):
                    prepare_bg_segment(vid_raw, actual_dur, seg_path)
                    vid_ok = True
                else:
                    vid_ok = False
            else:
                print(f"   🖼️ SDXL生成场景图片（{style}风格）...")
                try:
                    vid_ok = generate_scene_image(video_prompt, actual_dur, seg_path,
                                                  style=style, tmp_dir=tmp)
                except Exception as e:
                    print(f"   ⚠️ 图片生成失败：{e}")

            if not vid_ok:
                print("   ⚠️ 使用黑色背景")
                make_black_segment(actual_dur, seg_path)

            video_segs.append(seg_path)
            print(f"   ✅ 场景 {sid} 完成（实际时长 {actual_dur:.1f}s）")

        # 4. 拼接所有视频片段
        log("🎞️ 拼接视频片段...")
        bg_full = os.path.join(tmp, "bg_full.mp4")
        concatenate_segments(video_segs, bg_full)

        # 5. 拼接所有音频
        log("🔊 拼接音频...")
        audio_full = os.path.join(tmp, "audio_full.mp3")
        _concat_audio(audio_segs, audio_full)

        # 6. 合并字幕（重新编号）
        combined_srt = ""
        counter = 1
        for part in srt_parts:
            for entry in re.split(r"\n{2,}", part.strip()):
                lines = entry.strip().splitlines()
                if len(lines) < 3:
                    continue
                lines[0] = str(counter)
                combined_srt += "\n".join(lines) + "\n\n"
                counter += 1

        combined_srt_path = os.path.join(tmp, "subtitles.srt")
        with open(combined_srt_path, "w", encoding="utf-8") as f:
            f.write(combined_srt)

        # 7. 生成ASS字幕并合成最终视频
        log("🎬 合成最终视频...")
        # ASS文件写到out_dir（路径无空格），避免Windows临时目录路径传给ffmpeg ass=滤镜报错
        idx = 1
        while (out_dir / f"final_{idx:02d}.mp4").exists():
            idx += 1
        output_file = out_dir / f"final_{idx:02d}.mp4"

        ass_path      = str(out_dir / "subs.ass")
        total_duration = get_audio_duration(audio_full)
        _srt_to_ass(combined_srt_path, ass_path, lang="zh")

        # ffmpeg ass滤镜路径：Windows反斜杠→正斜杠，冒号转义
        ass_filter = ass_path.replace("\\", "/").replace(":", "\\:")

        cmd = [
            "ffmpeg", "-y",
            "-i", bg_full,
            "-i", audio_full,
            "-map", "0:v", "-map", "1:a",
            "-vf", f"ass={ass_filter}",
            "-t", str(total_duration),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_file)
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        if r.returncode != 0:
            raise RuntimeError(f"视频合成失败：{r.stderr[-500:]}")

        print(f"✅ 视频生成完成：{output_file}")

        # 8. 保存分镜脚本JSON（方便复查和复用）
        script_file = out_dir / "script.json"
        with open(script_file, "w", encoding="utf-8") as f:
            json.dump(script_data, f, ensure_ascii=False, indent=2)
        print(f"   📄 分镜脚本：{script_file}")

    log(f"🎉 「{concept}」完成！输出：{output_file.absolute()}")
    print(f"   推荐书籍：《{book_title}》  作者：{book_author}")
    return output_file


# ── 主程序 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Concept Maker - 概念寓言短视频生成器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python concept_maker.py \"NPD\"\n"
            "  python concept_maker.py \"原生家庭\" --voice Eddie\n"
            "  python concept_maker.py \"内耗\"\n"
        )
    )
    parser.add_argument("concept", help="要讲解的概念，如 NPD、原生家庭、内耗、讨好型人格")
    parser.add_argument("--voice", default="",
                        help="自定义声音名称（用 setup_voice.py 注册后使用）")
    parser.add_argument("--style", default="cartoon",
                        choices=["cartoon", "simple", "sketch"],
                        help="画面风格：cartoon（彩色卡通）/ simple（极简平面）/ sketch（黑白素描）")
    parser.add_argument("--mode", default="image",
                        choices=["image", "video"],
                        help="生成模式：image（SDXL图片+Ken Burns，快）/ video（Wan2.1 T2V动态视频，慢）")
    args = parser.parse_args()

    # 加载自定义声音 profile
    voice_profile = None
    if args.voice:
        profile_path = Path(__file__).parent / "voices" / args.voice / "profile.json"
        if not profile_path.exists():
            print(f"❌ 未找到声音：{args.voice}，请先运行 setup_voice.py 注册")
            sys.exit(1)
        with open(profile_path, encoding="utf-8") as f:
            voice_profile = json.load(f)
        print(f"🎤 使用自定义声音：{args.voice}")

    print(f"🎨 画面风格：{args.style}  模式：{args.mode}")
    run_concept_mode(args.concept, voice_profile=voice_profile, style=args.style, mode=args.mode)


if __name__ == "__main__":
    main()
