import os
import json
import re
from string import Template
import anthropic

from utils.claude_auth import CLAUDE_BASE_URL, get_claude_token

# 프롬프트 파일 디렉터리 (프로젝트 루트/prompts)
_PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")
# 파일 내용 캐시 (반복 디스크 IO 방지)
_PROMPT_CACHE: dict = {}


def _load_prompt_file(rel_path: str) -> str:
    """prompts/ 하위 파일을 읽어서 반환. 없으면 빈 문자열."""
    if rel_path in _PROMPT_CACHE:
        return _PROMPT_CACHE[rel_path]
    path = os.path.join(_PROMPT_DIR, rel_path)
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[post_writer] 프롬프트 파일 없음 → {path}")
        content = ""
    _PROMPT_CACHE[rel_path] = content
    return content


def _strip_comments(text: str) -> str:
    """'#'로 시작하는 마커/주석 줄을 제거하고 양끝 공백 정리."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()

# [메인] orchestrator에서 호출
async def post_writer_agent(
    shop_id: str,
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict,
    recent_posts: list,
    rag_context: dict,
    previous_draft: dict = None,    # 재작성 시 이전 초안
    feedback: str = None,           # 재작성 시 피드백
    user_request: str = None        # 사장님 직접 요청 (manual)
) -> dict:
    """
    게시물 작성 에이전트 메인 함수

    orchestrator STEP 4에서 호출.
    최초 작성 또는 재작성(previous_draft + feedback 있을 때) 모두 처리.

    Returns:
        {"caption": "...", "hashtags": [...], "cta": "..."}
    """
    is_rewrite = previous_draft is not None
    mode = "재작성" if is_rewrite else "최초 작성"
    print(f"[post_writer] 시작 → shop_id={shop_id}, 모드={mode}")

    client = _init_claude_client()

    # 프롬프트 구성
    system_prompt, user_prompt = _build_prompt(
        trend_data=trend_data,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=recent_posts,
        rag_context=rag_context,
        previous_draft=previous_draft,
        feedback=feedback,
        user_request=user_request
    )

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            temperature=0.85,   # 자연스러운 말투 + 일관성 균형
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)

        # 금칙어 + 할루시네이션 검증
        result = _validate_and_clean(result, brand_settings)

        # 할루시네이션 감지 시 한 번 재시도
        if result.get("needs_retry"):
            reason = result.get("retry_reason", "할루시네이션")
            print(f"[post_writer] {reason} 감지 → 재시도 (feedback 주입)")
            feedback_msg = f"이전 캡션에서 '{reason}'이 감지됐어. 확인되지 않은 사실은 절대 쓰지 마."
            response2 = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=600,
                temperature=0.85,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": str(result.get("caption", ""))},
                    {"role": "user", "content": feedback_msg},
                ]
            )
            raw2 = response2.content[0].text.strip().replace("```json", "").replace("```", "").strip()
            try:
                result = _validate_and_clean(json.loads(raw2), brand_settings)
            except Exception:
                pass  # 재시도도 실패하면 원본 그대로 사용

        result.pop("needs_retry", None)
        result.pop("retry_reason", None)

        # CTA는 DB 값으로 강제 덮어씌우기 (GPT가 임의로 바꾸지 못하게)
        cta_fixed = brand_settings.get("cta", "").strip()
        if cta_fixed:
            result["cta"] = cta_fixed
            print(f"[post_writer] CTA 고정 적용 → '{cta_fixed}'")

        print(f"[post_writer] 완료 → 캡션 {len(result.get('caption', ''))}자, "
              f"해시태그 {len(result.get('hashtags', []))}개")
        return result

    except Exception as e:
        print(f"[post_writer] GPT 실패 ({e}) → fallback 캡션 반환")
        return _fallback_draft(brand_settings, trend_data)


# [프롬프트] 통합 프롬프트 구성
def _build_prompt(
    trend_data: dict,
    selected_photos: list,
    brand_settings: dict,
    recent_posts: list,
    rag_context: dict,
    previous_draft: dict = None,
    feedback: str = None,
    user_request: str = None
) -> tuple:
    """
    시스템 프롬프트 + 유저 프롬프트 구성

    구조:
      시스템: 역할 + 브랜드 설정 + 응답 형식
      유저:   트렌드 + 사진 스타일 + RAG 예시 + 최근 말투 + (재작성 시 피드백)
    """

    # ── 시스템 프롬프트 ──

    # [FIX 1] brand_tone: 리스트 항목을 / 로 구분해서 GPT가 각 톤을 명확히 인식
    brand_tone = brand_settings.get("brand_tone", "친근하고 편안한 말투")
    if isinstance(brand_tone, list):
        brand_tone = " / ".join(brand_tone)  # 공백 대신 /로 구분 (혼합 톤 명확히 전달)

    # forbidden_words 리스트 처리
    forbidden_words = brand_settings.get("forbidden_words", [])
    if isinstance(forbidden_words, str):
        forbidden_words = [w.strip() for w in forbidden_words.split(",")]
    forbidden_str = ", ".join(forbidden_words) if forbidden_words else "없음"

    feed_style    = brand_settings.get("feed_style", {})
    emoji_usage   = feed_style.get("emoji_usage", "적당히")
    caption_len   = feed_style.get("caption_length", "2~4줄")
    hashtag_count = feed_style.get("hashtag_count", 10)

    # 이모지 "안 씀"은 단순 톤 힌트로는 약하게 인식됨 → 강한 금지 지시로 변환
    if emoji_usage == "안 씀":
        emoji_instruction = "이모지: 절대 사용 금지 — 캡션과 CTA 어디에도 이모지(✂️💈🔥 등)를 넣지 마"
    else:
        emoji_instruction = f"이모지: {emoji_usage}"

    # [FIX 2] hashtag_style: 리스트 항목을 모두 문자열로 변환
    # 공백 구분 — 콤마 나열 패턴을 LLM이 흉내내 출력 해시태그에 콤마 붙는 것 방지
    hashtag_style = brand_settings.get("hashtag_style", "감성형")
    if isinstance(hashtag_style, list):
        hashtag_style = " ".join(hashtag_style)  # 리스트 → 공백 구분 문자열

    # 필수 해시태그: hashtag_style 에서 #로 시작하는 항목 추출 (must_include_hashtags 필드 폐지)
    # 콤마/공백 전까지만 매칭 ([^\s,]) — 구분자가 콤마든 공백이든 안전
    extracted_hashtags = [w.strip() for w in re.findall(r'#[^\s,]+', hashtag_style)] if hashtag_style else []
    must_hashtag_str = " ".join(extracted_hashtags) if extracted_hashtags else ""

    preferred_styles = brand_settings.get("preferred_styles", [])
    if isinstance(preferred_styles, str):
        preferred_styles = [s.strip() for s in preferred_styles.split(",") if s.strip()]
    preferred_str = ", ".join(preferred_styles) if preferred_styles else "페이드컷, 투블럭 등 바버샵 스타일"

    exclude_conditions = brand_settings.get("exclude_conditions", [])
    if isinstance(exclude_conditions, str):
        exclude_conditions = [s.strip() for s in exclude_conditions.split(",") if s.strip()]
    exclude_str = ", ".join(exclude_conditions) if exclude_conditions else "없음"

    # [FIX 3] CTA 고정 명시 — 시스템 프롬프트에서부터 강제
    cta_fixed = brand_settings.get("cta", "").strip()
    cta_instruction = f"반드시 아래 문구 그대로 사용, 절대 바꾸지 마:\n  → \"{cta_fixed}\"" if cta_fixed else "자연스러운 예약 유도 문구"

    # shop_intro 있으면 시스템 프롬프트에 포함 (할루시네이션 오탐 방지용)
    shop_intro = brand_settings.get("shop_intro", "").strip()
    shop_intro_line = f"\n[샵 소개 - 이 내용은 사실이므로 캡션에 자연스럽게 활용 가능]\n{shop_intro}" if shop_intro else ""

    # ── 출력 언어 지시 (레이어1: 언어별 프롬프트 파일 없이 동적 주입) ──
    language = brand_settings.get("language", "ko")
    LANG_NAMES = {"ko": "한국어", "en": "English", "ja": "日本語",
                  "zh": "中文", "es": "Español"}
    lang_name = LANG_NAMES.get(language, language)
    if language != "ko":
        lang_instruction = f"\n\n[출력 언어 — 매우 중요]\n반드시 {lang_name}로 캡션과 해시태그, CTA를 작성해줘. 자연스러운 {lang_name} 표현으로 쓰되, 바버샵 정체성과 마케팅 의도는 그대로 유지해."
    else:
        lang_instruction = ""

    # insta_style_profile: 과거 게시물 분석 결과를 few-shot으로 주입
    insta_profile = brand_settings.get("insta_style_profile", {})
    insta_style_block = ""
    if insta_profile:
        tone_desc = insta_profile.get("tone_description", "")
        sentence_ending = insta_profile.get("sentence_ending", "")
        signature_expr = insta_profile.get("signature_expressions", [])
        sentence_length = insta_profile.get("sentence_length", "")
        emoji_pattern = insta_profile.get("emoji_pattern", "")
        tone_examples = insta_profile.get("tone_examples", [])

        # 언어 안전장치: profile 분석 언어 != 출력 언어면
        # 언어종속 필드(종결어미/특유표현/예시캡션)는 스킵, 구조적 특징만 주입
        profile_lang = insta_profile.get("detected_language", "ko")
        lang_mismatch = language != profile_lang

        lines = []
        if tone_desc:
            lines.append(f"- 말투 특징: {tone_desc}")

        # 언어 일치할 때만 언어종속 필드 주입
        if not lang_mismatch:
            if sentence_ending:
                lines.append(f"- 자주 쓰는 종결어미: {sentence_ending} (이 말끝을 살려줘)")
            if signature_expr:
                expr_str = ", ".join(f'"{e}"' for e in signature_expr[:5])
                lines.append(f"- 이 사장님 특유의 표현(가능하면 자연스럽게 녹여): {expr_str}")

        # 구조적 필드 — 언어 무관, 항상 주입
        if sentence_length:
            lines.append(f"- 문장 길이 습관: {sentence_length}")
        # 이모지 패턴: 언어 무관(✂️💈는 만국 공통) + emoji_usage "안 씀"이면 스킵
        if emoji_pattern and emoji_usage != "안 씀":
            lines.append(f"- 이모지 습관: {emoji_pattern}")

        # 예시 캡션도 언어종속 → 일치할 때만
        if not lang_mismatch and tone_examples:
            # 이모지 스트립: "안 씀" shop이면 예시에서 이모지 제거
            examples_clean = []
            for ex in tone_examples[:3]:
                if emoji_usage == "안 씀":
                    ex = re.sub(r'[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F300-\U0001F9FF✀-➿☀-⛿✂✅✈-✍✏✒✔✖✨✳✴❄❇❌❎❓-❕❗❣❤➕-➗➡➰➿⤴⤵⬅-⬇⬛⬜⭐⭕]', '', ex).strip()
                examples_clean.append(ex)
            ex_lines = "\n".join(f'  "{ex}"' for ex in examples_clean)
            lines.append(f"- 실제 사장님 캡션 예시:\n{ex_lines}")

        if lines:
            insta_style_block = "\n\n[이 사장님의 실제 인스타 말투 — 이 말투와 동일하게 써줘]\n" + "\n".join(lines)
            if lang_mismatch:
                insta_style_block += f"\n위 말투 특징을 {lang_name} 표현으로 자연스럽게 반영해줘. (원문 말투 예시는 언어가 달라 생략)"
            else:
                insta_style_block += "\n⚠️ 위 실제 말투와 최대한 동일하게. 광고체 절대 금지."

    # 시스템 프롬프트 구성 — prompts/post_writer/system.md 에서 로드 후 치환
    # few-shot 예시(good/bad)는 examples/ 파일에서 주입
    # '#' 주석 줄은 파일 마커용이므로 프롬프트에서 제외 (예: 예시 비어있을 때)
    good_captions = _strip_comments(_load_prompt_file("examples/good_captions.md"))
    bad_captions  = _strip_comments(_load_prompt_file("examples/bad_captions.md"))
    good_block = f"{good_captions}\n\n" if good_captions else ""
    bad_block  = f"{bad_captions}\n\n"  if bad_captions  else ""

    system_template = _load_prompt_file("post_writer/system.md")
    system_prompt = Template(system_template).safe_substitute(
        shop_intro_line=shop_intro_line,
        insta_style_block=insta_style_block,
        lang_instruction=lang_instruction,
        brand_tone=brand_tone,
        emoji_instruction=emoji_instruction,
        caption_len=caption_len,
        preferred_str=preferred_str,
        forbidden_str=forbidden_str,
        exclude_str=exclude_str,
        hashtag_count=hashtag_count,
        hashtag_style=hashtag_style,
        must_hashtag_str=must_hashtag_str if must_hashtag_str else "없음",
        cta_instruction=cta_instruction,
        cta_fixed=cta_fixed if cta_fixed else "예약 유도 문구",
        good_captions_block=good_block,
        bad_captions_block=bad_block,
    )

    # ── 유저 프롬프트 ──
    parts = []

    # 1. 오늘 트렌드
    trend_summary = trend_data.get("trend", "")
    weather       = trend_data.get("weather", "")
    promo         = trend_data.get("promo", "")

    parts.append(f"[오늘 트렌드]\n{trend_summary}")
    if weather:
        parts.append(f"[날씨/시즌]\n{weather}")
    if promo:
        parts.append(f"[바버샵 홍보 포인트]\n{promo}")

    # 샵 차별점 - brand_differentiation 있을 때만 반영
    brand_diff = brand_settings.get("brand_differentiation", "").strip()
    if brand_diff:
        parts.append(f"[우리 샵 차별점 - 첫 문장에 자연스럽게 녹여줘]\n{brand_diff}")

    # 실제 검색 스니펫 - 사람들이 실제로 쓰는 말투 참고용
    raw_snippets = trend_data.get("raw_snippets", [])
    if raw_snippets:
        snippet_text = "\n".join(f"- {s}" for s in raw_snippets[:3])
        parts.append(f"[실제 검색에서 수집한 표현 - 말투 참고만, 그대로 복붙 금지]\n{snippet_text}")

    # 2. 선택된 사진 스타일
    if selected_photos:
        style_info = []
        for photo in selected_photos:
            tags = photo.get("style_tags", photo.get("stage2_tags", []))
            if tags:
                style_info.append(f"- {', '.join(tags)}")
        if style_info:
            parts.append(f"[오늘 올릴 사진 스타일]\n" + "\n".join(style_info))

    # 3. RAG 예시 (과거 게시물 패턴)
    if rag_context:
        tone_rules       = rag_context.get("tone_rules", "")
        examples         = rag_context.get("examples", [])
        hashtag_patterns = rag_context.get("hashtag_patterns", [])

        if hashtag_patterns:
            parts.append(f"[자주 쓰는 해시태그]\n{' '.join(hashtag_patterns[:10])}")

        # 성과 인사이트
        if rag_context.get("performance_insights"):
            parts.append(f"[과거 성과 패턴 - 이 패턴대로 써줘]\n{rag_context['performance_insights']}")

        if tone_rules:
            parts.append(f"[이 샵의 말투 패턴]\n{tone_rules}")

        if examples:
            ex_text = "[이 샵의 과거 게시물 — 이 말투와 비슷하게 써줘]\n"
            for i, ex in enumerate(examples[:3], 1):
                caption  = ex.get("caption", "")
                hashtags = ex.get("hashtags", [])
                ex_text += f"{i}. {caption[:80]}{'...' if len(caption) > 80 else ''}\n"
                if hashtags:
                    ex_text += f"   해시태그: {' '.join(hashtags[:5])}\n"
            parts.append(ex_text)

    # 4. 최근 게시물 말투 참고
    if recent_posts:
        recent_text = "[최근 게시물 말투 참고 (이 말투와 비슷하게)]\n"
        for i, post in enumerate(recent_posts[:2], 1):
            caption = post.get("caption", "")
            recent_text += f"{i}. {caption[:60]}{'...' if len(caption) > 60 else ''}\n"
        parts.append(recent_text)

    # 5. 재작성 시 피드백 추가
    if previous_draft and feedback:
        prev_caption = previous_draft.get("caption", "")
        parts.append(
            f"[이전 초안 - 수정 필요]\n{prev_caption}\n\n"
            f"[수정 요청]\n{feedback}\n\n"
            f"마케터 관점으로 재작성: 문의율 올리는 데 집중."
        )
    else:
        parts.append(
            "위 내용 참고해서 게시물 써줘.\n"
            "체크리스트:\n"
            "✅ 첫 문장에 메인 키워드 배치\n"
            "✅ 타겟 고객 니즈 자극\n"
            "✅ 긴박감 있는 CTA\n"
            "✅ 검색량 높은 해시태그 우선 배치"
        )

    # 사장님 직접 요청(manual message)이 있으면 최우선으로 맨 앞에 배치
    if user_request:
        parts.insert(0, f"[사장님 요청 - 이 요청을 최우선으로 반영해줘]\n{user_request}")

    user_prompt = "\n\n".join(parts)
    return system_prompt, user_prompt


# 할루시네이션 방지 - 바버샵 무관 주제 키워드
_FORBIDDEN_TOPICS = [
    "레이어컷", "펌", "염색", "여성", "헤어숍", "미용실",
    "네일", "왁싱", "속눈썹", "피부", "스킨케어",
]

# 과장 표현 금지
_FORBIDDEN_EXAGGERATIONS = [
    "최고의", "완벽한", "세계 최초", "혁신적인", "압도적인",
    "독보적인", "전국 1위", "업계 최고",
]


# [검증] 금칙어 + 할루시네이션 자동 제거
def _validate_and_clean(result: dict, brand_settings: dict) -> dict:
    """
    AG-042 강화: 금칙어 + 주제 이탈 + 과장 표현 3중 검사

    1) 금칙어 (브랜드 설정)  → 자동 제거
    2) 주제 이탈 키워드      → 자동 제거 + 경고
    3) 과장 표현             → 자동 제거 + 경고

    [FIX 4] shop_intro에 명시된 내용은 할루시네이션 오탐에서 제외
    """
    # forbidden_words 리스트 처리
    forbidden_words = brand_settings.get("forbidden_words", [])
    if isinstance(forbidden_words, str):
        forbidden_words = [w.strip() for w in forbidden_words.split(",")]

    # [FIX 4] shop_intro 값 미리 추출 — 오탐 방지용
    shop_intro = brand_settings.get("shop_intro", "").strip()

    caption = result.get("caption", "")

    # 0) 할루시네이션 패턴 감지 → 재생성 신호 (제거 말고 플래그)
    hallucination_patterns = [
        (r'\d+년\s*경력',   "경력 연수 할루시네이션"),
        (r'\d+자리\s*남',   "예약 현황 할루시네이션"),
        (r'마감\s*임박',     "마감 임박 할루시네이션"),
        (r'오늘만\s*할인',   "근거없는 할인 할루시네이션"),
    ]
    for pattern, label in hallucination_patterns:
        match = re.search(pattern, caption)
        if match:
            # [FIX 4] shop_intro에 이미 있는 내용이면 오탐 → 통과
            matched_text = match.group(0)
            if shop_intro and matched_text in shop_intro:
                print(f"[post_writer] '{matched_text}' → shop_intro에 명시된 사실, 통과")
                continue
            print(f"[post_writer] ⚠️  {label} 감지 → needs_retry=True")
            result["needs_retry"] = True
            result["retry_reason"] = label
            return result

    # 1) 금칙어 제거
    found_forbidden = []
    for word in forbidden_words:
        if word in caption:
            found_forbidden.append(word)
            caption = caption.replace(word, "")
    if found_forbidden:
        print(f"[post_writer] AG-042 금칙어 제거: {found_forbidden}")

    # 2) 주제 이탈 제거
    found_topics = []
    for word in _FORBIDDEN_TOPICS:
        if word in caption:
            found_topics.append(word)
            caption = caption.replace(word, "")
    if found_topics:
        print(f"[post_writer] AG-042 주제 이탈 키워드 제거: {found_topics}")

    # 3) 과장 표현 제거
    found_exaggerations = []
    for word in _FORBIDDEN_EXAGGERATIONS:
        if word in caption:
            found_exaggerations.append(word)
            caption = caption.replace(word, "")
    if found_exaggerations:
        print(f"[post_writer] AG-042 과장 표현 제거: {found_exaggerations}")

    result["caption"] = caption.strip()

    # 해시태그에서도 금칙어 + 주제 이탈 제거
    all_banned = forbidden_words + _FORBIDDEN_TOPICS
    hashtags = result.get("hashtags", [])
    result["hashtags"] = [
        tag for tag in hashtags
        if not any(word in tag for word in all_banned)
    ]

    # 필수 해시태그 강제 추가: hashtag_style 에서 #로 시작하는 항목 추출 (must_include_hashtags 필드 폐지)
    hashtag_style = brand_settings.get("hashtag_style", "")
    if isinstance(hashtag_style, list):
        hashtag_style = ", ".join(hashtag_style)
    extracted_hashtags = [w.strip() for w in re.findall(r'#[^\s,]+', hashtag_style)]
    for tag in extracted_hashtags:
        normalized = tag if tag.startswith("#") else f"#{tag}"
        if normalized not in result["hashtags"]:
            result["hashtags"].append(normalized)

    print(f"[post_writer] 최종 hashtags: {result['hashtags']}")
    return result


# [Fallback] GPT 실패 시 기본 초안
def _fallback_draft(brand_settings: dict, trend_data: dict) -> dict:
    """
    GPT 호출 실패 시 기본 초안 반환.
    최소한의 내용으로 파이프라인이 멈추지 않게 유지.
    """
    cta   = brand_settings.get("cta", "DM으로 예약 문의주세요")
    trend = trend_data.get("trend", "")

    caption = "오늘도 깔끔한 스타일로 새로운 하루를 시작해보세요 ✂️"
    if trend:
        caption += f"\n{trend[:30]}"

    return {
        "caption":  caption,
        "hashtags": ["#바버샵", "#헤어스타일", "#남성헤어", "#페이드컷"],
        "cta":      cta
    }


# [Claude 클라이언트 초기화]
def _init_claude_client():
    # Claude Sonnet 4.6 (Azure Foundry) - AAD(Entra) 토큰 인증. /anthropic 경로는 api-key 미지원.
    return anthropic.AsyncAnthropic(
        base_url=CLAUDE_BASE_URL,
        auth_token=get_claude_token(),
        timeout=anthropic.Timeout(30.0)
    )