import os
import re
import json
import time
import random
import urllib.parse
from datetime import datetime
from io import BytesIO
import requests
import streamlit as st
from newspaper import Article
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageStat
from pydantic import BaseModel
from typing import List

st.set_page_config(page_title="SNS 인스타 카드뉴스 쾌속 생성기", page_icon="📱", layout="centered")

# =============================================
# 1. API 키 설정
# =============================================
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key and "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]

with st.expander("🔑 Gemini API 키 설정 (필요 시 확인/입력)"):
    user_key_input = st.text_input(
        "API Key",
        value=api_key if api_key else "",
        type="password",
        placeholder="Gemini API 키를 여기에 입력하세요"
    )
    if user_key_input.strip():
        api_key = user_key_input.strip()

client = None
if api_key:
    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        st.error(f"API 클라이언트 초기화 에러: {e}")

# =============================================
# 폰트 로더
# =============================================
def load_fonts(t_sz, c_sz):
    font_candidates_bold = [
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "malgunbd.ttf",
        "NanumGothicBold.ttf"
    ]
    font_candidates_regular = [
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "malgun.ttf",
        "NanumGothic.ttf"
    ]

    title_font, content_font, badge_font = None, None, None
    for f_path in font_candidates_bold:
        try:
            title_font = ImageFont.truetype(f_path, t_sz)
            badge_font = ImageFont.truetype(f_path, 22)
            break
        except Exception:
            continue
    for f_path in font_candidates_regular:
        try:
            content_font = ImageFont.truetype(f_path, c_sz)
            break
        except Exception:
            continue

    if not title_font: title_font = ImageFont.load_default()
    if not content_font: content_font = ImageFont.load_default()
    if not badge_font: badge_font = ImageFont.load_default()

    return title_font, content_font, badge_font

# =============================================
# 텍스트 정제기
# =============================================
def sanitize_korean_text(text):
    if not text:
        return ""
    text = re.sub(r"\[.*?기자.*?\]", "", text)
    text = re.sub(r"\(.*?=.*?기자\)", "", text)
    text = re.sub(r"\(.*?=.*?\)", "", text)
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r".*?기자\s*=", "", text)
    text = re.sub(r"\w+기자\b", "", text)
    text = re.sub(r"\(사진=.*?\)", "", text)
    text = re.sub(r"\b(오늘|어제|지난)\s*\(\d+일\)", "", text)
    text = re.sub(r"''\(이하\s*['\"].*?['\"]\)", "", text)
    text = re.sub(r"\(이하\s*['\"].*?['\"]\)", "", text)
    text = text.replace("''", "'").replace('""', '"')
    text = re.sub(r"\s+", " ", text)
    return text.strip()

# =============================================
# 문장/의미 단위 균형 조판 엔진 (외톨이 단어 방지)
# =============================================
def wrap_korean_balanced(text, max_chars_per_line=21):
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    final_lines = []

    for s in sentences:
        if len(s) <= max_chars_per_line:
            final_lines.append(s)
            continue

        words = s.split()
        if not words:
            continue

        target_lines = max(2, (len(s) + max_chars_per_line - 1) // max_chars_per_line)
        ideal_len = len(s) / target_lines

        curr_line = ""
        for w in words:
            candidate = f"{curr_line} {w}".strip() if curr_line else w
            if len(candidate) > max_chars_per_line and curr_line:
                final_lines.append(curr_line)
                curr_line = w
            elif len(curr_line) >= ideal_len and len(candidate) <= max_chars_per_line:
                final_lines.append(candidate)
                curr_line = ""
            else:
                curr_line = candidate

        if curr_line:
            if len(curr_line) < 5 and final_lines:
                prev = final_lines.pop()
                combined = f"{prev} {curr_line}"
                if len(combined) <= max_chars_per_line + 4:
                    final_lines.append(combined)
                else:
                    p_words = combined.split()
                    mid = len(p_words) // 2
                    final_lines.append(" ".join(p_words[:mid]))
                    final_lines.append(" ".join(p_words[mid:]))
            else:
                final_lines.append(curr_line)

    return final_lines

def wrap_natural_korean(text, max_chars_per_line=13):
    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        if len(current_line + word) > max_chars_per_line:
            if current_line.strip():
                lines.append(current_line.strip())
            current_line = word + " "
        else:
            current_line += word + " "

    if current_line.strip():
        lines.append(current_line.strip())
    
    return lines

# =============================================
# 피사체 보호 프레이밍 엔진 (1080x1350)
# =============================================
def smart_fit_or_crop(base_img, target_w=1080, target_h=1350):
    base_img = base_img.convert("RGBA")
    src_w, src_h = base_img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h

    if src_ratio > 1.15:
        bg_scale = max(target_w / src_w, target_h / src_h)
        bg_w, bg_h = int(src_w * bg_scale), int(src_h * bg_scale)
        bg = base_img.resize((bg_w, bg_h), Image.Resampling.LANCZOS)
        
        left = (bg_w - target_w) // 2
        top = (bg_h - target_h) // 2
        bg = bg.crop((left, top, left + target_w, top + target_h)).filter(ImageFilter.GaussianBlur(35))
        bg = Image.alpha_composite(bg, Image.new("RGBA", (target_w, target_h), (0, 0, 0, 95)))

        fit_scale = min(target_w / src_w, (target_h * 0.65) / src_h)
        fg_w, fg_h = int(src_w * fit_scale), int(src_h * fit_scale)
        fg = base_img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)

        pos_x = (target_w - fg_w) // 2
        pos_y = 120
        bg.paste(fg, (pos_x, pos_y), fg)
        return bg

    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left_offset = int((src_w - new_w) * 0.45)
        cropped = base_img.crop((left_offset, 0, left_offset + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top_offset = max(0, min(int((src_h - new_h) * 0.10), src_h - new_h))
        cropped = base_img.crop((0, top_offset, src_w, top_offset + new_h))

    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)

def is_valid_photo(pil_img):
    if pil_img.width < 300 or pil_img.height < 300:
        return False
    stat = ImageStat.Stat(pil_img.convert("L"))
    if stat.stddev[0] < 20:
        return False
    ratio = pil_img.width / pil_img.height
    return 0.45 <= ratio <= 2.6

def download_image_pil(img_url):
    if not img_url:
        return None
    try:
        res = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if res.status_code == 200 and len(res.content) > 5000:
            img = Image.open(BytesIO(res.content))
            if is_valid_photo(img):
                return img
    except Exception:
        pass
    return None

# =============================================
# AI 이미지 생성 파이프라인
# =============================================
def generate_contextual_ai_image(prompt_text, seed_val=42):
    clean_prompt = re.sub(r'[^a-zA-Z0-9\s,]', '', prompt_text).strip()
    if not clean_prompt:
        clean_prompt = "flagship tech product documentary scene"
    
    variations = [
        "dramatic cinematic lighting, photorealistic 8k, ultra sharp focus, dark background",
        "studio product photography, clean professional lighting, crisp contrast, 8k",
        "photojournalism editorial documentary style, 8k resolution, authentic detail",
        "close-up detail shot, moody dark aesthetic, high contrast editorial"
    ]
    selected_style = variations[seed_val % len(variations)]
    final_prompt = f"{clean_prompt}, {selected_style}"

    if client:
        try:
            result = client.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=final_prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="3:4",
                    output_mime_type="image/jpeg"
                )
            )
            for gen_img in result.generated_images:
                img = Image.open(BytesIO(gen_img.image.image_bytes))
                return img.resize((1080, 1350), Image.Resampling.LANCZOS)
        except Exception:
            pass

    encoded = urllib.parse.quote(final_prompt)
    ts = int(time.time() * 1000)
    external_urls = [
        f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1350&seed={seed_val}&model=turbo&nologo=true&t={ts}",
        f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1350&seed={seed_val + 99}&nologo=true&t={ts + 1}",
        f"https://image.pollinations.ai/prompt/{encoded}?width=1080&height=1350&seed={seed_val + 333}&model=flux&nologo=true&t={ts + 2}"
    ]

    headers = {
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.{random.randint(1, 200)}",
        "Cache-Control": "no-cache"
    }

    for u in external_urls:
        try:
            res = requests.get(u, headers=headers, timeout=12)
            if res.status_code == 200 and len(res.content) > 10000:
                img = Image.open(BytesIO(res.content))
                if is_valid_photo(img):
                    return img
        except Exception:
            continue

    base = Image.new("RGB", (1080, 1350), color=(15, 23, 42))
    draw = ImageDraw.Draw(base)
    for y in range(0, 1350):
        ratio = y / 1350.0
        r = int(15 + 25 * ratio)
        g = int(23 + 28 * ratio)
        b = int(42 + 40 * ratio)
        draw.line([(0, y), (1080, y)], fill=(r, g, b))
    return base

# =============================================
# Pydantic 모델 & 1회 통합 호출 엔진
# =============================================
class UnifiedCardNewsResponse(BaseModel):
    titles: List[str]
    card_subcopy: str
    image_prompt: str
    empathy: str
    vote: str
    explain: str

PRIMARY_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]

def generate_all_card_content(title, text):
    clean_t = sanitize_korean_text(title)
    if not client:
        raise Exception("Gemini API Key가 설정되지 않았습니다. 상단 API Key 설정창에 키를 입력해 주세요.")

    prompt = f"""
    당신은 SNS 시사/트렌드 뉴스 전문 에디터입니다.
    아래 기사의 실제 분야(스마트폰, IT, 부동산, 정치, 사회, 경제, 연예 등)의 사건 팩트에 부합하는 콘텐츠 세트를 작성하세요.

    [필수 작성 규칙]:
    1. titles: 독자의 스크롤을 멈추게 하는 강력한 후킹 제목 5개 (1줄당 14~20자 내외, 대괄호 [] 제외).
    2. card_subcopy: 피드 1장 카드에 들어갈 본문 요약 (공백 포함 65~80자 내외, 정확히 2개의 완결된 문장).
       - 중요: 줄바꿈 시 문맥이 깨지지 않도록 간결하고 명확한 2문장으로만 서술하세요. 끝에 '조명합니다', '살펴봅니다' 같은 상투적인 사족을 덧붙이지 마세요.
    3. image_prompt: 이 기사 내용에 정확히 들어맞는 영어 이미지 프롬프트.
       - 고급 빌라/부동산 기사면: 'modern luxury penthouse villa exterior architectural photography cinematic lighting 8k'
       - 스마트폰/IT 기사면: 'modern flagship smartphone device screen display product photography dark background 8k'
       - 교통사고/사건 기사면: 'car accident investigation road traffic police scene dramatic documentary lighting'
    4. empathy: 기사의 실제 팩트를 설명하고 의견을 나누는 공감형 인스타 본문.
    5. vote: 기사의 쟁점을 바탕으로 한 찬반(A vs B) 투표형 인스타 본문.
    6. explain: 기사의 핵심 팩트 3줄 요약 인스타 본문.

    기사 제목: {clean_t}
    기사 본문 내용:
    {text[:2000]}
    """

    last_err = None
    for m in PRIMARY_MODELS:
        try:
            res = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=UnifiedCardNewsResponse,
                    temperature=0.7
                )
            )
            data = json.loads(res.text)
            return {
                "titles": [sanitize_korean_text(t) for t in data.get("titles", [])][:5],
                "card_subcopy": sanitize_korean_text(data.get("card_subcopy", "")),
                "image_prompt": data.get("image_prompt", "luxury penthouse villa architectural photography"),
                "empathy": data.get("empathy", ""),
                "vote": data.get("vote", ""),
                "explain": data.get("explain", "")
            }
        except Exception as e:
            last_err = e
            continue

    raise Exception(f"AI 생성 실패: {last_err}")

# =============================================
# 단일 카드 렌더링 엔진 (문장 단위 조판 적용)
# =============================================
def render_single_card(title_text, sub_text, base_img, title_size, content_size, text_y_pos):
    width, height = 1080, 1350
    t_font, c_font, b_font = load_fonts(title_size, content_size)

    base_img = smart_fit_or_crop(base_img, width, height)

    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(gradient)

    for y in range(0, 180):
        alpha = int((1.0 - (y / 180.0)) * 85)
        g_draw.line([(0, y), (width, y)], fill=(5, 8, 15, alpha))

    start_g = int(text_y_pos - 140)
    for y in range(start_g, height):
        if y < start_g + 260:
            progress = (y - start_g) / 260.0
            alpha = int((progress ** 1.6) * 235)
        else:
            alpha = 248
        g_draw.line([(0, y), (width, y)], fill=(9, 13, 20, alpha))

    card = Image.alpha_composite(base_img, gradient).convert("RGB")
    draw = ImageDraw.Draw(card)

    badge_x, badge_y = 64, 64
    badge_w, badge_h = 224, 54
    badge_radius = 27

    badge_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    b_draw = ImageDraw.Draw(badge_layer)
    b_draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_radius,
        fill=(10, 18, 30,
