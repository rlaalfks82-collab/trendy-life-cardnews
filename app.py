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

# 모바일 단일 화면 뷰 설정
st.set_page_config(page_title="SNS 인스타 카드뉴스 쾌속 생성기", page_icon="📱", layout="centered")

# =============================================
# 1. API 키 설정 (Secrets 및 상단 직접 입력 지원)
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
# 기사 내용 100% 일치 AI 이미지 생성기
# =============================================
def generate_contextual_ai_image(prompt_text, seed_val=42, fallback_photo=None):
    clean_prompt = re.sub(r'[^a-zA-Z0-9\s,]', '', prompt_text).strip()
    if not clean_prompt:
        clean_prompt = "modern high tech gadget product shot close up dark background"
    
    enhanced_prompt = f"{clean_prompt}, clean dark studio background, professional product photography, 8k, dramatic lighting"
    encoded_prompt = urllib.parse.quote(enhanced_prompt)

    urls = [
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1350&seed={seed_val}&model=turbo&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1350&seed={seed_val}&nologo=true"
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for u in urls:
        try:
            res = requests.get(u, headers=headers, timeout=12)
            if res.status_code == 200 and len(res.content) > 10000:
                img = Image.open(BytesIO(res.content))
                if is_valid_photo(img):
                    return img
        except Exception:
            continue

    if fallback_photo and is_valid_photo(fallback_photo):
        return fallback_photo.copy()

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
    아래 기사의 실제 분야(스마트폰, IT, 정치, 사회, 경제, 연예 등)의 사건 팩트에 정확히 부합하는 콘텐츠 세트를 작성하세요.

    [필수 작성 규칙]:
    1. titles: 독자의 스크롤을 멈추게 하는 강력한 후킹 제목 5개 (1줄당 14~20자 내외, 기사 주제에 맞는 진지하고 정확한 어휘, 대괄호 [] 제외).
    2. card_subcopy: 피드 1장 카드에 들어갈 본문 요약 (70~90자).
       - 기사의 '핵심 사건/기기 사양/쟁점'을 1~2개 완결된 문장으로 서술. 기사 내용과 무관한 미사여구 금지.
    3. image_prompt: 이 기사 내용에 정확히 들어맞는 영어 이미지 프롬프트.
       - 스마트폰/IT 기사면: 'modern flagship smartphone device screen display product photography dark background 8k'
       - 정치/시사 기사면: 'press conference government intelligence room dark cinematic lighting'
       - 절대 기사와 무관한 자연, 바다, 산, 꽃 같은 엉뚱한 풍경을 넣지 마세요.
    4. empathy: 기사의 실제 팩트를 2~3줄로 설명하고 의견을 나누는 공감형 인스타 본문.
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
                "image_prompt": data.get("image_prompt", "flagship smartphone tech gadget product shot"),
                "empathy": data.get("empathy", ""),
                "vote": data.get("vote", ""),
                "explain": data.get("explain", "")
            }
        except Exception as e:
            last_err = e
            continue

    raise Exception(f"AI 생성 실패: {last_err}")

# =============================================
# 단일 카드 렌더링 엔진 (디자인 미감 & 뱃지 리파인)
# =============================================
def render_single_card(title_text, sub_text, base_img, title_size, content_size, text_y_pos):
    width, height = 1080, 1350
    t_font, c_font, b_font = load_fonts(title_size, content_size)

    base_img = smart_fit_or_crop(base_img, width, height)

    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(gradient)

    # 상단 은은한 비네팅
    for y in range(0, 180):
        alpha = int((1.0 - (y / 180.0)) * 85)
        g_draw.line([(0, y), (width, y)], fill=(5, 8, 15, alpha))

    # 하단 텍스트 가독성을 위한 부드러운 다크 그라데이션
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

    # [디자인 미감 개선] 제공 이미지와 동일한 Pill 알약형 TREND ISSUE 뱃지
    badge_x, badge_y = 64, 64
    badge_w, badge_h = 216, 52
    badge_radius = 26  # 완전한 라운드 알약 형태

    # 뱃지 배경 및 섬세한 반투명 테두리
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_radius,
        fill=(11, 19, 32)
    )
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_radius,
        outline=(255, 255, 255, 60),
        width=1
    )

    # 뱃지 내부 옐로우 포인트 점 (제공 이미지 스타일)
    dot_cx, dot_cy, dot_r = badge_x + 24, badge_y + 26, 4
    draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=(250, 204, 21))

    # 뱃지 영문 텍스트
    draw.text((badge_x + 38, badge_y + 13), "TREND ISSUE", font=b_font, fill=(248, 250, 252))

    # 제목 텍스트 (어절 줄바꿈)
    t_words = title_text.split()
    t_lines, curr = [], ""
    for w in t_words:
        if len(curr + w) > 13:
            if curr.strip(): t_lines.append(curr.strip())
            curr = w + " "
        else:
            curr += w + " "
    if curr.strip(): t_lines.append(curr.strip())

    # 본문 텍스트 (가독성 기준 20자 내외 줄바꿈)
    c_words = sub_text.split()
    c_lines, curr = [], ""
    for w in c_words:
        if len(curr + w) > 20:
            if curr.strip(): c_lines.append(curr.strip())
            curr = w + " "
        else:
            curr += w + " "
    if curr.strip(): c_lines.append(curr.strip())

    curr_y = text_y_pos
    # 제목 상단 옐로우 악센트 미니바
    draw.rounded_rectangle([64, curr_y - 20, 114, curr_y - 13], radius=4, fill=(250, 204, 21))

    # 제목 출력
    for l in t_lines:
        draw.text((64, curr_y), l, font=t_font, fill=(255, 255, 255))
        curr_y += title_size + 14

    # 본문 출력 (30px 기준 적정 행간 적용)
    curr_y += 18
    for l in c_lines:
        draw.text((64, curr_y), l, font=c_font, fill=(226, 232, 240))
        curr_y += content_size + 14

    # 하단 엣지 라인
    draw.line([64, 1260, 1016, 1260], fill=(51, 65, 85, 140), width=2)

    return card

# =============================================
# 세션 상태 관리 (제공해주신 슬라이더 기본값 54, 30, 860 반영)
# =============================================
if "app_state" not in st.session_state:
    st.session_state.app_state = {
        "is_ready": False,
        "copies": [],
        "active_title": "",
        "active_sub": "",
        "captions": {},
        "article_images": [],
        "ai_generated_images": [],
        "current_image_source": "ai",
        "current_img_idx": 0,
        "title_size": 54,      # 제공 이미지 기본값 세팅: 54
        "content_size": 30,    # 제공 이미지 기본값 세팅: 30
        "text_y": 860,         # 제공 이미지 기본값 세팅: 860
        "image_prompt": "modern smartphone gadget tech product shot",
        "seed": 42
    }

# =============================================
# 📱 메인 화면 UI
# =============================================
st.markdown("<h2 style='text-align: center; margin-bottom: 5px;'>🚀 인스타 단일 피드 카드뉴스 생성기</h2>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: #64748B; margin-bottom: 25px;'>기사 링크만 넣으면 기사 내용에 일치하는 비주얼과 팩트 요약으로 완성합니다</p>", unsafe_allow_html=True)

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

                    # 1회 통합 호출로 콘텐츠 생성
                    ai_result = generate_all_card_content(art.title, art.text)
                    
                    fallback_base = art_img_pool[0] if art_img_pool else None
                    initial_seed = int(time.time()) % 1000
                    ai_img = generate_contextual_ai_image(ai_result["image_prompt"], seed_val=initial_seed, fallback_photo=fallback_base)

                    st.session_state.app_state["is_ready"] = True
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
                    st.session_state.app_state["ai_generated_images"] = [ai_img]
                    st.session_state.app_state["current_image_source"] = "ai"
                    st.session_state.app_state["current_img_idx"] = 0
                    st.session_state.app_state["seed"] = initial_seed

            except Exception as e:
                st.error(f"생성 실패: {e}")

# =============================================
# 2. 결과 생성 완료 시: 실시간 인터랙션 화면
# =============================================
state = st.session_state.app_state

if state["is_ready"]:
    st.write("---")

    # 1) AI 추천 카피 선택
    st.markdown("#### 💡 AI 추천 후킹 카피 (클릭 시 즉시 변경)")
    cols_btn = st.columns(len(state["copies"]))
    for idx, c_text in enumerate(state["copies"]):
        with cols_btn[idx]:
            if st.button(f"카피 {idx + 1}", key=f"copy_btn_{idx}", use_container_width=True):
                state["active_title"] = c_text
                st.rerun()

    # 이미지 소스 분기
    if state["current_image_source"] == "ai" and state["ai_generated_images"]:
        active_bg_img = state["ai_generated_images"][0]
        badge_desc = "🤖 기사 맞춤 AI 비주얼"
    elif state["article_images"]:
        active_bg_img = state["article_images"][state["current_img_idx"]]
        badge_desc = f"📰 기사 원문 사진 ({state['current_img_idx'] + 1}/{len(state['article_images'])})"
    else:
        active_bg_img = state["ai_generated_images"][0]
        badge_desc = "🖼️ 맞춤 비주얼"

    rendered_img = render_single_card(
        state["active_title"],
        state["active_sub"],
        active_bg_img,
        state["title_size"],
        state["content_size"],
        state["text_y"]
    )

    st.image(rendered_img, caption=f"📱 완성된 인스타그램 피드 (1080x1350) · {badge_desc}", use_container_width=True)

    # 이미지 컨트롤 (다시 그리기 / 원문 전환)
    col_img1, col_img2 = st.columns(2)
    with col_img1:
        if st.button("🎨 AI로 다른 이미지 다시 그리기", use_container_width=True):
            new_seed = random.randint(1001, 99999)
            fallback_base = state["article_images"][0] if state["article_images"] else None
            new_ai_img = generate_contextual_ai_image(state["image_prompt"], seed_val=new_seed, fallback_photo=fallback_base)
            state["ai_generated_images"] = [new_ai_img]
            state["current_image_source"] = "ai"
            state["seed"] = new_seed
            st.rerun()

    with col_img2:
        if state["article_images"]:
            if st.button("📰 기사 원문 실물 사진으로 전환", use_container_width=True):
                state["current_image_source"] = "article"
                state["current_img_idx"] = (state["current_img_idx"] + 1) % len(state["article_images"])
                st.rerun()
        else:
            st.button("📰 기사 원문 사진 없음", disabled=True, use_container_width=True)

    # 커스터마이징 패널 (기본값: 제목 54, 본문 30, 높이 860)
    with st.expander("🛠️ 문구 직접 수정 & 글자 크기/위치 조절 (커스터마이징)"):
        col_ed1, col_ed2 = st.columns(2)
        with col_ed1:
            new_title = st.text_input("제목 문구 수정", value=state["active_title"])
            if new_title != state["active_title"]:
                state["active_title"] = new_title
                st.rerun()
        with col_ed2:
            new_sub = st.text_area("본문 문구 수정", value=state["active_sub"], height=70)
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
