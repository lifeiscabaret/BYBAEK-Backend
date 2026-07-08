"""
Instagram 과거 게시물 자동 분석 에이전트

역할: 사장님 인스타 계정 연동 완료 시 과거 게시물을 수집하고
      Claude(Sonnet 4.6)로 말투/이모지/해시태그 패턴을 분석하여 DB에 저장

입력: shop_id (Shop 컨테이너에서 insta_user_id, insta_access_token 조회)
출력: insta_style_profile 딕셔너리 → Shop DB에 자동 저장

흐름:
  1. Shop DB에서 인스타 인증 정보 조회
  2. Instagram Graph API로 과거 게시물 최대 50개 수집
  3. Claude(Sonnet 4.6)로 말투/패턴 분석
  4. 분석 결과를 Shop DB의 insta_style_profile 필드에 저장
"""

import json
import asyncio
import httpx
import anthropic
from services.cosmos_db import get_auth, save_auth
from utils.claude_auth import CLAUDE_BASE_URL, get_claude_token


async def analyze_instagram_history(shop_id: str) -> dict:
    """
    인스타 과거 게시물 분석 메인 함수

    Returns:
        {
            "tone_description": "말투 핵심 2~3문장",
            "sentence_ending": "자주 쓰는 종결어미",
            "signature_expressions": ["특유의 표현 3~5개"],
            "sentence_length": "short/medium/long",
            "emoji_pattern": "이모지 사용 패턴 (안 쓰면 '없음')",
            "hashtag_style": "해시태그 스타일 분석",
            "tone_examples": ["말투 잘 보여주는 캡션 2개"],
            "detected_language": "게시물 주 언어 (ko/en/ja 등)"
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

        # 말투 분석과 RAG 백필을 동시 실행 (둘 다 완료 보장, 백필은 자체 예외격리라 분석 실패와 무관)
        result, _ = await asyncio.gather(
            _analyze_with_gpt(posts),
            _backfill_rag_index(shop_id, posts),
        )
        if not result:
            print(f"[insta_analyzer] Claude 분석 실패 → 종료")
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
        "fields": "id,caption,timestamp,like_count",   # ← id 추가 (멱등 재실행용)
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
    """Claude(Sonnet 4.6)로 캡션 말투/스타일 패턴 분석 (NOVA식 구조 분석)"""
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
사장님의 과거 게시물 캡션들을 보고 말투/스타일 패턴을 구조적으로 분석해줘.

원문 캡션을 통째로 복사하지 말고, 말투의 '특징'을 서술하는 데 집중해.
반드시 아래 JSON 형식으로만 답해. 다른 텍스트 없이.

{
  "tone_description": "말투 핵심 2~3문장 (반말/존댓말, 문장 끝 습관)",
  "sentence_ending": "자주 쓰는 종결어미 (예: ~거든요, ~네요, ~습니다)",
  "signature_expressions": ["이 사장님 특유의 표현 3~5개 (짧은 구절)"],
  "sentence_length": "short/medium/long 중 하나",
  "emoji_pattern": "이모지 사용 패턴 (안 쓰면 '없음')",
  "hashtag_style": "해시태그 스타일 (개수, 한/영, 지역명)",
  "tone_examples": ["말투 가장 잘 보여주는 캡션 2개만 (원문)"],
  "detected_language": "게시물들의 주 언어 (ko/en/ja 등, 한영 병기면 주 언어)"
}"""

    user_prompt = f"""아래는 이 사장님의 인스타그램 과거 게시물 캡션 {len(posts)}개야.
분석해서 말투/패턴 프로필을 만들어줘.

[게시물 캡션 목록]
{captions_text}"""

    client = anthropic.AsyncAnthropic(
        base_url=CLAUDE_BASE_URL,
        auth_token=get_claude_token(),
        timeout=anthropic.Timeout(30.0)
    )

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,   # 800 → 1500 (잘림 방지)
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(raw)

    except json.JSONDecodeError as e:
        print(f"[insta_analyzer] JSON 파싱 실패: {e} | raw 앞 200자: {raw[:200]}")
        return {}
    except Exception as e:
        print(f"[insta_analyzer] Claude 분석 에러: {e}")
        return {}


async def _backfill_rag_index(shop_id: str, posts: list) -> None:
    """과거 게시물 캡션을 caption_body 타입으로 1회 일괄 인덱싱 (신규 샵 cold start 부트스트랩).
    절대 예외를 위로 던지지 않음."""
    from agents.rag_tool import get_embedding
    from services.vector_db import save_embeddings_batch
    try:
        docs = []
        for p in posts:
            cap   = (p.get("caption") or "").strip()
            ig_id = p.get("id")
            if not cap or not ig_id:
                continue
            vec = await get_embedding(cap)   # TODO: 추후 배치 임베딩으로 50콜→1콜 최적화 가능
            if not vec:
                continue
            docs.append({
                "id": f"coldstart_{ig_id}_caption_body",
                "shop_id": shop_id,
                "caption": cap,
                "caption_vector": vec,
                "content_type": "caption_body",
            })
        if docs:
            save_embeddings_batch(docs)
            print(f"[insta_analyzer] 백필 인덱싱 → {len(docs)}개")
    except Exception as e:
        print(f"[insta_analyzer] 백필 실패 (무시): {e}")
