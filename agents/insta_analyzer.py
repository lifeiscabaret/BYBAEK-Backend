"""
Instagram 과거 게시물 자동 분석 에이전트

역할: 사장님 인스타 계정 연동 완료 시 과거 게시물을 수집하고
      GPT-4.1-mini로 말투/이모지/해시태그 패턴을 분석하여 DB에 저장

입력: shop_id (Shop 컨테이너에서 insta_user_id, insta_access_token 조회)
출력: insta_style_profile 딕셔너리 → Shop DB에 자동 저장

흐름:
  1. Shop DB에서 인스타 인증 정보 조회
  2. Instagram Graph API로 과거 게시물 최대 50개 수집
  3. GPT-4.1-mini로 말투/패턴 분석
  4. 분석 결과를 Shop DB의 insta_style_profile 필드에 저장
"""

import os
import json
import httpx
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory
from services.cosmos_db import get_auth, save_auth


async def analyze_instagram_history(shop_id: str) -> dict:
    """
    인스타 과거 게시물 분석 메인 함수

    Returns:
        {
            "tone_examples": ["실제 캡션 예시 3개"],
            "emoji_pattern": "자주 쓰는 이모지 패턴",
            "hashtag_style": "해시태그 스타일 분석",
            "caption_length": "short/medium/long",
            "best_performing": ["좋아요 많은 캡션 3개"],
            "tone_description": "말투 특징 2~3문장 요약"
        }
    """
    print(f"[insta_analyzer] 시작 → shop_id={shop_id}")

    try:
        shop_data = get_auth(shop_id)
        if not shop_data:
            print(f"[insta_analyzer] Shop 데이터 없음 → 종료")
            return {}

        access_token = shop_data.get("insta_access_token")
        user_id = shop_data.get("insta_user_id")

        if not access_token or not user_id:
            print(f"[insta_analyzer] 인스타 인증 정보 없음 → 종료")
            return {}

        posts = await _fetch_instagram_posts(user_id, access_token)
        if not posts:
            print(f"[insta_analyzer] 게시물 0개 수집 → 종료")
            return {}

        print(f"[insta_analyzer] 게시물 {len(posts)}개 수집 완료")

        result = await _analyze_with_gpt(posts)
        if not result:
            print(f"[insta_analyzer] GPT 분석 실패 → 종료")
            return {}

        save_auth(shop_id, {"insta_style_profile": result})
        print(f"[insta_analyzer] 분석 완료 → shop_id={shop_id}, 게시물 {len(posts)}개 수집")
        return result

    except Exception as e:
        print(f"[insta_analyzer] 에러 발생 ({e}) → 서비스 영향 없이 종료")
        return {}


async def _fetch_instagram_posts(user_id: str, access_token: str) -> list:
    """Instagram Graph API로 과거 게시물 최대 50개 수집"""
    url = f"https://graph.instagram.com/v25.0/{user_id}/media"
    params = {
        "fields": "caption,timestamp,like_count",
        "limit": 50,
        "access_token": access_token
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)

            if resp.status_code != 200:
                print(f"[insta_analyzer] Instagram API 실패 (status={resp.status_code})")
                return []

            data = resp.json()
            posts = data.get("data", [])

            # like_count 없는 경우(개인 계정) 무시하고 진행
            return [p for p in posts if p.get("caption")]

    except Exception as e:
        print(f"[insta_analyzer] Instagram API 호출 에러: {e}")
        return []


async def _analyze_with_gpt(posts: list) -> dict:
    """GPT-4.1-mini로 캡션 분석"""
    kernel = _init_kernel()

    # 캡션 목록 구성 (like_count 있으면 포함)
    caption_lines = []
    for i, post in enumerate(posts, 1):
        caption = post.get("caption", "").strip()
        like_count = post.get("like_count")
        if like_count is not None:
            caption_lines.append(f"{i}. [좋아요 {like_count}] {caption}")
        else:
            caption_lines.append(f"{i}. {caption}")

    captions_text = "\n".join(caption_lines)

    system_prompt = """너는 인스타그램 계정 분석 전문가야.
사장님의 과거 게시물 캡션들을 보고 말투/스타일 패턴을 분석해줘.

반드시 아래 JSON 형식으로만 답해. 다른 텍스트 없이.

{
  "tone_examples": ["실제 캡션 중 이 사장님 말투를 가장 잘 보여주는 예시 3개 (원문 그대로)"],
  "emoji_pattern": "자주 쓰는 이모지와 사용 패턴 설명",
  "hashtag_style": "해시태그 스타일 분석 (개수, 한글/영문 비율, 지역명 포함 여부 등)",
  "caption_length": "short/medium/long 중 하나",
  "best_performing": ["좋아요 많은 상위 3개 캡션 (원문 그대로, 좋아요 데이터 없으면 퀄리티 높은 3개)"],
  "tone_description": "이 사장님 말투의 핵심 특징을 2~3문장으로 요약. 반말/존댓말, 문장 끝 습관, 특유의 표현 등."
}"""

    user_prompt = f"""아래는 이 사장님의 인스타그램 과거 게시물 캡션 {len(posts)}개야.
분석해서 말투/패턴 프로필을 만들어줘.

[게시물 캡션 목록]
{captions_text}"""

    chat_history = ChatHistory()
    chat_history.add_system_message(system_prompt)
    chat_history.add_user_message(user_prompt)

    try:
        chat_service = kernel.get_service("azure_openai")
        settings = chat_service.instantiate_prompt_execution_settings()
        settings.temperature = 0.3
        settings.max_tokens = 800

        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=settings
        )

        raw = str(response).strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)

    except Exception as e:
        print(f"[insta_analyzer] GPT 분석 에러: {e}")
        return {}


def _init_kernel() -> Kernel:
    """Semantic Kernel 초기화 (GPT-4.1-mini)"""
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI")
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel
