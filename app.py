# 1단계 제목 생성 함수 (429 발생 시 즉시 다른 모델로 전환)
def call_gemini_headlines(prompt):
    # 무료 쿼터가 분리되어 있는 모델들을 폭넓게 후보군으로 지정
    candidate_models = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.1-pro-preview"
    ]
    last_err = None
    for model_name in candidate_models:
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
            # 404(모델 없음), 429(일일 할당량 소진) 발생 시 지체 없이 다음 모델로 건너뜀
            if "404" in err_str or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                continue
            elif "503" in err_str:
                time.sleep(1)
                continue
            else:
                continue
    raise Exception(f"모든 AI 모델의 일일 호출 한도가 초과되었습니다: {last_err}")

# 2단계 스크립트 생성 함수 (429 발생 시 즉시 다른 모델로 전환)
def call_gemini_script(prompt):
    candidate_models = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.1-pro-preview"
    ]
    last_err = None
    for model_name in candidate_models:
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
            # 404, 429 발생 시 지체 없이 다음 모델로 건너뜀
            if "404" in err_str or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                continue
            elif "503" in err_str:
                time.sleep(1)
                continue
            else:
                continue
    raise Exception(f"모든 AI 모델의 일일 호출 한도가 초과되었습니다: {last_err}")
