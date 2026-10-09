import os
import json
import time
import zipfile
import urllib.parse
from datetime import datetime
from io import BytesIO
import requests
import streamlit as st
from newspaper import Article
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pydantic import BaseModel, Field
from typing import List

# =============================================
# 1. API 키 설정 (Streamlit Cloud Secrets 및 로컬 환경변수 지원)
# =============================================
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key and "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]

client = genai.Client(api_key=api_key)

st.set_page_config(page_title="트렌디 라이프 뉴스 - 맞춤 이미지 카드뉴스", layout="wide")

# 사이드바
st.sidebar.header("🎨 트렌디라이프 스타일 옵션")
title_size = st.sidebar.slider("제목 글자 크기", min_value=46, max_value=64, value=54, step=2)
content_size = st.sidebar.slider("본문 글자 크기", min_value=24, max_value=34, value=28, step=2)
brand_tag = st.sidebar.text_input("상단 브랜딩 태그", value="What's today?")
caption_length = st.sidebar.radio("인스타 캡션 길이", ["짧게 (3~4줄 핵심 요약)", "보통 (인사이트 중심)", "길게 (상세 스토리텔링)"], index=1)

# 헤더
st.markdown("""
<div style="text-align: center; line-height: 1.35; margin-bottom: 25px;">
    <h2 style="color: #0F172A; margin-bottom: 8px; font-weight: 800;">🔥 트렌디라이프 카드뉴스 생성기</h2>
    <p style="color: #475569; font-size: 19px; font-weight: 600; margin: 0;">슬라이드별 내용과 직결된 고화질 맞춤 실사 이미지를 매칭합니다</p>
</div>
""", unsafe_allow_html=True)
st.write("---")

news_url = st.text_input("🔗 뉴스 기사 링크(URL)를 입력하세요.", placeholder="뉴스 기사 링크를 붙여넣고 분석하기를 눌러주세요.")

# 세션 상태 캐시
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
    try:
        return (
            ImageFont.truetype("malgunbd.ttf", t_sz),
            ImageFont.truetype("malgun.ttf", c_sz),
            ImageFont.truetype("malgunbd.ttf", 22),
            ImageFont.truetype("arialbd.ttf", 26)
        )
    except:
        return (ImageFont.load_default(), ImageFont.load_default(), ImageFont.load_default(), ImageFont.load_default())

# 기사 원문 대표 이미지 다운로더
def fetch_top_image(img_url):
    headers = {"User-Agent": "Mozilla/5.0"}
    if img_url:
        try:
            res = requests.get(img_url, headers=headers, timeout=5)
            if res.status_code == 200:
                return Image.open(BytesIO(res.content))
        except:
            pass
    return None

# 문맥 키워드 기반 고화질 실사 이미지 매칭 엔진
def fetch_context_image(keyword, fallback_img=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    clean_keyword = urllib.parse.quote(keyword.strip()) if keyword else "trend"
    
    # 1. Unsplash Source 고해상도 검색
    search_url = f"https://source.unsplash.com/1080x1350/?{clean_keyword}"
    try:
        res = requests.get(search_url, headers=headers, timeout=5, allow_redirects=True)
        if res.status_code == 200 and len(res.content) > 5000:
            return Image.open(BytesIO(res.content))
    except:
        pass
    
    # 2. Picsum 시드 백업
    try:
        seed_hash = abs(hash(keyword)) % 1000
        backup_url = f"https://picsum.photos/seed/{seed_hash}/1080/1350"
        res = requests.get(backup_url, headers=headers, timeout=5)
        if res.status_code == 200:
            return Image.open(BytesIO(res.content))
    except:
        pass

    # 3. 최후 수단: 1번 원문 이미지 블러 배경
    if fallback_img:
        try:
            return fallback_img.copy().filter(ImageFilter.GaussianBlur(15))
        except:
            pass

    return Image.new("RGB", (1080, 1350), color=(25, 33, 44))

# =============================================
# Pydantic 스키마 정의 (JSON 문법 오류 원천 방지)
# =============================================
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

# 1단계 제목 생성 전용 호출 함수
def call_gemini_headlines(prompt):
    candidate_models = ["gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-3.5-flash"]
    last_err = None
    for model_name in candidate_models:
        for attempt in range(1, 3):
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
                last_err = e
                err_str = str(e)
                if "404" in err_str:
                    break
                elif "503" in err_str or "429" in err_str:
                    time.sleep(1.5)
                else:
                    break
    raise Exception(f"헤드라인 생성 실패: {last_err}")

# 2단계 카드뉴스 전체 스크립트 전용 호출 함수
def call_gemini_script(prompt):
    candidate_models = ["gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-3.5-flash"]
    last_err = None
    for model_name in candidate_models:
        for attempt in range(1, 3):
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
                last_err = e
                err_str = str(e)
                if "404" in err_str:
                    break
                elif "503" in err_str or "429" in err_str:
                    time.sleep(1.5)
                else:
                    break
    raise Exception(f"스크립트 생성 실패: {last_err}")

# 트렌디라이프 렌더링 엔진
def render_trendportal_card(page, title, content, base_img, fonts, tag_text="TREND ISSUE"):
    title_font, content_font, tag_font, page_font = fonts
    width, height = 1080, 1350

    base_img = base_img.convert("RGBA")
    img_ratio = base_img.width / base_img.height
    target_ratio = width / height

    if img_ratio > target_ratio:
        new_w = int(base_img.height * target_ratio)
        offset = (base_img.width - new_w) // 2
        base_img = base_img.crop((offset, 0, offset + new_w, base_img.height))
    else:
        new_h = int(base_img.width / target_ratio)
        offset = (base_img.height - new_h) // 2
        base_img = base_img.crop((0, offset, base_img.width, offset + new_h))
    base_img = base_img.resize((width, height))

    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(gradient)

    for y in range(0, 220):
        alpha = int((1.0 - (y / 220.0)) * 130)
        g_draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    for y in range(380, height):
        if y < 800:
            progress = (y - 380) / (800 - 380)
            alpha = int((progress ** 1.6) * 245)
        else:
            alpha = 248
        g_draw.line([(0, y), (width, y)], fill=(8, 12, 20, alpha))

    card = Image.alpha_composite(base_img, gradient).convert("RGB")
    draw = ImageDraw.Draw(card)

    tag_clean = tag_text.strip()
    tag_bbox = draw.textbbox((0, 0), tag_clean, font=tag_font)
    t_text_w = tag_bbox[2] - tag_bbox[0]
    t_text_h = tag_bbox[3] - tag_bbox[1]
    
    badge_w = t_text_w + 58
    badge_h = 48
    bx1, by1 = 64, 64
    bx2, by2 = bx1 + badge_w, by1 + badge_h

    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=24, fill=(15, 23, 42))
    draw.rounded_rectangle([bx1, by1, bx2, by2], radius=24, outline=(255, 255, 255, 90), width=1)
    
    dot_y = by1 + (badge_h // 2)
    draw.ellipse([bx1 + 16, dot_y - 5, bx1 + 26, dot_y + 5], fill=(250, 204, 21))
    draw.text((bx1 + 36, by1 + ((badge_h - t_text_h) // 2) - 2), tag_clean, font=tag_font, fill=(255, 255, 255))
    draw.text((945, 72), f"{page} / 5", font=page_font, fill=(203, 213, 225))

    t_clean = title.replace("\n", " ").strip()
    t_words = t_clean.split()
    t_lines, curr_t = [], ""
    for w in t_words:
        if len(curr_t + w) > 14:
            t_lines.append(curr_t.strip())
            curr_t = w + " "
        else:
            curr_t += w + " "
    if curr_t.strip():
        t_lines.append(curr_t.strip())

    c_clean = content.replace("\n", " ").strip()
    c_words = c_clean.split()
    c_lines, curr_c = [], ""
    for w in c_words:
        if len(curr_c + w) > 24:
            c_lines.append(curr_c.strip())
            curr_c = w + " "
        else:
            curr_c += w + " "
    if curr_c.strip():
        c_lines.append(curr_c.strip())

    line_y = 1230
    c_line_height = content_font.size + 14
    t_line_height = title_font.size + 14

    total_c_h = len(c_lines) * c_line_height
    total_t_h = len(t_lines) * t_line_height

    c_start_y = (line_y - 30) - total_c_h
    t_start_y = (c_start_y - 45) - total_t_h

    draw.rounded_rectangle([64, t_start_y - 24, 128, t_start_y - 16], radius=4, fill=(250, 204, 21))

    curr_y = t_start_y
    for l in t_lines:
        draw.text((64, curr_y), l, font=title_font, fill=(255, 255, 255))
        curr_y += t_line_height

    curr_y = c_start_y
    for l in c_lines:
        draw.text((64, curr_y), l, font=content_font, fill=(218, 224, 233))
        curr_y += c_line_height

    draw.line([64, line_y, 1016, line_y], fill=(51, 65, 85), width=2)
    footer_y = line_y + 20

    if page == 1:
        draw.text((64, footer_y), ">> 옆으로 넘겨서 전체 내용 확인하기", font=tag_font, fill=(250, 204, 21))
    elif page == 5:
        draw.text((64, footer_y), "Q. 여러분의 생각을 댓글로 남겨주세요!", font=tag_font, fill=(52, 211, 153))
    else:
        draw.text((64, footer_y), "@TRENDY.LIFE_NEWWWS", font=tag_font, fill=(148, 163, 184))

    return card

# ---------------------------------------------
# 1단계: 기사 분석
# ---------------------------------------------
if st.button("🔍 1단계: 기사 분석 및 트렌드 헤드라인 3개 추천받기", type="primary", use_container_width=True):
    if not news_url.strip():
        st.warning("뉴스 기사 링크를 입력해 주세요.")
    else:
        with st.spinner("기사 본문을 추출하고 후킹 헤드라인을 생성하는 중입니다..."):
            try:
                article = Article(news_url, language='ko')
                article.download()
                article.parse()
                st.session_state.article_data = {
                    "title": article.title,
                    "text": article.text,
                    "top_image": article.top_image
                }

                cand_prompt = f"""
                당신은 인스타그램 트렌드 뉴스 채널(@trendy.life_newwws)의 수석 카피라이터입니다.
                독자의 시선을 사로잡는 강력한 후킹 제목 3가지를 만드세요.
                - 특수문자나 이모지는 절대 사용하지 말고, 따옴표/물음표만 사용하세요.

                기사 원문 제목: {article.title}
                기사 본문 요약: {article.text[:1200]}
                """
                res = call_gemini_headlines(cand_prompt)
                st.session_state.headline_candidates = res.get("titles", [article.title])
                st.session_state.full_script = None
                st.session_state.rendered_images = []
                st.session_state.zip_data = None
            except Exception as e:
                st.error(f"기사 분석 실패: {e}")

# ---------------------------------------------
# 2단계: 제목 선택 및 맞춤 이미지 피드 생성
# ---------------------------------------------
if st.session_state.headline_candidates:
    st.subheader("💡 마음에 드는 표지 헤드라인을 선택하세요")
    selected_headline = st.radio(
        "추천 헤드라인 목록:",
        st.session_state.headline_candidates,
        index=0
    )

    if st.button("🚀 선택한 헤드라인으로 내용 맞춤 피드 완성하기", type="primary", use_container_width=True):
        art = st.session_state.article_data
        with st.spinner("슬라이드별 내용 맞춤 키워드 및 본문 캡션을 추출 중입니다..."):
            script_prompt = f"""
            당신은 인스타그램 트렌드 매거진(@trendy.life_newwws) 전문 에디터입니다.
            표지 제목은 반드시 "{selected_headline}"을 사용하세요.
            각 슬라이드(1~5번)의 내용(subhead)과 어울리는 고화질 실사 사진을 스톡 포토에서 가져올 수 있도록,
            'img_keyword' 필드에 핵심 영문 검색어 1~2단어(예: 'pampas grass', 'train travel', 'hiking', 'korean food', 'crowd')를 정확히 작성하세요.
            슬라이드 본문(subhead)은 2~3줄 내외(120자 이내)로 작성하고 이모지는 포함하지 마세요.

            기사 원문 제목: {art['title']}
            기사 내용: {art['text']}
            """
            try:
                st.session_state.full_script = call_gemini_script(script_prompt)
                data = st.session_state.full_script
                fonts = load_fonts(title_size, content_size)

                folder_name = datetime.now().strftime("card_news_%Y%m%d_%H%M%S")
                os.makedirs(folder_name, exist_ok=True)

                saved_images = []
                top_img_original = fetch_top_image(art.get("top_image"))

                for slide in data["slides"]:
                    page = slide["page"]
                    title = slide["headline"]
                    content = slide["subhead"]
                    keyword = slide.get("img_keyword", "trend")

                    # 1번 슬라이드는 기사 원문 사진 우선, 2~5번은 슬라이드 내용 맞춤 사진
                    base_img = None
                    if page == 1 and top_img_original:
                        base_img = top_img_original
                    else:
                        base_img = fetch_context_image(keyword, fallback_img=top_img_original)

                    card = render_trendportal_card(page, title, content, base_img, fonts, tag_text=brand_tag)
                    
                    save_path = os.path.join(folder_name, f"slide_{page}.png")
                    card.save(save_path)
                    saved_images.append(card)

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
    st.success("🎉 각 슬라이드 내용과 매칭된 실사 이미지가 정상 합성되었습니다!")

    st.download_button(
        label="📦 트렌디라이프 피드 5장 + 캡션 한 번에 다운로드 (ZIP)",
        data=st.session_state.zip_data,
        file_name=st.session_state.zip_filename,
        mime="application/zip",
        use_container_width=True
    )

    st.write("---")
    st.subheader("🖼️ 생성된 피드 미리보기 (슬라이드별 맞춤 이미지)")
    grid_cols = st.columns(5)
    for idx, img in enumerate(st.session_state.rendered_images):
        with grid_cols[idx]:
            st.image(img, caption=f"{idx + 1}번 슬라이드", use_container_width=True)

    st.subheader("📝 인스타그램 캡션 복사")
    st.text_area("캡션 및 해시태그", value=st.session_state.full_script.get("caption", ""), height=160)