import os
import re
import json
import time
import zipfile
import urllib.parse
from datetime import datetime
from io import BytesIO
import requests
import streamlit as st
import numpy as np
from newspaper import Article
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageStat
from pydantic import BaseModel
from typing import List

st.set_page_config(page_title="트렌디라이프 뉴스 - 스마트 멀티기사 카드뉴스", layout="wide")

# =============================================
# 1. API 키 설정 (Secrets + 사이드바 임시 입력 지원)
# =============================================
st.sidebar.header("⚙️ 개발 & 테스트 설정")

user_custom_key = st.sidebar.text_input(
    "🔑 임시 Gemini API Key (선택)",
    type="password",
    help="기존 키가 429 한도 초과되었을 때 다른 구글 계정의 키를 입력하면 즉시 교체 적용됩니다."
)

api_key = user_custom_key.strip() if user_custom_key.strip() else os.environ.get("GEMINI_API_KEY")
if not api_key and "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]

client = None
if api_key:
    try:
        client = genai.Client(api_key=api_key)
    except Exception as e:
        st.sidebar.error(f"API 클라이언트 초기화 실패: {e}")

# 스타일 옵션
st.sidebar.header("🎨 트렌디라이프 UX/디자인 옵션")
image_mode = st.sidebar.radio(
    "📸 이미지 생성 모드 선택",
    [
        "🎬 드라마/영화/콘텐츠 모드 (스틸컷·첨부사진 집중 활용)",
        "🎨 일반 맞춤형 모드 (표지 사진 + 슬라이드별 맞춤 실사)"
    ],
    index=0
)

cover_source_choice = st.sidebar.radio(
    "🌟 1번 표지 슬라이드 이미지 선택",
    [
        "기사 원문 대표 이미지 우선",
        "내가 직접 첨부한 이미지 우선"
    ],
    index=0
)

title_size = st.sidebar.slider("제목 글자 크기", min_value=46, max_value=64, value=52, step=2)
content_size = st.sidebar.slider("본문 글자 크기", min_value=24, max_value=34, value=26, step=2)
brand_tag = st.sidebar.text_input("상단 브랜딩 태그", value="TREND ISSUE")

# 메인 헤더
st.markdown("""
<div style="text-align: center; line-height: 1.35; margin-bottom: 25px;">
    <h2 style="color: #0F172A; margin-bottom: 8px; font-weight: 800;">🔥 트렌디라이프 매거진 카드뉴스 생성기</h2>
    <p style="color: #475569; font-size: 19px; font-weight: 600; margin: 0;">외부 무작위 이미지 원천 차단 & 피사체 보호 프레이밍 적용</p>
</div>
""", unsafe_allow_html=True)
st.write("---")

col_url1, col_url2 = st.columns(2)
with col_url1:
    news_url_1 = st.text_input("🔗 첫 번째 뉴스 기사 링크 (필수)", placeholder="메인 기사 URL을 입력하세요.")
with col_url2:
    news_url_2 = st.text_input("🔗 두 번째 뉴스 기사 링크 (선택)", placeholder="관련 추가 기사 URL을 입력하세요.")

uploaded_custom_files = st.file_uploader(
    "📁 직접 사용할 추가 고화질 이미지 첨부 (선택, 다중 선택 가능)",
    type=["png", "jpg", "jpeg", "webp"],
    accept_multiple_files=True,
    help="기사 스틸컷이 부족할 때 직접 캡처한 사진이나 포스터를 올리면 엉뚱한 이미지 없이 5장이 완성됩니다."
)

# 세션 상태 초기화
if "article_data" not in st.session_state:
    st.session_state.article_data = None
if "headline_candidates" not in st.session_state:
    st.session_state.headline_candidates = []
if "full_script" not in st.session_state:
    st.session_state.full_script = None
if "rendered_images" not in st.session_state:
    st.session_state.rendered_images = []
if "zip_data" not in st.session_state:
    st.session_state.zip_data = None
if "zip_filename" not in st.session_state:
    st.session_state.zip_filename = ""

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

    title_font, content_font, badge_font, page_font = None, None, None, None

    for f_path in font_candidates_bold:
        try:
            title_font = ImageFont.truetype(f_path, t_sz)
            badge_font = ImageFont.truetype(f_path, 20)
            page_font = ImageFont.truetype(f_path, 24)
            break
        except:
            continue

    for f_path in font_candidates_regular:
        try:
            content_font = ImageFont.truetype(f_path, c_sz)
            break
        except:
            continue

    if not title_font:
        title_font = ImageFont.load_default()
    if not content_font:
        content_font = ImageFont.load_default()
    if not badge_font:
        badge_font = ImageFont.load_default()
    if not page_font:
        page_font = ImageFont.load_default()

    return (title_font, content_font, badge_font, page_font)

# ---------------------------------------------
# 텍스트 노이즈 정제기 (Journalism Noise Sanitizer)
# ---------------------------------------------
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

# ---------------------------------------------
# 피사체 얼굴 절단 방지 프레이밍 엔진
# ---------------------------------------------
def smart_fit_or_crop(base_img, target_w=1080, target_h=1350):
    base_img = base_img.convert("RGBA")
    src_w, src_h = base_img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h

    # 가로가 긴 투샷/단체컷: 레터박스 블러 처리로 얼굴 보존
    if src_ratio > 1.15:
        bg_scale = max(target_w / src_w, target_h / src_h)
        bg_w, bg_h = int(src_w * bg_scale), int(src_h * bg_scale)
        bg = base_img.resize((bg_w, bg_h), Image.Resampling.LANCZOS)
        
        left = (bg_w - target_w) // 2
        top = (bg_h - target_h) // 2
        bg = bg.crop((left, top, left + target_w, top + target_h))
        bg = bg.filter(ImageFilter.GaussianBlur(35))
        
        dark_overlay = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 95))
        bg = Image.alpha_composite(bg, dark_overlay)

        fit_scale = min(target_w / src_w, (target_h * 0.65) / src_h)
        fg_w, fg_h = int(src_w * fit_scale), int(src_h * fit_scale)
        fg = base_img.resize((fg_w, fg_h), Image.Resampling.LANCZOS)

        pos_x = (target_w - fg_w) // 2
        pos_y = 120
        bg.paste(fg, (pos_x, pos_y), fg)
        return bg

    # 세로형 또는 정방형 사진: 상단 여백 확보 크롭
    if src_ratio > target_ratio:
        new_w = int(src_h * target_ratio)
        left_offset = int((src_w - new_w) * 0.45)
        cropped = base_img.crop((left_offset, 0, left_offset + new_w, src_h))
    else:
        new_h = int(src_w / target_ratio)
        top_offset = int((src_h - new_h) * 0.10)
        top_offset = max(0, min(top_offset, src_h - new_h))
        cropped = base_img.crop((0, top_offset, src_w, top_offset + new_h))

    return cropped.resize((target_w, target_h), Image.Resampling.LANCZOS)

# ---------------------------------------------
# 로고 및 단색 그래픽 자동 필터링 (Variance 체크)
# ---------------------------------------------
def is_valid_photo(pil_img):
    if pil_img.width < 350 or pil_img.height < 350:
        return False
    
    gray = pil_img.convert("L")
    stat = ImageStat.Stat(gray)
    stddev = stat.stddev[0]
    
    if stddev < 35:
        return False

    ratio = pil_img.width / pil_img.height
    if ratio < 0.45 or ratio > 2.6:
        return False

    return True

# ---------------------------------------------
# 지각 해시 (dHash) 기반 중복 검증
# ---------------------------------------------
def calculate_dhash(image):
    img_gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(img_gray.getdata())
    diff = []
    for row in range(8):
        for col in range(8):
            diff.append(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return diff

def is_duplicate_visual(new_hash, existing_hashes, threshold=10):
    for h in existing_hashes:
        dist = sum(el1 != el2 for el1, el2 in zip(new_hash, h))
        if dist <= threshold:
            return True
    return False

def download_image_pil(img_url):
    if not img_url:
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(img_url, headers=headers, timeout=6)
        if res.status_code == 200 and len(res.content) > 6000:
            img = Image.open(BytesIO(res.content))
            if is_valid_photo(img):
                return img
    except:
        pass
    return None

# Pydantic 모델
class SlideItem(BaseModel):
    page: int
    headline: str
    subhead: str
    img_keyword: str = "trend"

class CardNewsResponse(BaseModel):
    slides: List[SlideItem]
    caption: str

class HeadlineCandidates(BaseModel):
    titles: List[str]

FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.1-pro-preview"
]

def call_gemini_headlines(prompt, default_title):
    clean_default = sanitize_korean_text(default_title)
    if not client:
        return {"titles": [clean_default, f"'{clean_default[:18]}' 핵심 쟁점", f"{clean_default[:18]} 집중 조명"]}

    for model_name in FALLBACK_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=HeadlineCandidates,
                    temperature=0.7
                )
            )
            return json.loads(res.text)
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "404" in err_str:
                continue
            time.sleep(1)

    st.info("💡 AI 할당량 소진으로 로컬 스마트 분석 모드로 헤드라인을 생성했습니다.")
    return {
        "titles": [
            f"\"{clean_default}\"",
            f"요즘 화제라는 '{clean_default[:16]}' 무슨 일일까?",
            f"실시간 시선 집중된 '{clean_default[:16]}' 핵심 정리"
        ]
    }

def call_gemini_script(prompt, article_title, article_text):
    if not client:
        return build_local_editorial_script(article_title, article_text)

    for model_name in FALLBACK_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=CardNewsResponse,
                    temperature=0.7
                )
            )
            return json.loads(res.text)
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "404" in err_str:
                continue
            time.sleep(1)

    st.warning("⚠️ AI 일일 사용량이 소진되어 매거진 전용 완성형 스토리텔링으로 작성합니다.")
    return build_local_editorial_script(article_title, article_text)

# ---------------------------------------------
# 매거진 완결형 스토리텔링 엔진 (Local Fallback)
# ---------------------------------------------
def build_local_editorial_script(title, text):
    clean_title = sanitize_korean_text(title)
    
    raw_sentences = [sanitize_korean_text(s) for s in re.split(r'(?<=[.?!])\s+', text)]
    valid_sentences = [
        s for s in raw_sentences 
        if len(s) >= 25 and not any(kw in s for kw in ["스튜디오", "제작사", "연출", "극본", "기자", "배급", "사진="])
    ]

    s1 = valid_sentences[0] if len(valid_sentences) > 0 else f"{clean_title}이 압도적인 비주얼과 스토리로 화제를 모으고 있습니다."
    s2 = valid_sentences[1] if len(valid_sentences) > 1 else "예측을 뒤흔드는 파격적인 캐릭터와 긴장감 넘치는 전개가 펼쳐집니다."
    s3 = valid_sentences[2] if len(valid_sentences) > 2 else "타협 없는 시원한 전개와 거침없는 카타르시스가 시청자의 시선을 사로잡습니다."
    s4 = valid_sentences[3] if len(valid_sentences) > 3 else "탄탄한 연기력을 자랑하는 배우들의 숨 막히는 호흡이 몰입도를 극대화합니다."

    return {
        "slides": [
            {
                "page": 1,
                "headline": clean_title,
                "subhead": s1,
                "img_keyword": "drama main actor"
            },
            {
                "page": 2,
                "headline": "도대체 무슨 일일까?",
                "subhead": s2,
                "img_keyword": "drama suspense"
            },
            {
                "page": 3,
                "headline": "거침없는 사이다 매력",
                "subhead": s3,
                "img_keyword": "charismatic scene"
            },
            {
                "page": 4,
                "headline": "믿고 보는 배우 라인업",
                "subhead": s4,
                "img_keyword": "intense drama"
            },
            {
                "page": 5,
                "headline": "오늘 밤 첫 방송 시작",
                "subhead": "안방극장에 통쾌한 전율을 선사할 화제의 신작을 오늘 밤 본방송으로 직접 확인해 보세요.",
                "img_keyword": "broadcasting"
            }
        ],
        "caption": f"🔥 {clean_title}\n\n화제의 신작 소식! 과연 어떤 통쾌한 활약을 보여줄까요?\n\n#드라마 #트렌드 #이슈 #트렌디라이프"
    }

# ---------------------------------------------
# UX 가독성 단락 조판 (Measure Formatting)
# ---------------------------------------------
def format_lines_by_measure(text, max_chars_per_line):
    words = text.strip().split()
    lines, curr = [], ""
    for w in words:
        if len(curr + w) > max_chars_per_line:
            if curr.strip():
                lines.append(curr.strip())
            curr = w + " "
        else:
            curr += w + " "
    if curr.strip():
        lines.append(curr.strip())
    return lines

def render_trendportal_card(page, total_pages, title, content, base_img, fonts, tag_text="TREND ISSUE"):
    title_font, content_font, tag_font, page_font = fonts
    width, height = 1080, 1350

    base_img = smart_fit_or_crop(base_img, width, height)

    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(gradient)

    for y in range(0, 180):
        alpha = int((1.0 - (y / 180.0)) * 85)
        g_draw.line([(0, y), (width, y)], fill=(5, 8, 15, alpha))

    for y in range(680, height):
        if y < 980:
            progress = (y - 680) / (980 - 680)
            alpha = int((progress ** 1.8) * 235)
        else:
            alpha = 250
        g_draw.line([(0, y), (width, y)], fill=(9, 13, 20, alpha))

    card = Image.alpha_composite(base_img, gradient).convert("RGB")
    draw = ImageDraw.Draw(card)

    tag_clean = tag_text.strip()
    tag_bbox = draw.textbbox((0, 0), tag_clean, font=tag_font)
    t_text_w = tag_bbox[2] - tag_bbox[0]
    t_text_h = tag_bbox[3] - tag_bbox[1]
    
    badge_w = t_text_w + 54
    badge_h = 44
    bx1, by1 = 64, 64
    bx2, by2 = bx1 + badge_w, by1 + badge_h

    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=22, fill=(15, 23, 42, 220))
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=22, outline=(255, 255, 255, 70), width=1)
    
    dot_y = by1 + (badge_h // 2)
    draw.ellipse([bx1 + 16, dot_y - 4, bx1 + 24, dot_y + 4], fill=(250, 204, 21))
    draw.text((bx1 + 34, by1 + ((badge_h - t_text_h) // 2) - 2), tag_clean, font=tag_font, fill=(255, 255, 255))
    draw.text((930, 70), f"{page} / {total_pages}", font=page_font, fill=(203, 213, 225))

    clean_title = sanitize_korean_text(title)
    clean_content = sanitize_korean_text(content)

    t_lines = format_lines_by_measure(clean_title, max_chars_per_line=13)
    c_lines = format_lines_by_measure(clean_content, max_chars_per_line=21)

    line_y = 1230
    c_line_height = content_font.size + 14
    t_line_height = title_font.size + 14

    total_c_h = len(c_lines) * c_line_height
    total_t_h = len(t_lines) * t_line_height

    c_start_y = (line_y - 32) - total_c_h
    t_start_y = (c_start_y - 42) - total_t_h

    draw.rounded_rectangle([64, t_start_y - 20, 114, t_start_y - 13], radius=4, fill=(250, 204, 21))

    curr_y = t_start_y
    for l in t_lines:
        draw.text((64, curr_y), l, font=title_font, fill=(255, 255, 255))
        curr_y += t_line_height

    curr_y = c_start_y
    for l in c_lines:
        draw.text((64, curr_y), l, font=content_font, fill=(226, 232, 240))
        curr_y += c_line_height

    draw.line([64, line_y, 1016, line_y], fill=(51, 65, 85, 180), width=2)
    footer_y = line_y + 20

    if page == 1:
        draw.text((64, footer_y), ">> 옆으로 넘겨서 전체 내용 확인하기", font=tag_font, fill=(250, 204, 21))
    elif page == total_pages:
        draw.text((64, footer_y), "Q. 여러분의 생각을 댓글로 남겨주세요!", font=tag_font, fill=(52, 211, 153))
    else:
        draw.text((64, footer_y), "@TRENDY.LIFE_NEWWWS · DAILY ISSUE", font=tag_font, fill=(148, 163, 184))

    return card

# ---------------------------------------------
# 1단계: 기사 분석
# ---------------------------------------------
if st.button("🔍 1단계: 기사 분석 및 헤드라인 추천받기", type="primary", use_container_width=True):
    if not news_url_1.strip():
        st.warning("첫 번째 뉴스 기사 링크를 입력해 주세요.")
    else:
        with st.spinner("기사 본문과 스틸컷 이미지들을 분석 및 정제하고 있습니다..."):
            try:
                raw_image_urls = []

                art1 = Article(news_url_1, language='ko')
                art1.download()
                art1.parse()

                clean_art1_title = sanitize_korean_text(art1.title)
                clean_art1_text = sanitize_korean_text(art1.text)

                combined_title = clean_art1_title
                combined_text = f"[기사 1]\n{clean_art1_text}"
                top_image_url = art1.top_image

                if art1.top_image:
                    raw_image_urls.append(art1.top_image)
                for img in art1.images:
                    raw_image_urls.append(img)

                if news_url_2.strip():
                    try:
                        art2 = Article(news_url_2, language='ko')
                        art2.download()
                        art2.parse()
                        clean_art2_text = sanitize_korean_text(art2.text)
                        combined_text += f"\n\n[기사 2]\n{clean_art2_text}"
                        if not top_image_url and art2.top_image:
                            top_image_url = art2.top_image
                        if art2.top_image:
                            raw_image_urls.append(art2.top_image)
                        for img in art2.images:
                            raw_image_urls.append(img)
                    except Exception as e:
                        st.warning(f"두 번째 기사 분석 제외: {e}")

                unique_urls = []
                seen_urls = set()
                for u in raw_image_urls:
                    if u and u.startswith("http") and not u.endswith(".svg"):
                        clean_u = u.split("?")[0]
                        if clean_u not in seen_urls:
                            seen_urls.add(clean_u)
                            unique_urls.append(u)

                st.session_state.article_data = {
                    "title": combined_title,
                    "text": combined_text,
                    "top_image_url": top_image_url,
                    "image_urls": unique_urls
                }

                cand_prompt = f"""
                당신은 인스타그램 트렌드 매거진(@trendy.life_newwws)의 수석 카피라이터이자 UX 에디터입니다.
                독자의 시선을 사로잡는 강력한 후킹 제목 3가지를 만드세요.
                - 기자 이름, 날짜, 언론사명, 괄호 따위의 노이즈는 절대 넣지 마세요.
                - 따옴표와 핵심 키워드만 사용하여 완성하세요.

                기사 원문 제목: {combined_title}
                기사 본문 요약: {combined_text[:1400]}
                """
                res = call_gemini_headlines(cand_prompt, combined_title)
                st.session_state.headline_candidates = [sanitize_korean_text(t) for t in res.get("titles", [combined_title])]
                st.session_state.full_script = None
                st.session_state.rendered_images = []
                st.session_state.zip_data = None
            except Exception as e:
                st.error(f"기사 분석 실패: {e}")

# ---------------------------------------------
# 2단계: 제목 선택 및 맞춤 카드뉴스 생성
# ---------------------------------------------
if st.session_state.headline_candidates:
    st.subheader("💡 마음에 드는 표지 헤드라인을 선택하세요")
    selected_headline = st.radio(
        "추천 헤드라인 목록:",
        st.session_state.headline_candidates,
        index=0
    )

    if st.button("🚀 선택한 헤드라인으로 카드뉴스 완성하기", type="primary", use_container_width=True):
        art = st.session_state.article_data
        with st.spinner("피사체 보호 프레이밍 및 조판을 적용 중입니다..."):
            script_prompt = f"""
            당신은 인스타그램 트렌드 매거진(@trendy.life_newwws)의 전문 에디터입니다.
            표지 제목은 반드시 "{selected_headline}"을 사용하세요.
            반드시 5장의 슬라이드(page 1부터 5까지)를 구성하세요.
            
            [절대 작성 수칙 - 엄격 준수]:
            1. 문장은 중간에 끊기지 않도록 완결된 1개의 문장(또는 자연스러운 2개 문장, 마침표 필수)으로 70~90자 내외로 작성하세요.
            2. '스튜디오S', '극본 편성근', '아이즈 최재욱 기자', '28일 공개' 같은 제작사 정보, 날짜, 기사 정보는 절대 넣지 마세요.
            3. 각 슬라이드의 역할:
               - 1번: 작품/이슈의 핵심 사건 개요
               - 2번: 스토리의 흥미진진한 갈등 배경
               - 3번: 주인공/핵심 인물의 파격적이고 사이다 같은 매력 포인트
               - 4번: 주요 라인업 배우들의 활약과 연기 대립 구도
               - 5번: '댓글 질문'을 본문에 쓰지 말고, 작품/사건에 대한 최종 기대감을 매끄럽게 서술하세요.
            4. 각 슬라이드의 어울리는 검색 키워드를 'img_keyword'에 영어 1~2단어로 작성하세요.

            기사 내용: {art['text']}
            """
            try:
                st.session_state.full_script = call_gemini_script(script_prompt, art['title'], art['text'])
                data = st.session_state.full_script
                fonts = load_fonts(title_size, content_size)

                folder_name = datetime.now().strftime("card_news_%Y%m%d_%H%M%S")
                os.makedirs(folder_name, exist_ok=True)

                # ========================================================
                # [이미지 풀 구축] 로고/단색 그래픽 필터링 및 중복 검증
                # ========================================================
                unique_images_pool = []
                unique_hashes = []
                
                user_first_img = None
                article_cover_img = download_image_pil(art.get("top_image_url"))

                # 1) 사용자가 직접 업로드한 이미지 로드
                if uploaded_custom_files:
                    for up_file in uploaded_custom_files:
                        try:
                            pil_u = Image.open(up_file)
                            if is_valid_photo(pil_u):
                                h = calculate_dhash(pil_u)
                                if not is_duplicate_visual(h, unique_hashes, threshold=10):
                                    unique_hashes.append(h)
                                    unique_images_pool.append(pil_u)
                                    if user_first_img is None:
                                        user_first_img = pil_u
                        except:
                            pass

                # 2) 기사 본문 크롤링 이미지 로드 (로고 및 단색 심볼 철저 배제)
                for img_url in art.get("image_urls", []):
                    img_obj = download_image_pil(img_url)
                    if img_obj:
                        h = calculate_dhash(img_obj)
                        if not is_duplicate_visual(h, unique_hashes, threshold=10):
                            unique_hashes.append(h)
                            unique_images_pool.append(img_obj)

                # 1번 슬라이드 이미지 결정
                cover_candidate = None
                if "내가 직접 첨부" in cover_source_choice and user_first_img:
                    cover_candidate = user_first_img
                elif article_cover_img:
                    cover_candidate = article_cover_img
                elif len(unique_images_pool) > 0:
                    cover_candidate = unique_images_pool[0]

                # 표지로 쓴 이미지는 2~5번 슬라이드에서 중복 사용 제외
                used_pool = []
                cover_hash = calculate_dhash(cover_candidate) if cover_candidate else None
                for img in unique_images_pool:
                    if cover_hash and is_duplicate_visual(calculate_dhash(img), [cover_hash], threshold=10):
                        continue
                    used_pool.append(img)

                saved_images = []
                fallback_cover = cover_candidate if cover_candidate else (unique_images_pool[0] if unique_images_pool else None)
                total_slides_count = len(data["slides"])

                for idx, slide in enumerate(data["slides"]):
                    page = slide["page"]
                    title = slide["headline"]
                    content = slide["subhead"]

                    base_img = None

                    # 1번 슬라이드
                    if page == 1:
                        if cover_candidate:
                            base_img = cover_candidate
                        elif len(used_pool) > 0:
                            base_img = used_pool.pop(0)
                        elif fallback_cover:
                            base_img = fallback_cover
                        else:
                            base_img = Image.new("RGB", (1080, 1350), color=(15, 23, 42))
                    else:
                        # 2~5번 슬라이드: 준비된 스틸컷 소진
                        if len(used_pool) > 0:
                            base_img = used_pool.pop(0)
                        else:
                            # [핵심] 스틸컷이 소진되었을 때 엉뚱한 외부 랜덤 사진(딸기 등) 절대 금지!
                            # 메인 대표 스틸컷을 블러/다크 톤다운하여 세련된 엔딩 카드로 연출
                            if fallback_cover:
                                base_img = fallback_cover.copy().filter(ImageFilter.GaussianBlur(18))
                            else:
                                base_img = Image.new("RGB", (1080, 1350), color=(15, 23, 42))

                    card = render_trendportal_card(page, total_slides_count, title, content, base_img, fonts, tag_text=brand_tag)
                    
                    save_path = os.path.join(folder_name, f"slide_{page}.png")
                    card.save(save_path)
                    saved_images.append(card)

                # ZIP 패키징
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                    for idx, img in enumerate(saved_images):
                        img_buffer = BytesIO()
                        img.save(img_buffer, format="PNG")
                        zip_file.writestr(f"slide_{idx + 1}.png", img_buffer.getvalue())
                    zip_file.writestr("caption.txt", data.get("caption", "").encode("utf-8"))
                
                zip_buffer.seek(0)

                st.session_state.rendered_images = saved_images
                st.session_state.zip_data = zip_buffer.getvalue()
                st.session_state.zip_filename = f"{folder_name}.zip"

            except Exception as e:
                st.error(f"생성 실패: {e}")

# ---------------------------------------------
# 3단계: 화면 표시
# ---------------------------------------------
if st.session_state.rendered_images and st.session_state.zip_data:
    st.success("🎉 외부 엉뚱한 이미지 없이 완성도 높은 매거진 피드가 생성되었습니다!")

    st.download_button(
        label="📦 트렌디라이프 피드 한 번에 다운로드 (ZIP)",
        data=st.session_state.zip_data,
        file_name=st.session_state.zip_filename,
        mime="application/zip",
        use_container_width=True
    )

    st.write("---")
    st.subheader("🖼️ 생성된 피드 미리보기")
    
    total_imgs = len(st.session_state.rendered_images)
    grid_cols = st.columns(total_imgs)
    for idx, img in enumerate(st.session_state.rendered_images):
        with grid_cols[idx]:
            st.image(img, caption=f"{idx + 1}번 슬라이드", use_container_width=True)

    st.subheader("📝 인스타그램 캡션 복사")
    st.text_area("캡션 및 해시태그", value=st.session_state.full_script.get("caption", ""), height=160)
