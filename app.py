# =============================================
# 진짜 기사 팩트 기반 AI 생성 엔진 (가짜 Fallback 완전 제거)
# =============================================
# 지원되는 공식 최신 Flash 모델군
PRIMARY_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]

def generate_ai_copies(title, text):
    clean_t = sanitize_korean_text(title)
    
    if not client:
        raise Exception("Gemini API 클라이언트가 연결되지 않았습니다. API Key를 확인해 주세요.")

    prompt = f"""
    당신은 SNS 뉴스 에디터입니다. 아래 제공된 기사의 '실제 사건 내용'에 정확히 부합하는 후킹 제목 5개를 작성하세요.
    
    [절대 주의]:
    - 기사 분야(정치, 사회, 경제, IT, 연예 등)에 맞는 적절한 어조를 쓰세요. (정치/외교 기사에 드라마나 연예인 말투 절대 금지)
    - 기사 제목: {clean_t}
    - 기사 본문 요약:
    {text[:1800]}

    [작성 형식]:
    - 1줄당 14~20자 내외로 화면에 깔끔하게 들어가도록 작성.
    - 기자명, 언론사명, 날짜, 대괄호 [] 제외.
    """

    last_error = None
    for model_name in PRIMARY_MODELS:
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
            if titles:
                return titles[:5]
        except Exception as e:
            last_error = e
            continue

    # 조용히 가짜 문구를 넘기지 않고 실제 에러를 터뜨려 사용자에게 알림
    raise Exception(f"AI 제목 생성 실패 (API 상태 확인 필요): {last_error}")

def generate_news_content(title, text):
    clean_t = sanitize_korean_text(title)
    
    if not client:
        raise Exception("Gemini API 클라이언트가 연결되지 않았습니다. API Key를 확인해 주세요.")

    prompt = f"""
    당신은 전문 뉴스 에디터입니다. 아래 기사의 실제 팩트만을 바탕으로 카드뉴스 요약과 인스타 캡션을 작성하세요.

    [작성 요구사항]:
    1. card_subcopy:
       - 피드 카드에 들어갈 본문 요약 (70~90자).
       - 이 기사가 '누가, 무엇을 했고, 어떤 파장이나 쟁점이 있는지'를 정확한 팩트로 1~2개 완결된 문장으로 서술하세요.
       - 기사 내용과 무관한 미사여구나 드라마/엔터 문구 절대 금지.
    2. image_prompt:
       - 기사 주제에 어울리는 현실적이고 시네마틱한 배경 이미지 영문 프롬프트 (예: 정치/외교면 'white house intelligence meeting room serious lighting').
    3. empathy (공감형): 기사의 실제 사실 관계를 2~3줄로 설명한 뒤 의견을 묻는 캡션.
    4. vote (투표형): 기사의 실제 찬반/갈등 쟁점을 바탕으로 한 A vs B 투표 캡션.
    5. explain (설명형): 기사의 핵심 팩트 3줄 요약 캡션.

    기사 제목: {clean_t}
    기사 본문 내용:
    {text[:2000]}
    """

    last_error = None
    for model_name in PRIMARY_MODELS:
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
                "card_subcopy": sanitize_korean_text(data.get("card_subcopy", "")),
                "image_prompt": data.get("image_prompt", "editorial news documentary background"),
                "empathy": data.get("empathy", ""),
                "vote": data.get("vote", ""),
                "explain": data.get("explain", "")
            }
        except Exception as e:
            last_error = e
            continue

    raise Exception(f"AI 본문 분석 실패 (API 상태 확인 필요): {last_error}")
