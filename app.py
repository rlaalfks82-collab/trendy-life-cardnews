import os
import re
import json
import time
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
# 1. API 키 설정 (Secrets 및 환경변수 지원)
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
# 피사체 얼굴 절단 방지 프레이밍 엔진 (1080x1350)
# =============================================
def smart_fit_or_crop(base_img, target_w=1080, target_h=1350):
    base_img = base_img.convert("RGBA")
    src_w, src_h = base_img.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h

    # 가로가 긴 사진(투샷, 단체컷): 블러 레터박스로 인물 절단 방지
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

    # 세로형 또는 정방형 사진
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
# AI 후킹 카피 & 팩트 기반 3종 캡션 엔진
# =============================================
class HeadlineCandidates(BaseModel):
    titles: List[str]

class ContentSummaryResponse(BaseModel):
    card_subcopy: str
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
            f"실시간 화제 모은 '{clean_t[:14]}' 총정리"
        ]

    prompt = f"""
    당신은 인스타그램 트렌드 뉴스 계정의 수석 카피라이터입니다.
    기사 본문 내용을 정확하게 파악하고, 독자의 시선을 사로잡는 강력한 후킹 제목 5가지를 추천해 주세요.
    1. 🎯 타겟 지목형 ("내 얘기잖아?" 싶은 카피)
    2. 🚫 금지/경고형 ("절대 ~하지 마세요" 식의 호기심 자극 카피)
    3. 🔢 숫자/구체성형 (숫자로 궁금증 극대화)
    4. 📖 스토리텔링형 (비하인드/반전 카피)
    5. ❤️ 공감 자극형 카피

    [작성 규칙]:
    - 기사 내용에 없는 허위 사실을 지어내지 마세요.
    - 기자 이름, 날짜, 언론사명, 대괄호 []는 제목 본문 안에 절대 포함하지 마세요.
    - 1줄당 14자~20자 내외로 화면에 깔끔하게 들어오도록 작성하세요.

    기사 제목: {clean_t}
    기사 본문 내용: {text[:1500]}
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
            titles = [sanitize_korean_text(t) for t in data.get("titles", [])]
            if len(titles) >= 3:
                return titles[:5]
        except Exception:
            continue

    return [
        f"'{clean_t[:16]}' 아직 모르시는 분들 꼭 보세요",
        f"절대 '{clean_t[:16]}' 그냥 넘기지 마세요",
        f"단 1회 만에 난리 난 '{clean_t[:14]}' 핵심",
        f"관계자가 밝힌 '{clean_t[:14]}' 결정적 비하인드",
        f"실시간 화제 모은 '{clean_t[:14]}' 총정리"
    ]

def generate_news_content(title, text):
    clean_t = sanitize_korean_text(title)
    
    # AI 장애 시 팩트 기반 로컬 백업
    raw_sentences = [sanitize_korean_text(s) for s in re.split(r'(?<=[.?!])\s+', text)]
    valid_sentences = [
        s for s in raw_sentences 
        if len(s) >= 25 and not any(kw in s for kw in ["기자", "스튜디오", "연출", "극본", "배급", "사진="])
    ]
    fact_1 = valid_sentences[0] if len(valid_sentences) > 0 else f"{clean_t} 관련 소식이 전해졌습니다."
    fact_2 = valid_sentences[1] if len(valid_sentences) > 1 else "핵심 내용과 쟁점에 관심이 집중되고 있습니다."
    fact_3 = valid_sentences[2] if len(valid_sentences) > 2 else "이후 전개와 여론 반응에 이목이 쏠립니다."

    fallback_result = {
        "card_subcopy": f"{fact_1} {fact_2}"[:95],
        "empathy": (
            f"🔥 {clean_t}\n\n"
            f"📌 핵심 사건 요약\n"
            f"- {fact_1}\n"
            f"- {fact_2}\n\n"
            f"이 소식 접하고 마음 한구석이 찌릿하셨던 분들 많으시죠? 저 역시 깊은 인상을 받았습니다.\n"
            f"여러분의 생각은 어떠신가요? 댓글로 이야기 들려주세요 ❤️\n\n"
            f"#뉴스 #이슈 #트렌드"
        ),
        "vote": (
            f"🔥 {clean_t}\n\n"
            f"📌 핵심 쟁점 브리핑\n"
            f"• 상황: {fact_1}\n"
            f"• 대립 포인트: {fact_2}\n\n"
            f"지금 온라인에서도 의견이 크게 엇갈리고 있습니다.\n\n"
            f"👉 A. 충분히 납득되고 사이다다\n"
            f"👉 B. 조금 더 신중하게 지켜봐야 한다\n\n"
            f"여러분의 솔직한 생각은? 댓글로 A 또는 B를 남겨주세요! 👇\n\n"
            f"#토론 #투표 #이슈"
        ),
        "explain": (
            f"📌 {clean_t} 핵심 3줄 정리\n\n"
            f"1️⃣ {fact_1}\n"
            f"2️⃣ {fact_2}\n"
            f"3️⃣ {fact_3}\n\n"
            f"놓치지 않도록 나중에 볼 수 있게 [저장]해 두세요 🔖\n\n"
            f"#정보요약 #뉴스정리 #트렌드이슈"
        )
    }

    if not client:
        return fallback_result

    prompt = f"""
    당신은 인스타그램 전문 에디터입니다. 아래 기사 본문의 '구체적인 팩트와 정보'를 정확히 요약하여 다음 4가지 요소를 작성하세요.
    - 기자명, 날짜, 언론사명 같은 불필요한 메타데이터는 제외하고 순수 사건/콘텐츠 팩트만 담으세요.

    1. card_subcopy: 피드 카드 1장에 들어갈 핵심 본문 요약 (마침표로 끝나는 완결된 1~2개 문장, 70~90자 내외).
    2. empathy (공감형): 기사의 핵심 상황/팩트를 먼저 명확히 2~3줄로 설명한 뒤, 독자의 감정을 건드려 공감 댓글을 유도.
    3. vote (투표형): 기사의 핵심 쟁점과 대립 상황을 팩트 기반으로 정리한 뒤, 'A vs B' 양자택일 질문 제시.
    4. explain (설명형): 기사 내용을 바탕으로 1, 2, 3번으로 나눈 핵심 요약 브리핑 및 저장 유도.

    기사 제목: {clean_t}
    기사 본문 내용:
    {text[:1500]}
    """

    for model_name in FALLBACK_MODELS:
        try:
            res = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ContentSummaryResponse,
                    temperature=0.5
                )
            )
            data = json.loads(res.text)
            return {
                "card_subcopy": sanitize_korean_text(data.get("card_subcopy", fallback_result["card_subcopy"])),
                "empathy": data.get("empathy", fallback_result["empathy"]),
                "vote": data.get("vote", fallback_result["vote"]),
                "explain": data.get("explain", fallback_result["explain"])
            }
        except Exception:
            continue

    return fallback_result

# =============================================
# 단일 카드 렌더링 엔진 (인스타그램 공식 규격: 1080x1350)
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

    # 3. 상단 브랜드 뱃지
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
        "text_y": 880
    }

# =============================================
# 📱 메인 화면 UI
# =============================================
st.markdown("<h2 style='text-align: center; margin-bottom: 5px;'>🚀 인스타 보너스·뉴스 카드뉴스 생성기</h2>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: #64748B; margin-bottom: 25px;'>기사 링크만 넣으면 팩트 요약과 최적화 스틸컷으로 1분 만에 완성합니다</p>", unsafe_allow_html=True)

# 1. URL 입력 및 원클릭 만들기 버튼
news_url = st.text_input("🔗 뉴스 기사 링크 입력", placeholder="네이버/다음 등 포털 뉴스 기사 링크를 붙여넣으세요")

if st.button("✨ 인스타 게시물 만들기", type="primary", use_container_width=True):
    if not news_url.strip():
        st.warning("뉴스 링크를 입력해 주세요.")
    else:
        with st.spinner("기사 본문 팩트와 스틸컷을 정확하게 분석하고 있습니다..."):
            try:
                art = Article(news_url, language='ko')
                art.download()
                art.parse()

                # 실제 기사 스틸컷 수집
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

                # 4대 후킹 카피 5종 생성
                copies = generate_ai_copies(art.title, art.text)

                # 팩트 기반 카드 본문 요약 & 3종 캡션 일괄 생성
                content_res = generate_news_content(art.title, art.text)

                st.session_state.app_state["is_ready"] = True
                st.session_state.app_state["copies"] = copies
                st.session_state.app_state["active_title"] = copies[0]
                st.session_state.app_state["active_sub"] = content_res["card_subcopy"]
                st.session_state.app_state["captions"] = {
                    "empathy": content_res["empathy"],
                    "vote": content_res["vote"],
                    "explain": content_res["explain"]
                }
                st.session_state.app_state["images"] = img_pool
                st.session_state.app_state["current_img_idx"] = 0

            except Exception as e:
                st.error(f"기사 분석 실패: {e}")

# =============================================
# 2. 결과 생성 완료 시: 실시간 인터랙션 화면
# =============================================
state = st.session_state.app_state

if state["is_ready"]:
    st.write("---")

    # 1) AI 추천 카피 선택 (터치 시 0초 즉시 반영)
    st.markdown("#### 💡 AI 추천 후킹 카피 (클릭 시 즉시 변경)")
    cols_btn = st.columns(len(state["copies"]))
    for idx, c_text in enumerate(state["copies"]):
        with cols_btn[idx]:
            if st.button(f"카피 {idx + 1}", key=f"copy_btn_{idx}", use_container_width=True):
                state["active_title"] = c_text
                st.rerun()

    # 2) 카드 실시간 미리보기 (1080x1350)
    current_img = state["images"][state["current_img_idx"]]
    rendered_img = render_single_card(
        state["active_title"],
        state["active_sub"],
        current_img,
        state["title_size"],
        state["content_size"],
        state["text_y"]
    )

    st.image(rendered_img, caption="📱 완성된 인스타그램 피드 (1080x1350 / 4:5 규격)", use_container_width=True)

    # 3) 실시간 조절 패널 (문구 수정 / 슬라이더 조절 / 스틸컷 변경)
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

        if len(state["images"]) > 1:
            if st.button("🔄 다른 기사 사진으로 교체 (다시 그리기)", use_container_width=True):
                state["current_img_idx"] = (state["current_img_idx"] + 1) % len(state["images"])
                st.rerun()

    # 4) 이미지 다운로드 버튼
    buf = BytesIO()
    rendered_img.save(buf, format="PNG")
    st.download_button(
        label="📥 완성된 카드 이미지 저장하기 (1080x1350)",
        data = buf.getvalue(),
        file_name=f"instagram_feed_{datetime.now().strftime('%H%M%S')}.png",
        mime="image/png",
        use_container_width=True
    )

    st.write("---")

    # 5) 팩트 기반 인스타 본문 캡션 3종 탭
    st.markdown("#### 📝 인스타그램 본문 캡션 선택 (기사 팩트 반영)")
    tab_empathy, tab_vote, tab_explain = st.tabs(["❤️ 공감형", "🗳️ 투표형 (찬반)", "📑 정보 설명형 (요약)"])

    caps = state["captions"]
    with tab_empathy:
        st.text_area("공감형 캡션 (복사해서 인스타에 붙여넣으세요)", value=caps.get("empathy", ""), height=170)
    with tab_vote:
        st.text_area("투표형 캡션 (댓글 토론 유도)", value=caps.get("vote", ""), height=170)
    with tab_explain:
        st.text_area("설명형 캡션 (핵심 요약 & 저장 유도)", value=caps.get("explain", ""), height=170)
