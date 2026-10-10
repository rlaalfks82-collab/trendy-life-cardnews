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
# 어절 단위 지능형 자연스러운 줄바꿈 엔진 (Word-wrap)
# =============================================
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
# [개선] 100% 무한 갱신 보장 AI 이미지 생성 파이프라인
# =============================================
def generate_contextual_ai_image(prompt_text, seed_val=42):
    """
    무작위 자연/풍경 사이트를 완전히 배제하고, 무한히 계속해서 새로운 기사 맞춤형 AI 이미지를 그립니다.
    """
    clean_prompt = re.sub(r'[^a-zA-Z0-9\s,]', '', prompt_text).strip()
    if not clean_prompt:
        clean_prompt = "flagship tech product documentary scene"
    
    # 4회차 이상 넘어가도 계속 변형을 줄 수 있도록 dynamic 파라미터 매치
    visual_styles = [
        "cinematic lighting, ultra-realistic, 8k, professional photography, dramatic shadows, highly detailed",
        "studio product shot, ultra sharp details, dark background, photorealistic 8k, Award Winning photo",
        "handheld action photography, natural movement blur, raw image quality, 8k documentary capture",
        "industrial tech aesthetics, dark cinematic look, high contrast, immersive details, award winning"
    ]
    selected_style = random.choice(visual_styles)
    enhanced_prompt = f"{clean_prompt}, {selected_style}"
    encoded_prompt = urllib.parse.quote(enhanced_prompt)

    # 400ms 단위 실시간 타임스탬프와 난수를 곱해 API 캐싱 파쇄
    ts = int(time.time() * 1000) + random.randint(100, 999)

    # Pollinations 다변화 모델 엔드포인트 세트 (캐시 버스터 t 파라미터 포함)
    candidate_urls = [
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1350&seed={seed_val}&model=turbo&nologo=true&t={ts}",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1350&seed={seed_val + 52}&nologo=true&t={ts + 1}",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1350&seed={seed_val + 248}&model=flux&nologo=true&t={ts + 2}",
        f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1350&seed={seed_val + 891}&enhance=true&nologo=true&t={ts + 3}"
    ]

    headers = {
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.{random.randint(1, 200)} Safari/537.36",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }

    # 후보 AI 생성 URL을 돌며 응답 확보 시도
    for u in candidate_urls:
        try:
            res = requests.get(u, headers=headers, timeout=12)
            if res.status_code == 200 and len(res.content) > 10000:
                img = Image.open(BytesIO(res.content))
                if is_valid_photo(img):
                    return img
        except Exception:
            continue

    # 폴백 안전망 (어떠한 빽업 풍경/자연 사진도 거부, 럭셔리 다크 플레이트 생성)
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
    아래 기사의 실제 분야(스마트폰, IT, 정치, 사회, 경제, 사건사고 등)의 사건 팩트에 정확히 부합하는 콘텐츠 세트를 작성하세요.

    [필수 작성 규칙]:
    1. titles: 독자의 스크롤을 멈추게 하는 강력한 후킹 제목 5개 (1줄당 14~20자 내외, 기사 주제에 맞는 진지하고 정확한 어휘, 대괄호 [] 제외).
    2. card_subcopy: 피드 1장 카드에 들어갈 본문 요약 (70~90자).
       - 기사의 '핵심 사건/기기 사양/쟁점'을 1~2개 완결된 문장으로 서술. 기사 내용과 무관한 미사여구 금지.
    3. image_prompt: 이 기사 내용에 정확히 들어맞는 영어 이미지 프롬프트.
       - 스마트폰/IT 기사면: 'modern flagship smartphone device screen display product photography dark background 8k'
       - 교통사고/사건 기사면: 'car accident investigation road traffic police scene dramatic documentary lighting'
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

    dot_cx, dot_cy, dot_r = badge_x + 24, badge_y + 27, 4
    draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=(251, 191, 36))
    draw.text((badge_x + 40, badge_y + 14), "TREND ISSUE", font=b_font, fill=(241, 245, 249, 235))

    t_lines = wrap_natural_korean(title_text, max_chars_per_line=13)
    c_lines = wrap_natural_korean(sub_text, max_chars_per_line=19)

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
        "copies": [],
        "active_title": "",
        "active_sub": "",
        "captions": {},
        "article_images": [],
        "ai_generated_images": [],
        "current_image_source": "ai",
        "current_img_idx": 0,
        "title_size": 54,
        "content_size": 30,
        "text_y": 860,
        "image_prompt": "modern smartphone gadget tech product shot",
        "seed": 42,
        "redraw_count": 0
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

                    if not art_img_pool:
                        art_img_pool = [Image.new("RGB", (1080, 1350), color=(15, 23, 42))]

                    ai_result = generate_all_card_content(art.title, art.text)
                    
                    initial_seed = random.randint(1001, 99999)
                    ai_img = generate_contextual_ai_image(ai_result["image_prompt"], seed_val=initial_seed)

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
                    st.session_state.app_state["ai_generated_images"] = [ai_img] # 주소값 독립화
                    st.session_state.app_state["current_image_source"] = "ai"
                    st.session_state.app_state["current_img_idx"] = 0
                    st.session_state.app_state["seed"] = initial_seed
                    st.session_state.app_state["redraw_count"] = 0

            except Exception as e:
                st.error(f"생성 실패: {e}")

# =============================================
# 2. 결과 생성 완료 시: 실시간 인터랙션 화면
# =============================================
state = st.session_state.app_state

if state["is_ready"]:
    st.write("---")

    st.markdown("#### 💡 AI 추천 후킹 카피 (클릭 시 즉시 변경)")
    cols_btn = st.columns(len(state["copies"]))
    for idx, c_text in enumerate(state["copies"]):
        with cols_btn[idx]:
            if st.button(f"카피 {idx + 1}", key=f"copy_btn_{idx}", use_container_width=True):
                state["active_title"] = c_text
                st.rerun()

    # 이미지 소스 분기 및 뷰 캡션 유동 키 할당 (UI 캐싱 완전 차단용)
    active_bg_img = None
    if state["current_image_source"] == "ai" and len(state["ai_generated_images"]) > 0:
        active_bg_img = state["ai_generated_images"][0]
        badge_desc = f"🤖 기사 맞춤 AI 비주얼 ({state['redraw_count'] + 1}회차)"
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
        state["text_y"]
    )

    # 4번째 이상 클릭하더라도 렌더 뷰 컴포넌트를 강제 Refresh하기 위해 high_id 난수 key 부여
    dynamic_img_key = f"img_view_{state['seed']}_{state['redraw_count']}"
    st.image(
        rendered_img, 
        key=dynamic_img_key,
        caption=f"📱 완성된 인스타그램 피드 (1080x1350) · {badge_desc}", 
        use_container_width=True
    )

    col_img1, col_img2 = st.columns(2)
    with col_img1:
        # 버튼에 count 결합 및 클릭 시 action 함수에서 세션 메모리 오버라이드
        if st.button("🎨 AI로 다른 이미지 다시 그리기", key=f"btn_redraw_main_{state['redraw_count']}", use_container_width=True):
            state["redraw_count"] += 1
            # 매 횟수마다 겹침 없는 극한의 난수 + 카운터 곱
            new_seed = int(time.time() * 100) + random.randint(1000, 9999) + state["redraw_count"] * 142
            
            with st.spinner(f"기사 내용에 맞는 새 비주얼을 그리고 있습니다... (새 이미지 생성 중)"):
                # 생성 파이프라인에서 무조건 생성된 Image 객체를 직접 받아와 리스트로 신규 주입
                new_ai_img = generate_contextual_ai_image(state["image_prompt"], seed_val=new_seed)
                
                # [NEW] 세션 상태를 이전 값을 참조하지 않도록 완전히 새로 독립 교체
                state["ai_generated_images"] = [new_ai_img.copy()] # 복사본 생성으로 메모리 주소 격리
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
