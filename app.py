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
st.sidebar.header("🎨 트렌디라이프 디자인 옵션")
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
brand_tag = st.sidebar.text_input("상단 브랜딩 태그", value="What's today?")

# 메인 헤더
st.markdown("""
<div style="text-align: center; line-height: 1.35; margin-bottom: 25px;">
    <h2 style="color: #0F172A; margin-bottom: 8px; font-weight: 800;">🔥 트렌디라이프 멀티기사 카드뉴스 생성기</h2>
    <p style="color: #475569; font-size: 19px; font-weight: 600; margin: 0;">중복 사진 필터링 및 AI 에러 방어 로직이 적용된 테스트 안정 버전</p>
</div>
""", unsafe_allow_html=True)
st.write("---")

col_url1, col_url2 = st.columns(2)
with col_url1:
    news_url_1 = st.text_input("🔗 첫 번째 뉴스 기사 링크 (필수)", placeholder="메인 기사 URL을 입력하세요.")
with col_url2:
    news_url_2 = st.text_input("🔗 두 번째 뉴스 기사 링크 (선택)", placeholder="관련 추가 기사 URL을 입력하세요.")

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

# ---------------------------------------------
# 시각적 중복 이미지 판별 (dHash)
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
        if res.status_code == 200 and len(res.content) > 5000:
            img = Image.open(BytesIO(res.content))
            if img.width >= 350 and img.height >= 350:
                ratio = img.width / img.height
                if 0.4 <= ratio <= 2.5:
                    return img
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

# ---------------------------------------------
# 무중단 AI 호출 엔진 (429/404 발생 시 모델 자동 순환)
# ---------------------------------------------
FALLBACK_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.1-pro-preview"
]

def call_gemini_headlines(prompt, default_title):
    if not client:
        return {"titles": [default_title, f"'{default_title[:20]}' 핵심 쟁점", f"{default_title[:20]} 화제의 현장"]}

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

    # 모든 AI 모델이 제한되었을 때 안전 모드 작동 (에러로 멈추지 않음)
    st.info("💡 AI 할당량 소진으로 로컬 스마트 분석 모드로 안전하게 헤드라인을 생성했습니다.")
    return {
        "titles": [
            f"\"{default_title}\"",
            f"요즘 화제라는 '{default_title[:18]}' 도대체 무슨 일?",
            f"실시간 논란 확산 중인 '{default_title[:18]}' 핵심 정리"
        ]
    }

def call_gemini_script(prompt, article_title, article_text):
    if not client:
        return build_local_fallback_script(article_title, article_text)

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

    st.warning("⚠️ AI 일일 사용량이 소진되어 기사 본문 기반 자동 요약 스크립트로 완성합니다.")
    return build_local_fallback_script(article_title, article_text)

def build_local_fallback_script(title, text):
    lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 30]
    p1 = lines[0][:110] if len(lines) > 0 else "기사의 핵심 사건이 주목받고 있습니다."
    p2 = lines[1][:110] if len(lines) > 1 else "사건의 배경과 구체적인 경위가 공개되었습니다."
    p3 = lines[2][:110] if len(lines) > 2 else "이목을 끄는 사실과 쟁점이 대두되었습니다."
    p4 = lines[3][:110] if len(lines) > 3 else "온라인과 커뮤니티에서 다양한 의견이 쏟아지고 있습니다."

    return {
        "slides": [
            {"page": 1, "headline": title[:30], "subhead": p1, "img_keyword": "news issue"},
            {"page": 2, "headline": "도대체 무슨 일일까?", "subhead": p2, "img_keyword": "drama scene"},
            {"page": 3, "headline": "우리가 주목해야 할 사실", "subhead": p3, "img_keyword": "investigation"},
            {"page": 4, "headline": "현재 여론 반응", "subhead": p4, "img_keyword": "people discussion"},
            {"page": 5, "headline": "여러분의 생각은?", "subhead": "이번 이슈에 대한 여러분의 솔직한 생각을 댓글로 들려주세요!", "img_keyword": "opinion"}
        ],
        "caption": f"🔥 {title}\n\n상세한 내용은 피드에서 확인해 보세요!\n\n#이슈 #트렌드 #뉴스 #트렌디라이프"
    }

# ---------------------------------------------
# 렌더링 엔진
# ---------------------------------------------
def render_trendportal_card(page, total_pages, title, content, base_img, fonts, tag_text="TREND ISSUE"):
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
    draw.text((930, 72), f"{page} / {total_pages}", font=page_font, fill=(203, 213, 225))

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
    elif page == total_pages:
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
        with st.spinner("기사 본문과 스틸컷 이미지들을 안전하게 수집하고 있습니다..."):
            try:
                raw_image_urls = []

                art1 = Article(news_url_1, language='ko')
                art1.download()
                art1.parse()

                combined_title = art1.title
                combined_text = f"[기사 1]\n{art1.text}"
                if art1.top_image:
                    raw_image_urls.append(art1.top_image)
                for img in art1.images:
                    raw_image_urls.append(img)

                if news_url_2.strip():
                    try:
                        art2 = Article(news_url_2, language='ko')
                        art2.download()
                        art2.parse()
                        combined_text += f"\n\n[기사 2]\n{art2.text}"
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
                    "image_urls": unique_urls
                }

                cand_prompt = f"""
                당신은 인스타그램 트렌드 뉴스 채널(@trendy.life_newwws)의 수석 카피라이터입니다.
                기사 내용을 토대로 독자의 시선을 사로잡는 강력한 후킹 제목 3가지를 만드세요.
                - 특수문자나 이모지는 절대 사용하지 말고, 따옴표/물음표만 사용하세요.

                기사 원문 제목: {combined_title}
                기사 본문 요약: {combined_text[:1400]}
                """
                res = call_gemini_headlines(cand_prompt, combined_title)
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
        with st.spinner("시각적 중복 검증 및 슬라이드 합성을 진행 중입니다..."):
            script_prompt = f"""
            당신은 인스타그램 트렌드 매거진(@trendy.life_newwws) 전문 에디터입니다.
            표지 제목은 반드시 "{selected_headline}"을 사용하세요.
            반드시 5장의 슬라이드(page 1부터 5까지)를 구성하세요.
            각 슬라이드의 내용과 가장 잘 어울리는 검색 키워드를 'img_keyword'에 영어 1~2단어로 작성하세요.
            슬라이드 본문(subhead)은 2~3줄 내외(120자 이내)로 작성하고 이모지는 포함하지 마세요.

            기사 내용: {art['text']}
            """
            try:
                st.session_state.full_script = call_gemini_script(script_prompt, art['title'], art['text'])
                data = st.session_state.full_script
                fonts = load_fonts(title_size, content_size)

                folder_name = datetime.now().strftime("card_news_%Y%m%d_%H%M%S")
                os.makedirs(folder_name, exist_ok=True)

                # ========================================================
                # [중복 방지 핵심 검증] dHash 기반 지각 해시 필터링
                # ========================================================
                unique_images_pool = []
                unique_hashes = []

                for img_url in art.get("image_urls", []):
                    img_obj = download_image_pil(img_url)
                    if img_obj:
                        h = calculate_dhash(img_obj)
                        if not is_duplicate_visual(h, unique_hashes, threshold=10):
                            unique_hashes.append(h)
                            unique_images_pool.append(img_obj)

                saved_images = []
                used_pool = unique_images_pool.copy()
                fallback_cover = unique_images_pool[0] if unique_images_pool else None
                total_slides_count = len(data["slides"])

                for idx, slide in enumerate(data["slides"]):
                    page = slide["page"]
                    title = slide["headline"]
                    content = slide["subhead"]
                    keyword = slide.get("img_keyword", "trend")

                    base_img = None

                    if "드라마/영화" in image_mode:
                        if len(used_pool) > 0:
                            base_img = used_pool.pop(0)
                        else:
                            base_img = fetch_keyword_stock_image(keyword, fallback_img=fallback_cover)
                    else:
                        if page == 1 and len(used_pool) > 0:
                            base_img = used_pool.pop(0)
                        else:
                            base_img = fetch_keyword_stock_image(keyword, fallback_img=fallback_cover)

                    card = render_trendportal_card(page, total_slides_count, title, content, base_img, fonts, tag_text=brand_tag)
                    
                    save_path = os.path.join(folder_name, f"slide_{page}.png")
                    card.save(save_path)
                    saved_images.append(card)

                # ZIP 압축
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
    st.success("🎉 피드 5장 생성이 성공적으로 완료되었습니다!")

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
