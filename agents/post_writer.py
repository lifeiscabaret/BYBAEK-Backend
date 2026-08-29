import os
import json
import random
import re
from string import Template
import anthropic

# ──────────────────────────────────────────
# [다양성] insta_style_profile은 인스타 연동 시점에 1회 분석된 고정 스냅샷이다.
# 재분석 트리거가 없어서, 매 생성마다 같은 특유표현/예시캡션이 통째로 주입되면
# "말투는 똑같고 단어만 바뀌는" 캡션이 반복된다.
# → 프로필 값 중 일부만 매번 무작위로 골라 힌트로 주입한다.
# ──────────────────────────────────────────
_SIGNATURE_EXPR_SAMPLE = 2   # signature_expressions 중 매번 뽑을 개수
_TONE_EXAMPLE_SAMPLE   = 1   # tone_examples 중 매번 뽑을 개수


def _sample_for_variety(items: list, k: int) -> list:
    """items에서 최대 k개를 무작위로 뽑는다. 원소가 k개 이하면 그대로 반환."""
    if not items:
        return []
    if len(items) <= k:
        return list(items)
    return random.sample(list(items), k)

from utils.claude_auth import CLAUDE_BASE_URL, get_claude_model, get_claude_token
from utils.photo_vision import build_photo_image_blocks

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


def _repair_unescaped_quotes(raw: str) -> str:
    """JSON 문자열 값 안에 이스케이프 없이 들어간 큰따옴표를 복구한다.

    실측: 이 샵 CTA가 '예약: 네이버 검색창 "us바버샵용산본점" / 문의: ...' 처럼 따옴표를 포함해서,
    모델이 그대로 옮겨 적으면 json.loads가 깨지고 → fallback(목업) 캡션이 나갔다.
    문자열 안의 " 뒤에 오는 첫 비공백 문자가 구조 문자(, : } ])가 아니면 값의 일부로 보고 이스케이프한다.
    """
    out = []
    in_str = False
    escaped = False
    for i, ch in enumerate(raw):
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            continue
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == '\\':
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            j = i + 1
            while j < len(raw) and raw[j] in ' \t\r\n':
                j += 1
            if j >= len(raw) or raw[j] in ',:}]':
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')
            continue
        out.append(ch)
    return "".join(out)


def _extract_json_object(raw: str) -> str:
    """앞뒤에 붙은 설명 문장을 걷어내고 바깥 { ... } 덩어리만 남긴다.

    실측: 사진이 바버샵과 무관한 이미지(샵 앨범에 섞여 있던 모델 사진)일 때
    모델이 "이 사진은 여성 모델이라 …" 같은 안내 문장을 JSON 앞뒤에 덧붙였고,
    그 때문에 파싱이 깨져 fallback(목업) 캡션이 나갔다.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return raw
    return raw[start:end + 1]


def _parse_draft_json(raw: str) -> dict:
    """모델 응답 → dict. 이스케이프 누락으로 깨진 JSON은 한 번 복구해서 재시도한다.

    복구 시도에만 strict=False를 쓴다. 캡션은 여러 줄이 기본이라 모델이 \\n 대신
    실제 개행을 그대로 흘리면 표준 파서가 거부하는데(control character), 이때도
    목업 캡션으로 빠지는 대신 값을 살린다. 정상 JSON의 해석은 달라지지 않는다.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    candidate = _extract_json_object(raw)
    if candidate != raw:
        try:
            result = json.loads(candidate)
            print("[post_writer] 응답에 섞인 설명 문장 제거 후 파싱 성공")
            return result
        except json.JSONDecodeError:
            pass

    result = json.loads(_repair_unescaped_quotes(candidate), strict=False)
    print("[post_writer] JSON 이스케이프 오류 복구 후 파싱 성공")
    return result


def _strip_comments(text: str) -> str:
    """'#'로 시작하는 마커/주석 줄을 제거하고 양끝 공백 정리."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()


# ──────────────────────────────────────────
# [NEW] good_captions.md 톤별 선택 주입
#
# 문제: 기존엔 good_captions.md 전체(모든 톤 카테고리)를 항상 프롬프트에 통째로 넣었음.
#       Few-shot 예시는 LLM 출력 스타일을 강하게 앵커링하기 때문에, 톤이 다른 샵끼리도
#       전부 같은 예시를 보고 캡션을 쓰면 결국 다 비슷한 캡션이 나오는 "AI 냄새" 획일화 문제가 생김.
#
# 해결: 온보딩에서 수집한 brand_tone 배열과 good_captions.md의 톤 카테고리 태그
#       (narrative/expertise/humor/community/tips)를 매칭해서, 겹치는 카테고리 1~2개만 선택 주입.
#       RAG(STEP 4, search_rag)가 이미 그 샵의 실제 과거 캡션으로 개인화를 담당하고 있으므로,
#       good_captions.md는 "콜드스타트 + 톤별 최소 품질 기준선" 역할만 하면 됨.
# ──────────────────────────────────────────

# 온보딩 auto-upload 위저드 Step3 STYLE_OPTIONS 라벨 → good_captions.md 카테고리 태그 매핑
# (STYLE_OPTIONS: 힙/스트릿 바이브 · 클래식 프리미엄 · 친근한 동네 바버 · 감성/무드)
_STYLE_TO_CAPTION_CATEGORY = {
    "힙/스트릿 바이브": "humor",
    "클래식 프리미엄": "expertise",
    "친근한 동네 바버": "community",
    "감성/무드": "narrative",
}

# brand_tone에 매칭되는 카테고리가 하나도 없을 때 사용할 무난한 기본 카테고리
_DEFAULT_CAPTION_CATEGORIES = ["expertise", "community"]

_CAPTION_CATEGORY_TAGS = {"narrative", "expertise", "humor", "community", "tips"}


def _parse_good_captions_by_category(raw_text: str) -> dict:
    """
    good_captions.md를 '## N. tag — 설명' 레벨2 헤더 기준으로 섹션 분리.
    known tag(narrative/expertise/humor/community/tips)로 시작하는 섹션만 인식하고,
    그 외(사용법 표, '다음 단계' 안내 등 개발자용 메모)는 자동으로 제외됨.

    반환: {tag: "헤더+본문 텍스트"}
    """
    sections = {}
    parts = re.split(r'(?m)^(##\s+.*)$', raw_text)
    # parts = [헤더 이전 텍스트, 헤더1, 본문1, 헤더2, 본문2, ...]
    for i in range(1, len(parts) - 1, 2):
        header = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        m = re.match(r'##\s*\d+\.\s*([a-zA-Z]+)', header)
        if not m:
            continue
        tag = m.group(1).lower()
        if tag in _CAPTION_CATEGORY_TAGS:
            sections[tag] = (header + body).strip()
    return sections


def _select_good_captions(brand_tone_raw) -> str:
    """
    brand_tone(원본 리스트)과 good_captions.md 카테고리 태그가 겹치는 예시만 골라 반환.
    최대 2개 카테고리까지만 (프롬프트 길이 + 톤 집중도 관리).
    파싱 실패/매칭 실패 시엔 안전하게 파일 전체로 폴백 (기존 동작 유지, 파이프라인 중단 방지).
    """
    raw = _load_prompt_file("examples/good_captions.md")
    if not raw:
        return ""

    sections = _parse_good_captions_by_category(raw)
    if not sections:
        print("[post_writer] good_captions.md 카테고리 파싱 실패 → 파일 전체로 폴백")
        return _strip_comments(raw)

    if isinstance(brand_tone_raw, str):
        brand_tone_list = [brand_tone_raw]
    else:
        brand_tone_list = brand_tone_raw or []

    matched_tags = []
    for tone_value in brand_tone_list:
        tag = _STYLE_TO_CAPTION_CATEGORY.get(tone_value)
        if tag and tag not in matched_tags:
            matched_tags.append(tag)

    if not matched_tags:
        matched_tags = [t for t in _DEFAULT_CAPTION_CATEGORIES if t in sections]
        print(f"[post_writer] brand_tone 매칭 카테고리 없음 → 기본값 사용: {matched_tags}")
    else:
        print(f"[post_writer] good_captions 카테고리 선택 → {matched_tags} (brand_tone={brand_tone_list})")

    matched_tags = matched_tags[:2]
    selected = [sections[t] for t in matched_tags if t in sections]

    if not selected:
        print("[post_writer] good_captions 카테고리 매칭 완전 실패 → 파일 전체로 폴백")
        return _strip_comments(raw)

    return _strip_comments("\n\n".join(selected))


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
    user_request: str = None,       # 사장님 직접 요청 (manual)
    photo_intent: str = "haircut",   # [v2] 사진 의도 분기 (haircut/shop_intro)
    before_after_pair_ids: list = None  # [v2.1] 비포/애프터 쌍 (manual 전용, 2개)
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
    # 최초 호출과 재시도 호출이 같은 모델을 쓰도록 한 번만 읽는다.
    model_name = get_claude_model()

    # 사진 실제 이미지(멀티모달) 준비 — 실패하면 빈 리스트라서 텍스트 태그만으로 진행된다.
    image_blocks = await build_photo_image_blocks(selected_photos)

    # 프롬프트 구성
    system_prompt, user_prompt = _build_prompt(
        trend_data=trend_data,
        selected_photos=selected_photos,
        brand_settings=brand_settings,
        recent_posts=recent_posts,
        rag_context=rag_context,
        previous_draft=previous_draft,
        feedback=feedback,
        user_request=user_request,
        attached_photo_count=len(image_blocks),
        photo_intent=photo_intent,
        before_after_pair_ids=before_after_pair_ids
    )

    # 이미지가 있으면 content block 리스트로 구성한다.
    # 이미지를 텍스트보다 앞에 두는 편이 인식률이 좋고, 여러 장일 땐 라벨을 붙여 구분한다.
    if image_blocks:
        user_content = []
        for i, block in enumerate(image_blocks, 1):
            user_content.append({"type": "text", "text": f"[사진 {i}]"})
            user_content.append(block)
        user_content.append({"type": "text", "text": user_prompt})
    else:
        user_content = user_prompt

    try:
        response = await client.messages.create(
            model=model_name,
            max_tokens=600,
            temperature=0.85,   # 자연스러운 말투 + 일관성 균형
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}]
        )

        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = _parse_draft_json(raw)

        # 금칙어 + 할루시네이션 검증
        result = _validate_and_clean(result, brand_settings)

        # 위반/할루시네이션 감지 시 한 번 재시도
        if result.get("needs_retry"):
            reason = result.get("retry_reason", "할루시네이션")
            violations = result.get("violations") or []
            print(f"[post_writer] {reason} 감지 → 재시도 (feedback 주입)")
            if violations:
                # 어떤 단어가 문제인지 명시해야 LLM이 그 단어만 피해서 다시 쓴다.
                # (예전엔 그 단어를 코드가 지워버려서 문장이 깨졌다)
                feedback_msg = (
                    f"이전 캡션에 쓰면 안 되는 표현이 있었어: {', '.join(violations)}. "
                    f"이 단어들을 쓰지 말고, 문장을 통째로 자연스럽게 다시 써줘. "
                    f"단어만 빼서 어색해지면 안 돼."
                )
            else:
                feedback_msg = f"이전 캡션에서 '{reason}'이 감지됐어. 확인되지 않은 사실은 절대 쓰지 마."
            response2 = await client.messages.create(
                model=model_name,
                max_tokens=600,
                temperature=0.85,
                system=system_prompt,
                messages=[
                    # 재시도에도 같은 이미지가 유지돼야 한다 (user_content 그대로 재사용)
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": str(result.get("caption", ""))},
                    {"role": "user", "content": feedback_msg},
                ]
            )
            raw2 = response2.content[0].text.strip().replace("```json", "").replace("```", "").strip()
            try:
                # 2차에도 위반이 남으면 그때만 문장 단위로 들어낸다 (last_resort)
                result = _validate_and_clean(_parse_draft_json(raw2), brand_settings, last_resort=True)
            except Exception:
                pass  # 재시도 파싱 실패 시 1차 결과 그대로 사용

        result.pop("needs_retry", None)
        result.pop("retry_reason", None)
        result.pop("violations", None)

        # CTA는 DB 값으로 강제 덮어씌우기 (GPT가 임의로 바꾸지 못하게)
        cta_fixed = brand_settings.get("cta", "").strip()
        if cta_fixed:
            result["cta"] = cta_fixed
            # 본문에도 같은 CTA가 들어간 경우 제거한다.
            # 발행 시 caption + hashtags + cta 를 이어붙이므로, 안 지우면 게시물에 CTA가 두 번 나온다.
            result["caption"] = _strip_duplicate_cta(result.get("caption", ""), cta_fixed)
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
    user_request: str = None,
    attached_photo_count: int = 0,
    photo_intent: str = "haircut",
    before_after_pair_ids: list = None
) -> tuple:
    """
    시스템 프롬프트 + 유저 프롬프트 구성

    attached_photo_count: 메시지에 실제 이미지가 몇 장 첨부됐는지.
      0이면 기존처럼 스타일 태그만 설명하고, 1장 이상이면 "사진을 직접 보고 써라"로 지시가 바뀐다.

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

    # [v2] photo_intent에 따른 게시물 목적 톤 분기
    if photo_intent == "shop_intro":
        intent_block = (
            "\n\n[게시물 목적 — 매장·바버 소개]\n"
            "이번 게시물은 시술 결과 홍보가 아니라 매장 분위기/바버 소개가 목적이야.\n"
            "- 시술 기술력이나 페이드 디테일을 강조하지 마\n"
            "- 매장의 분위기, 공간감, 바버의 캐릭터를 자연스럽게 전달해줘\n"
            "- CTA도 예약 강요 대신 '편하게 둘러보러 오세요' / '궁금한 건 DM 주세요' 톤으로\n"
            "- 해시태그도 #바버샵인테리어 #매장소개 같은 공간·분위기 태그 위주로"
        )
    else:
        intent_block = ""

    # [v2.1] 비포/애프터 쌍: 정확히 2장이 비포/애프터로 지정된 경우만 비교 블록 주입.
    # (사람이 사진 고를 때만 발생 — 자동 분류/AI 추론 없음. intent_block과 별도 블록.)
    if before_after_pair_ids and len(before_after_pair_ids) == 2:
        before_after_block = (
            "\n\n[게시물 형식 — 비포/애프터 비교]\n"
            "첨부된 두 사진은 같은 손님의 '시술 전(비포)'과 '시술 후(애프터)' 쌍이야.\n"
            "- 변화(before → after)를 자연스럽게 대비시켜 캡션을 써줘\n"
            "- '이렇게 달라졌어요' 식의 변화 강조가 핵심이야\n"
            "- 과장·거짓 변화 묘사는 금지, 실제 사진에서 보이는 차이만 언급"
        )
    else:
        before_after_block = ""

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
                # [다양성] 매 생성마다 같은 표현 5개를 전부 못박아 주입하면
                # "말투는 똑같고 단어만 바뀌는" 캡션이 나온다 → 일부만 무작위로 뽑아 힌트로 준다.
                picked = _sample_for_variety(signature_expr, _SIGNATURE_EXPR_SAMPLE)
                expr_str = ", ".join(f'"{e}"' for e in picked)
                lines.append(f"- 이 사장님이 쓸 법한 표현(참고만, 꼭 넣을 필요 없음): {expr_str}")

        # 구조적 필드 — 언어 무관, 항상 주입
        if sentence_length:
            lines.append(f"- 문장 길이 습관: {sentence_length}")
        # 이모지 패턴: 언어 무관(✂️💈는 만국 공통) + emoji_usage "안 씀"이면 스킵
        if emoji_pattern and emoji_usage != "안 씀":
            lines.append(f"- 이모지 습관: {emoji_pattern}")

        # 예시 캡션도 언어종속 → 일치할 때만
        if not lang_mismatch and tone_examples:
            # [다양성] 같은 예시 캡션을 매번 통째로 보여주면 LLM이 그 문장 구조에 강하게 앵커링된다.
            # → 매 생성마다 1개만 무작위로 뽑는다.
            examples_clean = []
            for ex in _sample_for_variety(tone_examples, _TONE_EXAMPLE_SAMPLE):
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
                # "최대한 동일하게"는 예시 문장 자체를 베끼게 만들어 매번 같은 캡션이 나오는 원인이 됐다.
                # → 목소리(말투)는 유지하되 문장은 새로 쓰라고 명시.
                insta_style_block += (
                    "\n⚠️ 말투와 목소리는 위와 똑같이 유지하되, 문장 구조와 표현은 매번 새로 써줘. "
                    "예시 문장을 그대로 따라 쓰거나 단어만 바꿔 끼우지 마. 광고체 절대 금지."
                )

    # 시스템 프롬프트 구성 — prompts/post_writer/system.md 에서 로드 후 치환
    # few-shot 예시(good/bad)는 examples/ 파일에서 주입
    # '#' 주석 줄은 파일 마커용이므로 프롬프트에서 제외 (예: 예시 비어있을 때)
    # [FIX 4] good_captions는 brand_tone과 매칭되는 톤 카테고리 1~2개만 선택 주입
    # (원본 brand_settings에서 다시 조회 — 위에서 brand_tone 변수는 이미 문자열로 join됨)
    good_captions = _select_good_captions(brand_settings.get("brand_tone", []))
    bad_captions  = _strip_comments(_load_prompt_file("examples/bad_captions.md"))
    good_block = f"{good_captions}\n\n" if good_captions else ""
    bad_block  = f"{bad_captions}\n\n"  if bad_captions  else ""

    system_template = _load_prompt_file("post_writer/system.md")
    system_prompt = Template(system_template).safe_substitute(
        shop_intro_line=shop_intro_line,
        intent_block=intent_block,
        before_after_block=before_after_block,
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

    # 타겟 고객 - 차별점/첫문장 슬롯과 분리된 별도 슬롯.
    # (예전엔 프론트가 이 문구를 shop_intro에 합쳐 보내서 "차별점 - 첫 문장에 녹여줘"로
    #  잘못 쓰였고, 캡션이 타겟 설명으로 시작하는 원인이 됐다.)
    # 프론트가 아직 별도 필드를 안 보내면 빈 값 → 이 블록 자체가 생략된다.
    target_customer = brand_settings.get("target_customer_text", "").strip()
    if target_customer:
        parts.append(
            f"[타겟 고객 - 이 사람들에게 말 걸듯이 써줘. 이 설명 자체를 캡션에 옮겨 적지는 마]\n"
            f"{target_customer}"
        )

    # 경쟁샵 틈새 - competitor_analysis가 뽑은 gap_opportunity.
    # 예전엔 계산만 되고 캡션 생성엔 전혀 안 쓰이던 값이었다.
    # fallback(검색 실패 시 고정 문구)은 모든 샵에 같은 문장을 주입해 획일화를 키우므로 제외.
    competitor = trend_data.get("competitor_insights") or {}
    gap = (competitor.get("gap_opportunity") or "").strip()
    if gap and competitor.get("source") != "fallback":
        parts.append(f"[경쟁샵이 놓치고 있는 틈새 - 살릴 수 있으면 이 각도로]\n{gap}")

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

    # 2-1. 실제 이미지가 첨부된 경우 — 태그가 아니라 사진 자체를 보고 쓰라고 명시.
    # (지시가 없으면 모델이 이미지를 참고만 하고 결국 태그 수준의 일반론으로 돌아간다)
    if attached_photo_count:
        parts.append(
            f"[오늘 올릴 사진 — 실제 이미지 {attached_photo_count}장이 이 메시지에 첨부돼 있어]\n"
            "위 태그 나열이 아니라 사진을 직접 보고 써줘. "
            "그 사진에만 있는 구체적인 디테일(옆라인/기장 같은 시술 결과, 표정, 조명과 분위기, 배경, 옷차림)을 "
            "한두 군데 자연스럽게 녹여서, 이 사진이 아니면 못 쓸 문장을 만들어줘.\n"
            "단, 사진에서 확인되지 않는 건 절대 지어내지 마 "
            "(손님 나이·직업·사연, 시술 시간, 사용 제품 등). 사진 설명문처럼 나열하지도 마.\n"
            "⚠️ 사진이 바버샵/남성 헤어 내용과 안 맞거나 캡션에 쓰기 어려우면, "
            "그 사진 얘기는 빼고 나머지 정보만으로 캡션을 완성해. "
            "사진이 부적절하다고 설명하거나 되묻지 말고, 어떤 경우에도 JSON 외의 문장은 출력하지 마."
        )

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

    # 4. (제거됨) 최근 게시물 말투 참고 슬롯
    #
    # 예전엔 recent_posts(= status='success'인 최근 게시물 3건)를
    # "[최근 게시물 말투 참고 (이 말투와 비슷하게)]"로 주입했다.
    # 그런데 그 게시물들은 대부분 직전에 이 파이프라인이 생성한 캡션이라,
    # 매 생성이 직전 생성을 모방하는 자기강화 루프가 됐다
    # ("말투가 매번 똑같고 단어만 바뀐다"의 직접 원인).
    # 말투 개인화는 RAG(rag_context.examples)가 이미 같은 샵 캡션으로 담당하므로
    # 이 슬롯은 순수 중복이었고, 유일한 추가 효과가 자기강화라서 제거했다.
    #
    # recent_posts 인자 자체는 호출부 4곳(orchestrator_v2 / 구 orchestrator / 테스트)이
    # 그대로 넘기고 있어 시그니처 호환을 위해 남겨둔다. 다시 프롬프트에 넣지 말 것.

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


def _strip_duplicate_cta(caption: str, cta: str) -> str:
    """캡션 본문에 CTA가 그대로 들어가 있으면 제거한다.

    발행 시 caption + hashtags + cta 로 합쳐지므로(orchestrator_v2._auto_upload_instagram,
    routers/agent._handle_upload), 본문에 남아 있으면 게시물에 CTA가 두 번 나온다.
    오탐을 피하려고 CTA 문자열이 정확히 들어 있는 경우만 건드린다.

    [변경] 예전엔 cleaned.replace(cta, "")로 CTA 부분만 도려냈다. CTA가 문장 중간에
    섞여 있으면("...스타일 고민 있으면 편하게 DM으로 예약해주세요") 앞부분이 잘린 채
    "스타일 고민 있으면 편하게" 로 남아 캡션이 문장 중간에서 끊겼다 (실측: 5조합 10건 중 6건).
    금칙어를 단어 단위로 지웠다가 문장이 깨졌던 것과 같은 패턴이라, 같은 해법을 쓴다
    → CTA가 든 줄은 _strip_violating_sentences()로 그 **문장**을 통째로 들어낸다.
    (캡션 전체에 걸지 않고 해당 줄에만 적용하는 이유: 그 함수는 빈 줄을 버리므로
     캡션 전체에 걸면 문단 구분이 사라진다.)
    """
    if not caption or not cta:
        return caption

    norm = lambda s: re.sub(r'\s+', ' ', s).strip()
    cta_norm = norm(cta)

    # CTA 자체가 여러 문장이면 어느 한 문장도 CTA 전체를 담지 못해 문장 단위 비교에 걸리지 않는다.
    # 이럴 때만 CTA를 문장 조각으로도 함께 넘겨서 각 조각이 든 문장을 제거한다.
    cta_parts = [p for p in re.split(r'(?<=[.!?…])\s+', cta) if p.strip()]
    banned = [cta] + (cta_parts if len(cta_parts) > 1 else [])

    kept = []
    for line in caption.split("\n"):
        if norm(line) == cta_norm:
            continue                # 줄 전체가 CTA → 줄째 제거 (기존 동작 유지)
        if cta in line:
            line = _strip_violating_sentences(line, banned)
            if not line.strip():
                continue            # 그 줄이 CTA 문장뿐이었으면 줄째 제거
        kept.append(line)

    cleaned = "\n".join(kept)

    # 문장 단위로 지웠더니 캡션이 통째로 비는 극단적인 경우만 최후 수단으로 부분 제거.
    # (빈 캡션보다는 잘린 문장이라도 남기는 편이 낫다 — _validate_and_clean의 처리와 동일한 안전망)
    if not cleaned.strip() and caption.strip():
        print("[post_writer] CTA 문장 제거 시 캡션이 비어 부분 제거로 대체")
        cleaned = caption.replace(cta, "")

    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    if cleaned != caption.strip():
        print("[post_writer] 본문에 중복된 CTA 제거")
    return cleaned


def _finalize_hashtags(generated: list, required: list, brand_settings: dict) -> list:
    """생성 해시태그 + 필수 해시태그를 합치고 feed_style.hashtag_count에 맞춘다.

    [변경] 예전엔 필수 태그를 무조건 뒤에 덧붙이기만 해서, 온보딩에서 고른
    hashtag_count(예: 13)를 항상 넘겼다 (실측: 13 설정에 결과 15~17개).
    → 필수 태그는 반드시 유지하고, 초과분은 생성 태그 쪽에서 잘라낸다.
      필수 태그만으로 이미 상한을 넘으면 상한을 못 지킨다는 사실을 로그로 남긴다.

    순서: 주제 태그(생성분) 먼저 → 고정 태그(필수) 뒤.
          인스타는 앞쪽 태그가 먼저 노출되므로 그날 내용과 맞는 태그를 앞에 둔다.
    """
    feed_style = brand_settings.get("feed_style", {}) or {}
    try:
        limit = int(feed_style.get("hashtag_count", 10))
    except (TypeError, ValueError):
        limit = 10

    req = [t for t in required if t]
    gen = [t for t in generated if t and t not in req]

    if limit <= 0:
        return gen + req

    if len(req) >= limit:
        if len(req) > limit:
            print(f"[post_writer] 필수 해시태그 {len(req)}개가 설정값({limit}개)을 초과 "
                  f"→ 필수 태그 유지, 생성 태그 제외")
        return req

    room = limit - len(req)
    if len(gen) > room:
        print(f"[post_writer] 해시태그 상한({limit}개) 초과 → 생성 태그 {len(gen)}개 중 {room}개만 사용")
    return gen[:room] + req


def _shop_allowed_terms(brand_settings: dict) -> str:
    """이 샵에서는 정당하게 쓸 수 있는 단어들을 한 덩어리 문자열로 모은다.

    _FORBIDDEN_TOPICS는 "바버샵과 무관한 주제"를 막으려는 목록인데,
    샵에 따라 그 단어가 오히려 핵심 정보인 경우가 있다.
    (실제 사례: "펌 시술 하지 않습니다"가 간판인 정통 바버샵 → '펌'이 정당한 단어)
    shop_intro / exclude_conditions / preferred_styles에 이미 등장하는 단어는
    이 샵의 맥락에서 정당하다고 보고 위반으로 잡지 않는다.
    """
    def flat(v):
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
        return str(v or "")

    return " ".join([
        flat(brand_settings.get("shop_intro")),
        flat(brand_settings.get("exclude_conditions")),
        flat(brand_settings.get("preferred_styles")),
        flat(brand_settings.get("target_customer_text")),
    ])


def _strip_violating_sentences(caption: str, banned: list) -> str:
    """최후 수단: 금지 단어가 든 '문장'을 통째로 제거한다.

    예전엔 caption.replace(word, "")로 단어만 지워서
    "펌 없이 컷·드라이만으로" → " 없이 컷·드라이만으로" 처럼 문장이 깨졌다.
    단어가 아니라 문장 단위로 들어내면 최소한 말이 되는 글은 남는다.
    """
    kept_lines = []
    for line in caption.split("\n"):
        sentences = re.split(r'(?<=[.!?…])\s+', line)
        kept = [s for s in sentences if not any(w and w in s for w in banned)]
        joined = " ".join(s for s in kept if s.strip()).strip()
        if joined:
            kept_lines.append(joined)
    return "\n".join(kept_lines).strip()


# [검증] 금칙어 + 할루시네이션
def _validate_and_clean(result: dict, brand_settings: dict, last_resort: bool = False) -> dict:
    """
    AG-042: 금칙어 + 주제 이탈 + 과장 표현 + 할루시네이션 검사.

    [변경] 예전엔 위반 단어를 caption.replace(word, "")로 삭제했는데,
    이게 문장을 파괴했다 (실측: 4회 생성 중 3회에서 "펌 없이 …" → " 없이 …").
    → 할루시네이션과 동일하게 **재생성 플래그**로 바꿨다.
      재시도 후에도 위반이 남으면(last_resort=True) 그때만 문장 단위로 들어낸다.

    shop_intro 등 이 샵 설정에 이미 있는 단어는 오탐으로 보고 통과시킨다.
    """
    forbidden_words = brand_settings.get("forbidden_words", [])
    if isinstance(forbidden_words, str):
        forbidden_words = [w.strip() for w in forbidden_words.split(",")]
    forbidden_words = [w for w in forbidden_words if w]

    shop_intro = brand_settings.get("shop_intro", "").strip()
    allowed_terms = _shop_allowed_terms(brand_settings)

    caption = result.get("caption", "")

    # 0) 할루시네이션 패턴 → 재생성 신호
    hallucination_patterns = [
        (r'\d+년\s*경력',   "경력 연수 할루시네이션"),
        (r'\d+자리\s*남',   "예약 현황 할루시네이션"),
        (r'마감\s*임박',     "마감 임박 할루시네이션"),
        (r'오늘만\s*할인',   "근거없는 할인 할루시네이션"),
    ]
    if not last_resort:
        for pattern, label in hallucination_patterns:
            match = re.search(pattern, caption)
            if match:
                matched_text = match.group(0)
                if shop_intro and matched_text in shop_intro:
                    print(f"[post_writer] '{matched_text}' → shop_intro에 명시된 사실, 통과")
                    continue
                print(f"[post_writer] ⚠️  {label} 감지 → needs_retry=True")
                result["needs_retry"] = True
                result["retry_reason"] = label
                result["violations"] = []
                return result

    # 1) 위반 단어 수집 (여기서 지우지 않는다)
    violations = []   # (분류, 단어)
    for word in forbidden_words:
        if word in caption:
            violations.append(("금칙어", word))
    for word in _FORBIDDEN_TOPICS:
        if word in caption and word not in allowed_terms:
            violations.append(("주제 이탈", word))
    for word in _FORBIDDEN_EXAGGERATIONS:
        if word in caption:
            violations.append(("과장 표현", word))

    if violations:
        words = [w for _, w in violations]
        summary = ", ".join(f"{kind}:'{w}'" for kind, w in violations)
        if not last_resort:
            print(f"[post_writer] ⚠️  위반 감지 ({summary}) → needs_retry=True")
            result["needs_retry"] = True
            result["retry_reason"] = summary
            result["violations"] = words
            return result
        # 재시도 후에도 남았을 때만 문장 단위 제거
        cleaned = _strip_violating_sentences(caption, words)
        if cleaned:
            print(f"[post_writer] 재시도 후에도 위반 잔존 ({summary}) → 해당 문장 제거")
            caption = cleaned
        else:
            # 문장을 다 들어내면 캡션이 비어버리는 경우 — 단어 제거로 최소 복구
            print(f"[post_writer] 위반 문장 제거 시 캡션이 비어 단어 단위로 대체 제거 ({summary})")
            for w in words:
                caption = caption.replace(w, "")
            caption = re.sub(r'[ \t]{2,}', ' ', caption)

    result["caption"] = caption.strip()

    # 해시태그: 태그 단위 제거는 문법을 깨지 않으므로 그대로 필터링한다.
    # 단, 이 샵 맥락에서 정당한 주제어는 제외하지 않는다.
    banned_in_tags = forbidden_words + [w for w in _FORBIDDEN_TOPICS if w not in allowed_terms]
    hashtags = result.get("hashtags", [])
    hashtags = [tag for tag in hashtags if not any(word in tag for word in banned_in_tags)]

    # 필수 해시태그: hashtag_style 에서 #로 시작하는 항목 추출
    hashtag_style = brand_settings.get("hashtag_style", "")
    if isinstance(hashtag_style, list):
        hashtag_style = ", ".join(hashtag_style)
    required = []
    for tag in re.findall(r'#[^\s,]+', hashtag_style):
        tag = tag.strip()
        normalized = tag if tag.startswith("#") else f"#{tag}"
        if normalized not in required:
            required.append(normalized)

    result["hashtags"] = _finalize_hashtags(hashtags, required, brand_settings)
    print(f"[post_writer] 최종 hashtags({len(result['hashtags'])}개): {result['hashtags']}")
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
    # Claude (Azure Foundry) - AAD(Entra) 토큰 인증. /anthropic 경로는 api-key 미지원.
    # 모델 식별자는 get_claude_model() — CLAUDE_MODEL_NAME 환경변수로 바꾼다.
    return anthropic.AsyncAnthropic(
        base_url=CLAUDE_BASE_URL,
        auth_token=get_claude_token(),
        timeout=anthropic.Timeout(30.0)
    )