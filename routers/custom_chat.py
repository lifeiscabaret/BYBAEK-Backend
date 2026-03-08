"""
수동 채팅 라우터
- POST /api/custom_chat/manual_chat: 사장님이 GPT와 실시간 대화 (스트리밍)

용도:
- 자동 파이프라인 외에 수동으로 게시물 아이디어 얻기
- ChatGPT처럼 글자가 실시간으로 나타나는 UX 제공
- 즉석에서 "오늘 페이드컷으로 뭐라고 홍보해?" 같은 질문 가능
"""

import os
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List
from openai import AsyncAzureOpenAI

router = APIRouter()

class ManualChatRequest(BaseModel):
    shop_id: str
    message: str
    photo_ids: List[str] = []
    
    class Config:
        json_schema_extra = {
            "example": {
                "shop_id": "3sesac18",
                "message": "오늘 페이드컷으로 인스타그램 홍보 문구 만들어줘",
                "photo_ids": []
            }
        }


async def generate_chat_stream(shop_id: str, message: str, photo_ids: List[str]):
    """
    GPT와 실시간 스트리밍 대화
    
    ChatGPT처럼 글자가 타닥타닥 나타나는 효과
    """
    # 1. 환경변수 검증
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_KEY")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_FULL") or \
                 os.getenv("AZURE_OPENAI_DEPLOYMENT")
    
    if not endpoint or not api_key:
        yield "[❌ 오류: Azure OpenAI 설정이 없습니다. 관리자에게 문의하세요.]"
        return
    
    if not deployment:
        yield "[❌ 오류: 모델 배포 이름이 설정되지 않았습니다.]"
        return
    
    # 디버깅 로그 (배포 전 확인용)
    print("=" * 50)
    print(f"[custom_chat] 엔드포인트: {endpoint}")
    print(f"[custom_chat] 배포 이름: {deployment}")
    print(f"[custom_chat] API 키 길이: {len(api_key)}")
    print(f"[custom_chat] shop_id: {shop_id}")
    print(f"[custom_chat] 메시지: {message[:50]}...")
    print("=" * 50)
    
    # 2. Azure OpenAI 클라이언트 초기화
    client = AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    )
    
    # 3. 시스템 프롬프트 (전문 마케터 페르소나)
    system_prompt = """너는 남성 바버샵 전문 마케팅 디렉터야.
10년간 300개 이상 바버샵의 인스타그램 문의율을 평균 237% 증가시킨 전문가.

[대화 목표]
사장님이 "아, 이렇게 하면 문의가 들어오겠구나" 깨달음을 얻게 도와주기.
단순 예쁜 문구 제안이 아니라, 왜 그 전략이 효과적인지 근거와 함께 설명.

[전문 마케터 사고방식]

1. 키워드 전략
   - "메인 키워드는 첫 문장에 넣어야 검색 노출 올라가요"
   - 검색량 높은 키워드: 페이드/투블럭/남자머리/바버샵/사이드파트
   - 금지 키워드: cut/컷/자르다 (Azure 필터 차단됨)

2. 타겟팅 전략
   - "20-40대 남성 직장인이 찾는 키워드는..."
   - "대학생은 '데일리룩' 해시태그 반응 좋아요"
   - 고객 니즈: 깔끔함/시간절약/전문성/트렌디함

3. 문의 유도 전략 (핵심!)
   - 수동적 X: "예약 문의주세요"
   - 능동적 O: "지금 DM 주시면 이번 주 예약 가능"
   - 긴박감 + 행동 유도 = 문의 폭발

4. 해시태그 전략
   - 대분류(바버샵/남자머리) + 중분류(스타일명) + 소분류(지역/상황)
   - 검색량 높은 순으로 앞에 배치
   - 총 10개 내외 권장

5. 성과 지표 중심 사고
   - "이 문구는 클릭률이 30% 더 높아요"
   - "이 해시태그 조합이 노출 2배 올려줘요"
   - 근거 있는 조언

[금지 사항]
- 여성 헤어/펌/염색 언급
- 과장/거짓 표현
- "cut/컷/자르다" 단어

친절하고 구체적으로 답변해줘. 
"왜 그런지" 근거와 함께 설명하면 사장님이 배우고 성장할 수 있어."""
    
    try:
        # 4. 스트리밍 요청
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            stream=True,  # 핵심: 스트리밍 켜기
            temperature=0.7,
            max_tokens=800
        )
        
        # 5. 글자가 조각(chunk)으로 도착할 때마다 프론트로 전송
        async for chunk in response:
            if len(chunk.choices) > 0:
                content = chunk.choices[0].delta.content
                if content:
                    yield content
                    
    except Exception as e:
        # 에러 로그 (서버)
        print(f"[custom_chat] ❌ 오류 발생: {e}")
        
        # 사용자 친화적 메시지 (프론트)
        yield "\n\n[죄송합니다. 일시적인 오류가 발생했습니다. 다시 시도해주세요.]"


@router.post("/manual_chat")
async def manual_chat_agent(req: ManualChatRequest):
    """
    사장님이 GPT와 실시간 대화
    
    Returns:
        StreamingResponse (SSE): 글자가 실시간으로 나타남
    
    Usage:
        프론트엔드에서 EventSource 또는 fetch로 받기
        
        ```javascript
        const response = await fetch('/api/custom_chat/manual_chat', {
            method: 'POST',
            body: JSON.stringify({shop_id: "3sesac18", message: "안녕"})
        });
        
        const reader = response.body.getReader();
        while (true) {
            const {done, value} = await reader.read();
            if (done) break;
            console.log(new TextDecoder().decode(value));
        }
        ```
    """
    return StreamingResponse(
        generate_chat_stream(req.shop_id, req.message, req.photo_ids),
        media_type="text/event-stream"
    )