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
from newspaper import Article
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageStat
from pydantic import BaseModel
from typing import List

# 모바일 친화형 레이아웃 설정
st.set_page_config(page_title="SNS 인스타 카드뉴스 자동생성기", page_icon="📱", layout="centered")

# =============================================
# 1. API 키 설정 (Secrets / 환경변수)
# =============================================
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key and "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]

client = None
if api_key:
    try:
        client = genai.Client(api_key=api_key)
    except Exception:
        pass

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
            badge_font = ImageFont.truetype(f_path, 20)
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
# 텍스트 노이즈 정제기
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
# 스마트 프레이밍 (얼굴 보존 블러 레터박스)
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
        bg.paste(fg, ((target_w - fg_w) // 2, 120), fg)
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
    if pil_img.width < 320 or pil_img.height < 320:
        return False
    stat = ImageStat.Stat(pil_img.convert("L"))
    if stat.stddev[0] < 35:
        return False
    ratio = pil_img.width / pil_img.height
    return 0.45 <= ratio <= 2.6

def download_image_pil(img_url):
    if not img_url:
        return None
    try:
        res = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        if res.status_code == 200 and len(res.content) > 5000:
            img = Image.open(BytesIO(res.content))
            if is_valid_photo(img):
                return img
    except Exception:
        pass
    return None

# =============================================
# AI 카피라이팅 & 캡션 생성 (Gemini API / Fallback)
# =============================================
class HeadlineCandidates(BaseModel):
    titles: List[str]

class CaptionGroup(BaseModel):
    empathy: str
    vote: str
    explain: str

FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"]

def generate_ai_copies(title, text):
    clean_t = sanitize_korean_text(title)
    if not client:
        return [
            f"'{clean_t[:16]}' 아직 모르시는 분들 꼭 보세요",
            f"절대 '{clean_t[:16]}' 그냥 넘기지 마세요",
            f"단 1회 만에 난리 난 '{clean_t[:14]}' 핵심",
            f"관계자가 밝힌 '{clean_t[:14]}' 결정적 비하인드",
            f"실시간 검색어 1위 오른 '{clean_t[:14]}' 총정리"
        ]

    prompt = f"""
    당신은 인스타그램 트렌드 뉴스 계정의 수석 카피라이터입니다.
    영상 속 시청자의 시선을 사로잡는 강력한 후킹 제목 5가지를 추천해 주세요.
    1. 타겟 지목형 ("내 얘기잖아?" 싶은 카피)
    2. 금지/경고형 ("절대 ~하지 마세요" 식의 호기심 자극 카피)
    3. 숫자와 구체성형 (숫자로 궁금증 극대화)
    4. 스토리텔링형 (비하인드/반전 카피)
    5. 공감 자극형 카피

    기사 제목: {clean_t}
    기사 요약: {text[:1200]}
    """
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
            data = json.loads(res.text)
            return [sanitize_korean_text(t) for t in data.get("titles", [])][:5]
        except Exception:
            continue

    return [
        f"'{clean_t[:16]}' 아직 모르시는 분들 꼭 보세요",
        f"절대 '{clean_t[:16]}' 그냥 넘기지 마세요",
        f"단 1회 만에 난리 난 '{clean_t[:14]}' 핵심",
        f"관계자가 밝힌 '{clean_t[:14]}' 결정적 비하인드",
        f"실시간 검색어 1위 오른 '{clean_t[:14]}' 총정리"
    ]

def generate_captions(title, text):
    clean_t = sanitize_korean_text(title)
    empathy_fallback = f"🔥 {clean_t}\n\n오늘 이 소식 보면서 마음 한구석이 찌릿하셨던 분들 많으시죠? 저 역시 이번 소식을 보며 깊은 인상을 받았습니다.\n\n여러분의 오늘 하루는 어떠셨나요? 공감되셨다면 댓글로 이야기 들려주세요 ❤️\n\n#이슈 #공감 #뉴스 #트렌드"
    vote_fallback = f"🔥 {clean_t}\n\n지금 온라인에서 가장 뜨겁게 찬반이 갈리는 주제입니다.\n\n👉 A. 완벽히 통쾌하고 사이다다\n👉 B. 아직은 조금 더 지켜봐야 한다\n\n여러분의 솔직한 생각은 어느 쪽인가요? 댓글로 A 또는 B를 남겨주세요! 👇\n\n#투표 #토론 #이슈 #인스타"
    explain_fallback = f"📌 {clean_t} 핵심 3줄 요약\n\n1. 화제의 핵심 이슈와 배경 전격 공개\n2. 놓치면 아쉬운 핵심 관전 포인트\n3. 앞으로 이어질 새로운 전개와 파장\n\n도움이 되셨다면 나중에 다시 보실 수 있게 [저장]해 두세요 🔖\n\n#정보 #뉴스 #요약 #트렌드"

    if not client:
        return {"empathy": empathy_fallback, "vote": vote_fallback, "explain": explain_fallback}

    prompt = f"""
    당신은 인스타그램 전문 에디터입니다. 이 기사를 바탕으로 인스타그램 본문 캡션 3가지 유형을 작성하세요.
    - empathy: 독자의 감정을 건드려 공감 댓글을 유도하는 공감형
    - vote: A vs B 양자택일 선택을 유도하여 댓글 반응을 폭발시키는 찬반 투표형
    - explain: 핵심 3줄 요약과 함께 저장을 유도하는 정보 설명형

    기사 제목: {clean_t}
    기사 내용: {text[:1200]}
    """
    for model_name in FALLBACK_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=CaptionGroup,
                    temperature=0.7
                )
            )
            return json.loads(res.text)
        except Exception:
            continue

    return {"empathy": empathy_fallback, "vote": vote_fallback, "explain": explain_fallback}

# =============================================
# 카드 렌더링 함수 (영상 속 1장 직관 뷰)
# =============================================
def render_single_card(title_text, sub_text, base_img, title_size, content_size, text_y_pos):
    width, height = 1080, 1350
    t_font, c_font, b_font = load_fonts(title_size, content_size)

    # 1. 배경 프레이밍
    base_img = smart_fit_or_crop(base_img, width, height)

    # 2. 다크 그라데이션
    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(gradient)

    for y in range(0, 160):
        alpha = int((1.0 - (y / 160.0)) * 75)
        g_draw.line([(0, y), (width, y)], fill=(5, 8, 15, alpha))

    start_g = int(text_y_pos - 120)
    for y in range(start_g, height):
        if y < start_g + 260:
            progress = (y - start_g) / 260.0
            alpha = int((progress ** 1.6) * 230)
        else:
            alpha = 248
        g_draw.line([(0, y), (width, y)], fill=(9, 13, 20, alpha))

    card = Image.alpha_composite(base_img, gradient).convert("RGB")
    draw = ImageDraw.Draw(card)

    # 3. 상단 뱃지
    tag_clean = "TREND ISSUE"
    draw.rounded_rectangle([64, 64, 230, 110], radius=22, fill=(15, 23, 42, 220))
    draw.rounded_rectangle([64, 64, 230, 110], radius=22, outline=(255, 255, 255, 70), width=1)
    draw.ellipse([80, 82, 88, 90], fill=(250, 204, 21))
    draw.text((98, 76), tag_clean, font=b_font, fill=(255, 255, 255))

    # 4. 텍스트 조판
    t_words = title_text.split()
    t_lines, curr = [], ""
    for w in t_words:
        if len(curr + w) > 13:
            if curr.strip(): t_lines.append(curr.strip())
            curr = w + " "
        else:
            curr += w + " "
    if curr.strip(): t_lines.append(curr.strip())

    c_words = sub_text.split()
    c_lines, curr = [], ""
    for w in c_words:
        if len(curr + w) > 22:
            if curr.strip(): c_lines.append(curr.strip())
            curr = w + " "
        else:
            curr += w + " "
    if curr.strip(): c_lines.append(curr.strip())

    curr_y = text_y_pos
    # 포인트 바
    draw.rounded_rectangle([64, curr_y - 20, 114, curr_y - 13], radius=4, fill=(250, 204, 21))

    for l in t_lines:
        draw.text((64, curr_y), l, font=t_font, fill=(255, 255, 255))
        curr_y += title_size + 14

    curr_y += 18
    for l in c_lines:
        draw.text((64, curr_y), l, font=c_font, fill=(226, 232, 240))
        curr_y += content_size + 12

    # 하단 푸터
    draw.line([64, 1240, 1016, 1240], fill=(51, 65, 85, 180), width=2)
    draw.text((64, 1260), ">> 옆으로 넘겨서 전체 내용 확인하기", font=b_font, fill=(250, 204, 21))

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
        "images": [],
        "current_img_idx": 0,
        "title_size": 52,
        "content_size": 26,
        "text_y": 820
    }

# =============================================
# 📱 메인 UI (영상 속 단일 화면 레이아웃)
# =============================================
st.markdown("<h2 style='text-align: center; margin-bottom: 5px;'>🚀 인스타 보너스·뉴스 카드뉴스 생성기</h2>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: #64748B; margin-bottom: 25px;'>기사 링크만 넣으면 AI가 후킹 카피와 이미지를 1분 만에 완성합니다</p>", unsafe_allow_html=True)

# 1. URL 입력 및 원클릭 만들기 버튼
news_url = st.text_input("🔗 뉴스 기사 링크 입력", placeholder="네이버/다음 등 포털 뉴스 기사 링크를 붙여넣으세요")

if st.button("✨ 인스타 게시물 만들기", type="primary", use_container_width=True):
    if not news_url.strip():
        st.warning("뉴스 링크를 입력해 주세요.")
    else:
        with st.spinner("기사를 읽고 눈길을 끄는 제목과 이미지를 생성하고 있습니다..."):
            try:
                art = Article(news_url, language='ko')
                art.download()
                art.parse()

                # 이미지 수집
                img_pool = []
                if art.top_image:
                    top_img = download_image_pil(art.top_image)
                    if top_img: img_pool.append(top_img)
                for u in art.images:
                    if u != art.top_image:
                        p = download_image_pil(u)
                        if p: img_pool.append(p)

                if not img_pool:
                    img_pool = [Image.new("RGB", (1080, 1350), color=(15, 23, 42))]

                # AI 카피 및 캡션 생성
                copies = generate_ai_copies(art.title, art.text)
                captions = generate_captions(art.title, art.text)

                # 첫 문단 서브카피
                first_lines = [sanitize_korean_text(s) for s in re.split(r'(?<=[.?!])\s+', art.text) if len(s) > 20]
                sub_copy = first_lines[0] if first_lines else "지금 가장 뜨거운 화제의 사건! 상세한 내막과 핵심 관전 포인트를 피드에서 확인하세요."

                st.session_state.app_state["is_ready"] = True
                st.session_state.app_state["copies"] = copies
                st.session_state.app_state["active_title"] = copies[0]
                st.session_state.app_state["active_sub"] = sub_copy
                st.session_state.app_state["captions"] = captions
                st.session_state.app_state["images"] = img_pool
                st.session_state.app_state["current_img_idx"] = 0

            except Exception as e:
                st.error(f"생성 실패: {e}")

# =============================================
# 2. 결과 생성 완료 시: 영상 속 실시간 인터랙션 화면
# =============================================
state = st.session_state.app_state

if state["is_ready"]:
    st.write("---")

    # 1) AI 추천 카피 선택 (클릭 시 즉시 실시간 반영)
    st.markdown("#### 💡 AI 추천 후킹 카피 (클릭 시 즉시 변경)")
    cols_btn = st.columns(len(state["copies"]))
    for idx, c_text in enumerate(state["copies"]):
        with cols_btn[idx]:
            if st.button(f"카피 {idx + 1}", key=f"copy_btn_{idx}", use_container_width=True):
                state["active_title"] = c_text
                st.rerun()

    # 2) 카드 실시간 미리보기
    current_img = state["images"][state["current_img_idx"]]
    rendered_img = render_single_card(
        state["active_title"],
        state["active_sub"],
        current_img,
        state["title_size"],
        state["content_size"],
        state["text_y"]
    )

    st.image(rendered_img, caption="📱 완성된 인스타그램 피드 (1080x1350)", use_container_width=True)

    # 3) 인터랙티브 커스터마이징 도구 (문구 직접 수정 / 글자 크기 / 위치 조절 / 이미지 교체)
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
            state["text_y"] = st.slider("텍스트 높이 위치", 680, 920, state["text_y"], step=10)

        # 이미지 변경 (다시 그리기 / 다른 스틸컷 전환)
        if len(state["images"]) > 1:
            if st.button("🔄 다른 기사 사진으로 교체 (다시 그리기)", use_container_width=True):
                state["current_img_idx"] = (state["current_img_idx"] + 1) % len(state["images"])
                st.rerun()

    # 4) 이미지 다운로드 버튼
    buf = BytesIO()
    rendered_img.save(buf, format="PNG")
    st.download_button(
        label="📥 완성된 카드 이미지 저장하기",
        data=buf.getvalue(),
        file_name=f"instagram_feed_{datetime.now().strftime('%H%M%S')}.png",
        mime="image/png",
        use_container_width=True
    )

    st.write("---")

    # 5) 인스타 본문 캡션 (영상 속 공감형 / 투표형 / 설명형 탭)
    st.markdown("#### 📝 인스타그램 본문 캡션 선택 (반응도 유도)")
    tab_empathy, tab_vote, tab_explain = st.tabs(["❤️ 공감형", "🗳️ 투표형 (찬반)", "📑 설명형 (요약)"])

    caps = state["captions"]
    with tab_empathy:
        st.text_area("공감형 캡션 (복사해서 인스타에 붙여넣으세요)", value=caps.get("empathy", ""), height=150)
    with tab_vote:
        st.text_area("투표형 캡션 (댓글 토론 유도)", value=caps.get("vote", ""), height=150)
    with tab_explain:
        st.text_area("설명형 캡션 (핵심 요약 & 저장 유도)", value=caps.get("explain", ""), height=150)
