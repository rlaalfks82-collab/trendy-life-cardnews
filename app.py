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
            badge_font = ImageFont.truetype(f_path, 21)
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
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

# =============================================
# 줄바꿈 처리 엔진 (수동 엔터 보존 & 자동 분할)
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

def format_text_lines(text, wrap_func, max_chars_per_line):
    if not text:
        return []
    raw_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(raw_lines) > 1:
        return raw_lines
    elif len(raw_lines) == 1:
        return wrap_func(raw_lines[0], max_chars_per_line)
    return []

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
    if pil_img.width < 250 or pil_img.height < 250:
        return False
    stat = ImageStat.Stat(pil_img.convert("L"))
    if stat.stddev[0] < 18:
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
# [100% 무제한 생성 보장] 다중 분산 AI 이미지 파이프라인
# =============================================
def generate_contextual_ai_image(prompt_text, seed_val=42):
    clean_prompt = re.sub(r'[^a-zA-Z0-9\s,]', '', prompt_text).strip()
    if not clean_prompt:
        clean_prompt = "modern editorial issue topic visual concept"

    variations = [
        "dramatic cinematic lighting, photorealistic 8k, ultra sharp focus, dark atmosphere, editorial photography",
        "commercial photography, architectural grandeur, crisp contrast, 8k resolution, elegant mood",
        "photojournalism editorial documentary style, 8k resolution, authentic detail, realistic textures",
        "golden hour cinematic lighting, luxury aesthetic, moody dark contrast, award-winning photography"
    ]
    selected_style = variations[seed_val % len(variations)]
    final_prompt = f"{clean_prompt}, {selected_style}"

    # 1순위: Google Imagen 3 (보유 계정 쿼터 허용 시 우선)
    if client:
        for m_name in ['imagen-3.0-generate-002', 'imagen-3.0-generate-001']:
            try:
                result = client.models.generate_images(
                    model=m_name,
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

    # 2순위: 다중 분산 생성 엔드포인트 로테이션 (타임아웃 5초 초고속 폴링)
    encoded = urllib.parse.quote(final_prompt)
    ts = int(time.time() * 1000)
    
    endpoints = [
        f"https://image.pollinations.ai/prompt/{encoded}?width=640&height=800&seed={seed_val}&model=turbo&nologo=true&t={ts}",
        f"https://image.pollinations.ai/prompt/{encoded}?width=640&height=800&seed={seed_val + 177}&nologo=true&t={ts + 1}",
        f"https://image.pollinations.ai/prompt/{encoded}?width=640&height=800&seed={seed_val + 491}&model=flux&nologo=true&t={ts + 2}",
        f"https://image.pollinations.ai/prompt/{encoded}?width=512&height=640&seed={seed_val + 999}&model=turbo&nologo=true&t={ts + 3}"
    ]

    for ep in endpoints:
        try:
            res = requests.get(
                ep,
                headers={
                    "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{100 + (seed_val % 30)}.0.0.0 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache"
                },
                timeout=5
            )
            if res.status_code == 200 and len(res.content) > 6000:
                img = Image.open(BytesIO(res.content))
                if is_valid_photo(img):
                    return img.resize((1080, 1350), Image.Resampling.LANCZOS)
        except Exception:
            continue

    # 3순위: 기사 키워드 기반 실사 에디토리얼 풀
    keywords = [w for w in clean_prompt.split() if len(w) > 3][:3]
    search_q = ",".join(keywords) if keywords else "modern,editorial"
    editorial_urls = [
        f"https://loremflickr.com/1080/1350/{urllib.parse.quote(search_q)}?lock={seed_val % 9999}",
        f"https://source.unsplash.com/1080x1350/?{urllib.parse.quote(search_q)}&sig={seed_val % 500}"
    ]
    for e_url in editorial_urls:
        try:
            res = requests.get(e_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
            if res.status_code == 200 and len(res.content) > 8000:
                img = Image.open(BytesIO(res.content))
                if is_valid_photo(img):
                    return smart_fit_or_crop(img, 1080, 1350)
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
# Pydantic 모델 & [피드 진단 피드백 반영] 프롬프트 엔진
# =============================================
class UnifiedCardNewsResponse(BaseModel):
    category: str           # NEWS, CULTURE, TREND 중 1개 자동 판별
    titles: List[str]       # 모바일 3x3 그리드용 짧고 강렬한 제목 5개
    card_subcopy: str       # 팩트 1줄 + 독자 영향/맥락 1줄
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
    당신은 인스타그램 트렌드 뉴스 미디어 '트랜디 라이프'의 수석 에디터입니다.
    피드 진단 원칙에 입각하여 독자의 스크롤을 멈추고 저장을 유도하는 카드뉴스를 기획하세요.

    [작성 규칙]:
    1. category: 기사 성격에 따라 다음 3가지 중 1가지를 정확히 지정하세요:
       - 'NEWS': 사회, 사건사고, 경제, 정책, 법률, 소비자 피해, 제도 변화 (비중 50%)
       - 'CULTURE': 연예, 셀럽, 방송/드라마, 인터넷 화제, 대중문화 (비중 30%)
       - 'TREND': 라이프스타일, 여행, 건강, 소비 패턴, 핫플레이스 (비중 20%)

    2. titles: 모바일 3x3 피드 그리드에서 한눈에 읽히는 강력한 제목 5개.
       - 1줄당 9~13자 내외, 최대 2줄로 매우 짧고 명확하게 핵심만 타격 (대괄호 [] 제외).

    3. card_subcopy: 카드 표지 하단 요약 (공백 포함 60~75자, 정확히 2문장).
       - 1문장: 무슨 일이 일어났는지 핵심 팩트 요약.
       - 2문장: [독자가 알아야 할 배경, 영향, 또는 주의할 점] 1줄 추가. (단순 사건 나열 금지)

    4. image_prompt: 기사 맥락에 정확히 일치하는 고화질 영문 이미지 프롬프트.
    5. empathy: 팩트와 함께 독자의 생각/의견을 묻는 인스타 본문 캡션.
    6. vote: 독자 참여를 유도하는 찬반(A vs B) 투표형 인스타 본문 캡션.
    7. explain: 핵심 요약과 함께 저장(북마크)을 유도하는 정보형 인스타 본문 캡션.

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
            cat = str(data.get("category", "NEWS")).strip().upper()
            if cat not in ["NEWS", "CULTURE", "TREND"]:
                cat = "NEWS"

            return {
                "category": cat,
                "titles": [sanitize_korean_text(t) for t in data.get("titles", [])][:5],
                "card_subcopy": sanitize_korean_text(data.get("card_subcopy", "")),
                "image_prompt": data.get("image_prompt", "modern architectural visual"),
                "empathy": data.get("empathy", ""),
                "vote": data.get("vote", ""),
                "explain": data.get("explain", "")
            }
        except Exception as e:
            last_err = e
            continue

    raise Exception(f"AI 생성 실패: {last_err}")

# =============================================
# 단일 카드 렌더링 엔진 (동적 카테고리 뱃지 탑재)
# =============================================
def render_single_card(title_text, sub_text, base_img, title_size, content_size, text_y_pos, category_text="NEWS"):
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

    # 카테고리 뱃지 글자 길이에 따른 너비 유동 계산
    badge_label = category_text.strip().upper()
    badge_x, badge_y = 64, 64
    badge_w = 175 if len(badge_label) <= 5 else (210 if len(badge_label) <= 7 else 230)
    badge_h = 52
    badge_radius = 26

    badge_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    b_draw = ImageDraw.Draw(badge_layer)
    b_draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_radius,
        fill=(10, 18, 30, 200)
    )

    for i in range(2):
        b_draw.rounded_rectangle(
            [badge_x - i, badge_y - i, badge_x + badge_w + i, badge_y + badge_h + i],
            radius=badge_radius + i,
            outline=(255, 255, 255, int(45 - i * 15)),
            width=1
        )
    b_draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_radius,
        outline=(15, 25, 40, 90),
        width=2
    )

    card = Image.alpha_composite(card.convert("RGBA"), badge_layer).convert("RGB")
    draw = ImageDraw.Draw(card)

    # 옐로우 포인트 점 + 동적 카테고리 영문
    dot_cx, dot_cy, dot_r = badge_x + 22, badge_y + 26, 4
    draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=(251, 191, 36))
    draw.text((badge_x + 36, badge_y + 13), badge_label, font=b_font, fill=(241, 245, 249, 235))

    t_lines = format_text_lines(title_text, wrap_natural_korean, max_chars_per_line=13)
    c_lines = format_text_lines(sub_text, wrap_korean_balanced, max_chars_per_line=21)

    curr_y = text_y_pos
    draw.rounded_rectangle([64, curr_y - 20, 114, curr_y - 13], radius=4, fill=(251, 191, 36))

    for l in t_lines:
        draw.text((64, curr_y), l, font=t_font, fill=(255, 255, 255))
        curr_y += title_size + 14

    curr_y += 18
    for l in c_lines:
        draw.text((64, curr_y), l, font=c_font, fill=(226, 232, 240))
        curr_y += content_size + 14

    draw.line([64, 1260, 1016, 1260], fill=(51, 65, 85, 140), width=2)
    return card

# =============================================
# 세션 상태 관리
# =============================================
if "app_state" not in st.session_state:
    st.session_state.app_state = {
        "is_ready": False,
        "category": "NEWS",
        "copies": [],
        "active_title": "",
        "active_sub": "",
        "captions": {},
        "article_images": [],
        "ai_image_history": [],
        "history_idx": 0,
        "current_image_source": "ai",
        "current_img_idx": 0,
        "title_size": 54,
        "content_size": 30,
        "text_y": 860,
        "image_prompt": "modern luxury architectural photography",
        "seed": 42,
        "redraw_count": 0
    }

# =============================================
# 메인 화면 UI
# =============================================
st.markdown("<h2 style='text-align: center; margin-bottom: 5px;'>🚀 인스타 단일 피드 카드뉴스 생성기</h2>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: #64748B; margin-bottom: 25px;'>트랜디 라이프 피드 규격(NEWS·CULTURE·TREND) 기반 자동화</p>", unsafe_allow_html=True)

news_url = st.text_input("🔗 뉴스 기사 링크 입력", placeholder="네이버/다음 등 포털 뉴스 기사 링크를 붙여넣으세요")

if st.button("✨ 인스타 게시물 만들기", type="primary", use_container_width=True):
    if not news_url.strip():
        st.warning("뉴스 링크를 입력해 주세요.")
    else:
        with st.spinner("기사 분석 및 내용에 맞는 맞춤 이미지를 제작하고 있습니다..."):
            try:
                art = Article(news_url, language='ko')
                art.download()
                art.parse()

                if not art.text or len(art.text.strip()) < 50:
                    st.error("기사 본문을 불러오지 못했습니다. 링크를 다시 확인해 주세요.")
                else:
                    art_img_pool = []
                    if art.top_image:
                        top_img = download_image_pil(art.top_image)
                        if top_img: art_img_pool.append(top_img)
                    for u in art.images:
                        if u != art.top_image:
                            p = download_image_pil(u)
                            if p: art_img_pool.append(p)

                    if not art_img_pool:
                        art_img_pool = [Image.new("RGB", (1080, 1350), color=(15, 23, 42))]

                    ai_result = generate_all_card_content(art.title, art.text)
                    
                    initial_seed = random.randint(1001, 99999)
                    ai_img = generate_contextual_ai_image(ai_result["image_prompt"], seed_val=initial_seed)

                    st.session_state.app_state["is_ready"] = True
                    st.session_state.app_state["category"] = ai_result["category"]
                    st.session_state.app_state["copies"] = ai_result["titles"]
                    st.session_state.app_state["active_title"] = ai_result["titles"][0]
                    st.session_state.app_state["active_sub"] = ai_result["card_subcopy"]
                    st.session_state.app_state["image_prompt"] = ai_result["image_prompt"]
                    st.session_state.app_state["captions"] = {
                        "empathy": ai_result["empathy"],
                        "vote": ai_result["vote"],
                        "explain": ai_result["explain"]
                    }
                    st.session_state.app_state["article_images"] = art_img_pool
                    st.session_state.app_state["ai_image_history"] = [ai_img]
                    st.session_state.app_state["history_idx"] = 0
                    st.session_state.app_state["current_image_source"] = "ai"
                    st.session_state.app_state["current_img_idx"] = 0
                    st.session_state.app_state["seed"] = initial_seed
                    st.session_state.app_state["redraw_count"] = 0

            except Exception as e:
                st.error(f"생성 실패: {e}")

# =============================================
# 결과 생성 완료 시: 실시간 인터랙션 화면
# =============================================
state = st.session_state.app_state

if state["is_ready"]:
    st.write("---")

    # 1. AI 추천 후킹 카피 선택 (모바일 그리드형)
    st.markdown("#### 💡 AI 추천 후킹 제목 (클릭 시 즉시 변경)")
    cols_btn = st.columns(len(state["copies"]))
    for idx, c_text in enumerate(state["copies"]):
        with cols_btn[idx]:
            if st.button(f"제목 {idx + 1}", key=f"copy_btn_{idx}", use_container_width=True):
                state["active_title"] = c_text
                st.rerun()

    active_bg_img = None
    hist_len = len(state["ai_image_history"])
    if state["current_image_source"] == "ai" and hist_len > 0:
        h_idx = state["history_idx"]
        active_bg_img = state["ai_image_history"][h_idx]
        badge_desc = f"🤖 AI 생성 비주얼 ({h_idx + 1}/{hist_len}번째 기록)"
    elif state["article_images"] and state["current_img_idx"] < len(state["article_images"]):
        active_bg_img = state["article_images"][state["current_img_idx"]]
        badge_desc = f"📰 기사 원문 사진 ({state['current_img_idx'] + 1}/{len(state['article_images'])})"
    else:
        active_bg_img = Image.new("RGB", (1080, 1350), color=(15, 23, 42))
        badge_desc = "🖼️ 맞춤 비주얼"

    rendered_img = render_single_card(
        state["active_title"],
        state["active_sub"],
        active_bg_img,
        state["title_size"],
        state["content_size"],
        state["text_y"],
        category_text=state["category"]
    )

    st.image(
        rendered_img, 
        caption=f"📱 완성된 인스타그램 피드 (1080x1350) · 뱃지: {state['category']} · {badge_desc}", 
        use_container_width=True
    )

    # 이전 생성 이미지로 돌아가기 컨트롤
    if hist_len > 1 and state["current_image_source"] == "ai":
        c_prev, c_info, c_next = st.columns([1, 2, 1])
        with c_prev:
            if st.button("⬅️ 이전 생성 이미지", disabled=(state["history_idx"] == 0), use_container_width=True):
                state["history_idx"] -= 1
                st.rerun()
        with c_info:
            st.markdown(f"<p style='text-align:center; line-height:36px; margin:0;'>생성 기록: <b>{state['history_idx'] + 1}</b> / {hist_len}</p>", unsafe_allow_html=True)
        with c_next:
            if st.button("다음 생성 이미지 ➡️", disabled=(state["history_idx"] == hist_len - 1), use_container_width=True):
                state["history_idx"] += 1
                st.rerun()

    # 이미지 생성/전환 액션 버튼
    col_img1, col_img2 = st.columns(2)
    with col_img1:
        if st.button("🎨 AI로 새로운 이미지 다시 그리기", key="btn_ai_redraw", use_container_width=True):
            state["redraw_count"] += 1
            new_seed = random.randint(10000, 999999) + state["redraw_count"] * 139
            
            with st.spinner("새로운 AI 비주얼을 생성하고 있습니다..."):
                new_ai_img = generate_contextual_ai_image(state["image_prompt"], seed_val=new_seed)
                state["ai_image_history"].append(new_ai_img)
                state["history_idx"] = len(state["ai_image_history"]) - 1
                state["current_image_source"] = "ai"
                state["seed"] = new_seed
            st.rerun()

    with col_img2:
        if state["article_images"]:
            if st.button("📰 기사 원문 실물 사진으로 전환", key="btn_toggle_article_photo", use_container_width=True):
                state["current_image_source"] = "article"
                state["current_img_idx"] = (state["current_img_idx"] + 1) % len(state["article_images"])
                st.rerun()
        else:
            st.button("📰 기사 원문 사진 없음", disabled=True, use_container_width=True)

    # 커스터마이징 패널 (카테고리 뱃지 변경 + 줄바꿈 수정)
    with st.expander("🛠️ 카테고리 뱃지 / 문구 줄바꿈 / 글자 크기 커스터마이징"):
        cat_options = ["NEWS", "CULTURE", "TREND", "TREND ISSUE"]
        curr_cat_idx = cat_options.index(state["category"]) if state["category"] in cat_options else 0
        selected_cat = st.segmented_control("🏷️ 상단 뱃지 카테고리 선택", cat_options, default=cat_options[curr_cat_idx])
        if selected_cat and selected_cat != state["category"]:
            state["category"] = selected_cat
            st.rerun()

        st.caption("💡 팁: 제목이나 본문 입력창에서 원하는 위치에 **엔터(줄바꿈)**를 치시면 입력하신 그대로 카드뉴스 줄바꿈이 적용됩니다.")
        col_ed1, col_ed2 = st.columns(2)
        with col_ed1:
            new_title = st.text_area("제목 문구 수정 (엔터로 줄바꿈 지정 가능)", value=state["active_title"], height=85)
            if new_title != state["active_title"]:
                state["active_title"] = new_title
                st.rerun()
        with col_ed2:
            new_sub = st.text_area("본문 문구 수정 (엔터로 줄바꿈 지정 가능)", value=state["active_sub"], height=85)
            if new_sub != state["active_sub"]:
                state["active_sub"] = new_sub
                st.rerun()

        col_sl1, col_sl2, col_sl3 = st.columns(3)
        with col_sl1:
            state["title_size"] = st.slider("제목 글자 크기", 42, 64, state["title_size"], step=2)
        with col_sl2:
            state["content_size"] = st.slider("본문 글자 크기", 22, 34, state["content_size"], step=2)
        with col_sl3:
            state["text_y"] = st.slider("텍스트 높이 위치", 700, 1000, state["text_y"], step=10)

    buf = BytesIO()
    rendered_img.save(buf, format="PNG")
    st.download_button(
        label="📥 완성된 카드 이미지 저장하기 (1080x1350)",
        data=buf.getvalue(),
        file_name=f"instagram_feed_{datetime.now().strftime('%H%M%S')}.png",
        mime="image/png",
        use_container_width=True
    )

    st.write("---")

    st.markdown("#### 📝 인스타그램 본문 캡션 선택 (기사 팩트 반영)")
    tab_empathy, tab_vote, tab_explain = st.tabs(["❤️ 공감형", "🗳️ 투표형 (찬반)", "📑 정보 설명형 (요약)"])

    caps = state["captions"]
    with tab_empathy:
        st.text_area("공감형 캡션 (복사해서 인스타에 붙여넣으세요)", value=caps.get("empathy", ""), height=170)
    with tab_vote:
        st.text_area("투표형 캡션 (댓글 토론 유도)", value=caps.get("vote", ""), height=170)
    with tab_explain:
        st.text_area("설명형 캡션 (핵심 요약 & 저장 유도)", value=caps.get("explain", ""), height=170)
