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
        res = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=7)
        if res.status_code == 200 and len(res.content) > 5000:
            img = Image.open(BytesIO(res.content))
            if is_valid_photo(img):
                return img
    except Exception:
        pass
    return None

def generate_ai_custom_image(prompt_text, seed_val=42):
    clean_prompt = re.sub(r'[^a-zA-Z0-9\s,]', '', prompt_text)
    encoded_prompt = urllib.parse.quote(f"{clean_prompt}, dramatic cinematic lighting, photorealistic, 8k, editorial photography")
    
    gen_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1080&height=1350&seed={seed_val}&model=flux&nologo=true"
    
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(gen_url, headers=headers, timeout=12)
        if res.status_code == 200 and len(res.content) > 10000:
            return Image.open(BytesIO(res.content))
    except Exception:
        pass

    try:
        fallback_kw = urllib.parse.quote(clean_prompt.split(",")[0].strip())
        res = requests.get(f"https://source.unsplash.com/1080x1350/?{fallback_kw}", headers=headers, timeout=6)
        if res.status_code == 200 and len(res.content) > 5000:
            return Image.open(BytesIO(res.content))
    except Exception:
        pass

    return Image.new("RGB", (1080, 1350), color=(15, 23, 42))

# =============================================
# Pydantic 모델
# =============================================
class HeadlineCandidates(BaseModel):
    titles: List[str]

class ContentSummaryResponse(BaseModel):
    card_subcopy: str
    image_prompt: str
    empathy: str
    vote: str
    explain: str

FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-pro-preview"]

# =============================================
# 강력한 후킹 카피 & 다이나믹 서브카피 생성 엔진
# =============================================
def generate_ai_copies(title, text):
    clean_t = sanitize_korean_text(title)
    
    random_fallbacks = [
        f"\"이건 진짜 선 넘었죠\" 지금 난리 난 {clean_t[:12]} 실체",
        f"절대 그냥 지나치면 안 되는 {clean_t[:14]} 결정적 이유",
        f"방송 1회 만에 뒤집어진 {clean_t[:13]}... 도대체 왜?",
        f"\"저 사람 누구야?\" 모두가 충격받은 {clean_t[:12]} 반전",
        f"{clean_t[:14]} 아직도 모르면 대화에 못 낍니다",
        f"\"결국 터질 게 터졌다\" 실시간 발칵 뒤집힌 현장",
        f"단 3분 만에 여론 싹 바뀐 {clean_t[:13]} 결정적 장면",
        f"\"이게 실화라고?\" 다들 경악하고 있는 {clean_t[:12]} 속사정"
    ]

    if not client:
        return random.sample(random_fallbacks, 5)

    prompt = f"""
    당신은 인스타그램 100만 팔로워 이슈 매거진의 탑티어 카피라이터입니다.
    기사 내용을 바탕으로, 스크롤을 내리던 사람의 손가락을 0.5초 만에 멈추게 만드는 '초강력 후킹 제목' 5개를 작성하세요.

    [카피라이팅 스타일 지침 - 반드시 적용]:
    1. 도발/경고형: "절대 혼자 보지 마세요", "~인 줄 알았는데 충격 반전"
    2. 공감/결핍형: "~아직도 모르는 사람 없죠?", "이거 보고 소름 돋았습니다"
    3. 비밀/폭로형: "관계자들만 알던 비하인드", "결국 수면 위로 드러난 진실"
    4. 숫자/디테일: "단 10초 만에", "시청률 3배 폭등한 결정적 장면"
    5. 따옴표 인용형: "진짜 미쳤다 소리 절로 나오는", "이 조합이 실화냐고 난리 난"

    [주의 사항]:
    - 뻔하고 진부한 어휘(총정리, 화제, 집중 조명, 핵심 등)는 절대 사용 금지.
    - 기자명, 날짜, 언론사명, [ ] 대괄호는 제목에 절대 포함하지 마세요.
    - 1줄당 14자~20자 내외로 화면에 강렬하게 꽂히도록 작성하세요.
    - 매번 호출될 때마다 완전히 새로운 어휘와 파격적인 관점을 시도하세요.

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
                    response_schema=HeadlineCandidates,
                    temperature=0.95
                )
            )
            data = json.loads(res.text)
            titles = [sanitize_korean_text(t) for t in data.get("titles", [])]
            if len(titles) >= 3:
                return titles[:5]
        except Exception:
            continue

    return random.sample(random_fallbacks, 5)

def generate_news_content(title, text):
    clean_t = sanitize_korean_text(title)
    
    prompt = f"""
    당신은 인스타그램 트렌드 뉴스 수석 에디터입니다. 아래 기사에서 대중이 가장 흥미로워할 '핵심 도파민 포인트'를 짚어 작성하세요.
    뻔한 줄거리 요약이 아니라, 왜 사람들이 열광하거나 논란인지 구체적인 사실을 바탕으로 작성해야 합니다.

    1. card_subcopy:
       - 피드 1장 카드에 들어갈 본문 요약 (70~90자).
       - "누가 어떤 파격적인 상황/행동을 했는지 + 여론의 반응"을 마침표 1~2개 완결 문장으로 임팩트 있게 서술하세요.
       - 제작사, 기자 이름, 단순 방영 일정 같은 지루한 정보는 절대 금지.

    2. image_prompt:
       - 배경으로 쓸 고화질 시네마틱 이미지 생성용 영어 프롬프트 (인물/상황 중심, 8k cinematic lighting).

    3. empathy (공감형):
       - 사건의 실체와 핵심 장면을 생생하게 설명한 뒤 독자의 일상과 감정을 자극하는 인스타 본문 (이모지 포함).

    4. vote (투표형):
       - "A(완전 호감/사이다) vs B(선 넘었다/지켜봐야 함)"처럼 댓글 창에서 밤새 토론할 수 있는 극단적인 선택지 제시.

    5. explain (설명형):
       - 1️⃣ 팩트 체킹 2️⃣ 숨겨진 반전 3️⃣ 앞으로의 파장 구조로 정리한 저장 유도형 본문.

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
                    temperature=0.85
                )
            )
            data = json.loads(res.text)
            return {
                "card_subcopy": sanitize_korean_text(data.get("card_subcopy", "")),
                "image_prompt": data.get("image_prompt", "dramatic cinematic scene"),
                "empathy": data.get("empathy", ""),
                "vote": data.get("vote", ""),
                "explain": data.get("explain", "")
            }
        except Exception:
            continue

    return {
        "card_subcopy": f"기존의 상식을 뒤엎는 파격적인 캐릭터 변신과 거침없는 전개로 공개 직후 커뮤니티가 발칵 뒤집혔습니다.",
        "image_prompt": "dramatic cinematic scene, intense lighting, 8k",
        "empathy": f"🔥 {clean_t}\n\n이 소식 듣고 다들 어떻게 생각하셨나요? 실시간 반응이 정말 뜨겁습니다.",
        "vote": f"🔥 {clean_t}\n\n👉 A. 완벽하게 사이다다\n👉 B. 조금 과한 것 같다\n\n여러분의 선택을 댓글로 남겨주세요!",
        "explain": f"📌 {clean_t} 핵심 정리\n\n1️⃣ 화제의 배경\n2️⃣ 대중의 반응\n3️⃣ 향후 전망"
    }

# =============================================
# 단일 카드 렌더링 엔진 (인스타그램 공식 규격: 1080x1350)
# =============================================
def render_single_card(title_text, sub_text, base_img, title_size, content_size, text_y_pos):
    width, height = 1080, 1350
    t_font, c_font, b_font = load_fonts(title_size, content_size)

    base_img = smart_fit_or_crop(base_img, width, height)

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

    tag_clean = "TREND ISSUE"
    draw.rounded_rectangle([64, 64, 230, 110], radius=22, fill=(15, 23, 42, 220))
    draw.rounded_rectangle([64, 64, 230, 110], radius=22, outline=(255, 255, 255, 70), width=1)
    draw.ellipse([80, 82, 88, 90], fill=(250, 204, 21))
    draw.text((98, 76), tag_clean, font=b_font, fill=(255, 255, 255))

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
        "article_images": [],
        "ai_generated_images": [],
        "current_image_source": "ai",
        "current_img_idx": 0,
        "title_size": 52,
        "content_size": 26,
        "text_y": 880,
        "image_prompt": "",
        "seed": 42
    }

# =============================================
# 📱 메인 화면 UI
# =============================================
st.markdown("<h2 style='text-align: center; margin-bottom: 5px;'>🚀 인스타 보너스·뉴스 카드뉴스 생성기</h2>", unsafe_allow_html=True)
st.markdown("<p style='text-align: center; color: #64748B; margin-bottom: 25px;'>기사 링크만 넣으면 맞춤 AI 생성 이미지와 팩트 요약으로 1분 만에 완성합니다</p>", unsafe_allow_html=True)

news_url = st.text_input("🔗 뉴스 기사 링크 입력", placeholder="네이버/다음 등 포털 뉴스 기사 링크를 붙여넣으세요")

if st.button("✨ 인스타 게시물 만들기", type="primary", use_container_width=True):
    if not news_url.strip():
        st.warning("뉴스 링크를 입력해 주세요.")
    else:
        with st.spinner("기사 팩트 분석 및 맞춤 AI 생성 이미지를 제작하고 있습니다..."):
            try:
                art = Article(news_url, language='ko')
                art.download()
                art.parse()

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

                copies = generate_ai_copies(art.title, art.text)
                content_res = generate_news_content(art.title, art.text)
                ai_img = generate_ai_custom_image(content_res["image_prompt"], seed_val=int(time.time()) % 1000)

                st.session_state.app_state["is_ready"] = True
                st.session_state.app_state["copies"] = copies
                st.session_state.app_state["active_title"] = copies[0]
                st.session_state.app_state["active_sub"] = content_res["card_subcopy"]
                st.session_state.app_state["image_prompt"] = content_res["image_prompt"]
                st.session_state.app_state["captions"] = {
                    "empathy": content_res["empathy"],
                    "vote": content_res["vote"],
                    "explain": content_res["explain"]
                }
                st.session_state.app_state["article_images"] = art_img_pool
                st.session_state.app_state["ai_generated_images"] = [ai_img]
                st.session_state.app_state["current_image_source"] = "ai"
                st.session_state.app_state["current_img_idx"] = 0

            except Exception as e:
                st.error(f"기사 분석 실패: {e}")

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

    if state["current_image_source"] == "ai":
        active_bg_img = state["ai_generated_images"][0]
        badge_desc = "🤖 맞춤 AI 생성 이미지"
    else:
        active_bg_img = state["article_images"][state["current_img_idx"]]
        badge_desc = f"📰 기사 원문 사진 ({state['current_img_idx'] + 1}/{len(state['article_images'])})"

    rendered_img = render_single_card(
        state["active_title"],
        state["active_sub"],
        active_bg_img,
        state["title_size"],
        state["content_size"],
        state["text_y"]
    )

    st.image(rendered_img, caption=f"📱 완성된 인스타그램 피드 (1080x1350) · {badge_desc}", use_container_width=True)

    col_img1, col_img2 = st.columns(2)
    with col_img1:
        if st.button("🎨 AI로 다른 이미지 다시 그리기", use_container_width=True):
            with st.spinner("새로운 스타일로 이미지를 다시 그리고 있습니다..."):
                new_seed = int(time.time() * 10) % 9999
                new_ai_img = generate_ai_custom_image(state["image_prompt"], seed_val=new_seed)
                state["ai_generated_images"] = [new_ai_img]
                state["current_image_source"] = "ai"
                st.rerun()
    with col_img2:
        if st.button("📰 기사 원문 스틸컷으로 전환/변경", use_container_width=True):
            state["current_image_source"] = "article"
            state["current_img_idx"] = (state["current_img_idx"] + 1) % len(state["article_images"])
            st.rerun()

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
