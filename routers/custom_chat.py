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
    
    # 3. 시스템 프롬프트 (안전장치 포함)
    system_prompt = """너는 경력 20년의 바버샵(Barbershop) 전문 인스타그램 마케터야.

[역할]
- 사장님이 인스타그램 게시물 아이디어를 얻을 수 있게 도와줘
- 모든 대화는 '헤어 스타일링', '이용(Barbering)', '남성 그루밍'에 관한 것
- 친근하고 전문적인 톤으로 대화해

[중요 규칙]
- "컷(Cut)"은 오직 '머리카락을 자르는 시술'의 의미로만 사용
- 여성 헤어, 펌, 염색 관련 내용은 제외
- 과장되거나 거짓된 표현 금지
- 구체적인 스타일명 포함 (페이드컷, 사이드파트, 슬릭백 등)

친절하게 답변해줘!"""
    
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