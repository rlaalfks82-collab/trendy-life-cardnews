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
from pydantic import BaseModel
from typing import List

# =============================================
# 1. API 키 설정 (Streamlit Cloud Secrets / 로컬 환경변수 지원)
# =============================================
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key and "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]

client = genai.Client(api_key=api_key)

st.set_page_config(page_title="트렌디라이프 뉴스 - 스마트 멀티기사 카드뉴스", layout="wide")

# 사이드바 설정
st.sidebar.header("🎨 트렌디라이프 옵션")

image_mode = st.sidebar.radio(
    "📸 이미지 생성 모드 선택",
    [
        "🎬 드라마/영화/콘텐츠 모드 (두 기사의 스틸컷·포스터 집중 활용)",
        "🎨 일반 맞춤형 모드 (기사 대표사진 + 슬라이드별 맞춤 실사)"
    ],
    index=0
)

title_size = st.sidebar.slider("제목 글자 크기", min_value=46, max_value=64, value=54, step=2)
content_size = st.sidebar.slider("본문 글자 크기", min_value=24, max_value=34, value=28, step=2)
brand_tag = st.sidebar.text_input("상단 브랜딩 태그", value="TREND ISSUE")
caption_length = st.sidebar.radio("인스타 캡션 길이", ["짧게 (3~4줄 요약)", "보통 (인사이트 중심)", "길게 (상세 스토리텔링)"], index=1)

# 헤더
st.markdown("""
<div style="text-align: center; line-height: 1.35; margin-bottom: 25px;">
    <h2 style="color: #0F172A; margin-bottom: 8px; font-weight: 800;">🔥 트렌디라이프 멀티기사 카드뉴스 생성기</h2>
    <p style="color: #475569; font-size: 19px; font-weight: 600; margin: 0;">2개 기사의 스틸컷과 팩트를 통합 분석하여 고감도 피드를 완성합니다</p>
</div>
""", unsafe_allow_html=True)
st.write("---")

col_url1, col_url2 = st.columns(2)
with col_url1:
    news_url_1 = st.text_input("🔗 첫 번째 뉴스 기사 링크 (필수)", placeholder="메인 기사 URL을 입력하세요.")
with col_url2:
    news_url_2 = st.text_input("🔗 두 번째 뉴스 기사 링크 (선택 / 추가 스틸컷 확보)", placeholder="관련 추가 기사 URL을 입력하세요.")

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

    title_font = None
    content_font = None
    badge_font = None
    page_font = None

    for f_path in font_candidates_bold:
        try:
            title_font = ImageFont.truetype(f_path, t_sz)
            badge_font = ImageFont.truetype(f_path, 22)
            page_font = ImageFont.truetype(f_path, 26)
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

def download_image_from_url(img_url):
    if not img_url:
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(img_url, headers=headers, timeout=5)
        if res.status_code == 200 and len(res.content) > 3000:
            return Image.open(BytesIO(res.content))
    except:
        pass
    return None

def fetch_keyword_stock_image(keyword, fallback_img=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    clean_keyword = urllib.parse.quote(keyword.strip()) if keyword else "editorial"
    
    search_url = f"https://source.unsplash.com/1080x1350/?{clean_keyword}"
    try:
        res = requests.get(search_url, headers=headers, timeout=5, allow_redirects=True)
        if res.status_code == 200 and len(res.content) > 5000:
            return Image.open(BytesIO(res.content))
    except:
        pass

    try:
        seed_hash = abs(hash(keyword)) % 1000
        res = requests.get(f"https://picsum.photos/seed/{seed_hash}/1080/1350", headers=headers, timeout=5)
        if res.status_code == 200:
            return Image.open(BytesIO(res.content))
    except:
        pass

    if fallback_img:
        try:
            return fallback_img.copy().filter(ImageFilter.GaussianBlur(15))
        except:
            pass

    return Image.new("RGB", (1080, 1350), color=(25, 33, 44))

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
        draw.text((64, footer_y), "@TRENDY.LIFE_NEWWWS · DAILY ISSUE", font=tag_font, fill=(148, 163, 184))

    return card

# ---------------------------------------------
# 1단계: 기사 2개 통합 분석
# ---------------------------------------------
if st.button("🔍 1단계: 기사 분석 및 헤드라인 추천받기", type="primary", use_container_width=True):
    if not news_url_1.strip():
        st.warning("첫 번째 뉴스 기사 링크를 입력해 주세요.")
    else:
        with st.spinner("기사 본문과 스틸컷 이미지들을 추출하고 있습니다..."):
            try:
                # 기사 1 파싱
                art1 = Article(news_url_1, language='ko')
                art1.download()
                art1.parse()

                combined_title = art1.title
                combined_text = f"[기사 1]\n{art1.text}"
                all_images = [img for img in art1.images if img.startswith("http") and not img.endswith(".svg")]
                top_image = art1.top_image

                # 기사 2가 있을 경우 통합
                if news_url_2.strip():
                    try:
                        art2 = Article(news_url_2, language='ko')
                        art2.download()
                        art2.parse()
                        combined_text += f"\n\n[기사 2]\n{art2.text}"
                        for img in art2.images:
                            if img.startswith("http") and not img.endswith(".svg") and img not in all_images:
                                all_images.append(img)
                        if not top_image and art2.top_image:
                            top_image = art2.top_image
                    except Exception as e:
                        st.warning(f"두 번째 기사 분석에 실패하여 첫 번째 기사로만 진행합니다: {e}")

                st.session_state.article_data = {
                    "title": combined_title,
                    "text": combined_text,
                    "top_image": top_image,
                    "all_images": all_images
                }

                cand_prompt = f"""
                당신은 인스타그램 트렌드 뉴스 채널(@trendy.life_newwws)의 수석 카피라이터입니다.
                기사 내용을 토대로 독자의 시선을 사로잡는 강력한 후킹 제목 3가지를 만드세요.
                - 특수문자나 이모지는 절대 사용하지 말고, 따옴표/물음표만 사용하세요.

                기사 원문 제목: {combined_title}
                기사 본문 요약: {combined_text[:1400]}
                """
                res = call_gemini_headlines(cand_prompt)
                st.session_state.headline_candidates = res.get("titles", [combined_title])
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
        with st.spinner("슬라이드 대본 작성 및 이미지를 합성 중입니다..."):
            script_prompt = f"""
            당신은 인스타그램 트렌드 매거진(@trendy.life_newwws) 전문 에디터입니다.
            표지 제목은 반드시 "{selected_headline}"을 사용하세요.
            각 슬라이드의 내용과 가장 잘 어울리는 검색 키워드를 'img_keyword'에 영어 1~2단어로 작성하세요.
            슬라이드 본문(subhead)은 2~3줄 내외(120자 이내)로 작성하고 이모지는 포함하지 마세요.

            기사 내용: {art['text']}
            """
            try:
                st.session_state.full_script = call_gemini_script(script_prompt)
                data = st.session_state.full_script
                fonts = load_fonts(title_size, content_size)

                folder_name = datetime.now().strftime("card_news_%Y%m%d_%H%M%S")
                os.makedirs(folder_name, exist_ok=True)

                saved_images = []
                top_img_original = download_image_from_url(art.get("top_image"))

                # 기사 내 모든 스틸컷 풀 다운로드 및 필터링
                article_images_pool = []
                for img_url in art.get("all_images", []):
                    img_obj = download_image_from_url(img_url)
                    if img_obj and (img_obj.width >= 350 or img_obj.height >= 350):
                        article_images_pool.append(img_obj)

                for idx, slide in enumerate(data["slides"]):
                    page = slide["page"]
                    title = slide["headline"]
                    content = slide["subhead"]
                    keyword = slide.get("img_keyword", "trend")

                    base_img = None

                    # 이미지 배치 로직
                    if "드라마/영화" in image_mode:
                        if idx < len(article_images_pool):
                            base_img = article_images_pool[idx]
                        elif top_img_original:
                            base_img = top_img_original
                        else:
                            base_img = fetch_keyword_stock_image(keyword, fallback_img=top_img_original)
                    else:
                        if page == 1 and top_img_original:
                            base_img = top_img_original
                        elif page == 2 and len(article_images_pool) > 0:
                            base_img = article_images_pool[0]
                        else:
                            base_img = fetch_keyword_stock_image(keyword, fallback_img=top_img_original)

                    card = render_trendportal_card(page, title, content, base_img, fonts, tag_text=brand_tag)
                    
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
    st.success("🎉 두 기사의 스틸컷과 팩트를 반영한 피드 5장이 완성되었습니다!")

    st.download_button(
        label="📦 트렌디라이프 피드 5장 + 캡션 한 번에 다운로드 (ZIP)",
        data=st.session_state.zip_data,
        file_name=st.session_state.zip_filename,
        mime="application/zip",
        use_container_width=True
    )

    st.write("---")
    st.subheader("🖼️ 생성된 피드 미리보기")
    grid_cols = st.columns(5)
    for idx, img in enumerate(st.session_state.rendered_images):
        with grid_cols[idx]:
            st.image(img, caption=f"{idx + 1}번 슬라이드", use_container_width=True)

    st.subheader("📝 인스타그램 캡션 복사")
    st.text_area("캡션 및 해시태그", value=st.session_state.full_script.get("caption", ""), height=160)
