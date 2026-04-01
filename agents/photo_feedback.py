"""
photo_select.py에 추가할 사진 피드백 학습 코드

원장님이 사진을 탈락시킬 때 이유를 학습해서
다음 선택 기준에 누적 반영함.

CosmosDB에 rejection_log 컨테이너 필요:
{
  "id": "shop_id:photo_id",
  "shop_id": "shop_123",
  "photo_id": "photo_abc",
  "reason": "배경이 지저분해서",
  "weak_dimension": "background",   # GPT가 분류
  "rejected_at": "2026-04-01T..."
}
"""

import os
import json
from datetime import datetime, timezone, timedelta
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory


KST = timezone(timedelta(hours=9))

# 평가 차원 (photo_filter.py의 stage2 기준과 동일)
SCORE_DIMENSIONS = ["gradient", "lighting", "background", "model_vibe", "sharpness"]


async def learn_from_rejection(shop_id: str, photo_id: str, reason: str) -> dict:
    """
    원장님이 사진을 탈락시킨 이유를 학습.

    라우터에서 호출:
    POST /api/photos/{photo_id}/reject
    { "reason": "배경이 지저분해서" }

    Returns:
        { "weak_dimension": "background", "saved": True }
    """
    print(f"[photo_feedback] 탈락 학습 → photo_id={photo_id}, reason={reason[:30]}")

    # 1. GPT로 어느 평가 차원 문제인지 분류
    weak_dimension = await _classify_rejection_reason(reason)

    # 2. CosmosDB에 저장
    saved = await _save_rejection_log(shop_id, photo_id, reason, weak_dimension)

    print(f"[photo_feedback] 분류 완료 → weak_dimension={weak_dimension}")
    return {"weak_dimension": weak_dimension, "saved": saved}


async def get_shop_weakness_profile(shop_id: str) -> dict:
    """
    이 샵에서 누적된 탈락 이유를 분석해서
    photo_select_agent()가 사진 고를 때 반영할 가중치 반환.

    Returns:
        {
            "penalize": {"background": 0.3, "lighting": 0.1},  # 이 차원 낮으면 감점
            "total_rejections": 12,
            "top_weakness": "background"
        }
    """
    logs = await _load_rejection_logs(shop_id)

    if not logs:
        return {"penalize": {}, "total_rejections": 0, "top_weakness": None}

    # 차원별 탈락 횟수 집계
    dimension_count = {}
    for log in logs:
        dim = log.get("weak_dimension")
        if dim and dim in SCORE_DIMENSIONS:
            dimension_count[dim] = dimension_count.get(dim, 0) + 1

    total = len(logs)
    if not dimension_count:
        return {"penalize": {}, "total_rejections": total, "top_weakness": None}

    # 탈락 비율 → 감점 가중치 (최대 0.5)
    penalize = {
        dim: min(0.5, round(count / total, 2))
        for dim, count in dimension_count.items()
        if count / total > 0.15  # 15% 이상 탈락된 차원만 반영
    }

    top_weakness = max(dimension_count, key=dimension_count.get)

    print(f"[photo_feedback] 약점 프로파일 → top={top_weakness}, penalize={penalize}")
    return {
        "penalize":         penalize,
        "total_rejections": total,
        "top_weakness":     top_weakness
    }


async def apply_weakness_to_selection(
    photos: list,
    weakness_profile: dict
) -> list:
    """
    weakness_profile 기반으로 사진 점수에 패널티 적용.
    photo_select_agent()의 _categorize_by_angle() 이후에 호출.

    penalize = {"background": 0.3} 이면
    → background 점수가 2 이하인 사진은 _sort_score에서 -0.3 감점
    """
    penalize = weakness_profile.get("penalize", {})
    if not penalize:
        return photos

    adjusted = []
    for photo in photos:
        scores = photo.get("scores", {})
        penalty = 0.0

        for dim, weight in penalize.items():
            dim_score = scores.get(dim, 3)
            if dim_score <= 2:  # 약점 차원에서 낮은 점수 → 감점
                penalty += weight

        # _sort_score 조정
        original = photo.get("_sort_score", 0)
        photo["_sort_score"] = max(0.0, original - penalty)
        if penalty > 0:
            print(f"[photo_feedback] 패널티 적용 → {photo.get('id','?')} -{penalty:.2f}")

        adjusted.append(photo)

    return adjusted


# ──────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────

async def _classify_rejection_reason(reason: str) -> str:
    """
    탈락 이유 텍스트 → 평가 차원 분류

    예:
    "배경이 지저분해서" → "background"
    "사진이 흔들려" → "sharpness"
    "너무 어두워" → "lighting"
    """
    try:
        kernel = _init_kernel()
        chat_history = ChatHistory()
        chat_history.add_user_message(
            f"""바버샵 사진 탈락 이유를 아래 5개 차원 중 하나로 분류해줘.

차원:
- gradient: 페이드 그라데이션 (경계 뭉침, 자연스럽지 않음)
- lighting: 조명 (너무 어두움, 과노출)
- background: 배경 (지저분함, 복잡함)
- model_vibe: 모델 표정/분위기 (어색함, 홍보용 부적합)
- sharpness: 선명도/구도 (흔들림, 구도 불량)

탈락 이유: "{reason}"

차원 이름 하나만 반환. 예: background"""
        )

        chat_service = kernel.get_service("azure_openai")
        response = await chat_service.get_chat_message_content(
            chat_history=chat_history,
            settings=chat_service.instantiate_prompt_execution_settings()
        )

        result = str(response).strip().lower()
        if result in SCORE_DIMENSIONS:
            return result

        # 키워드 매칭 fallback
        reason_lower = reason.lower()
        if any(w in reason_lower for w in ["배경", "background", "지저분"]):
            return "background"
        if any(w in reason_lower for w in ["어두", "밝기", "lighting", "조명"]):
            return "lighting"
        if any(w in reason_lower for w in ["흔들", "blur", "선명", "구도", "sharpness"]):
            return "sharpness"
        if any(w in reason_lower for w in ["페이드", "그라데이션", "gradient"]):
            return "gradient"
        if any(w in reason_lower for w in ["표정", "분위기", "어색", "model"]):
            return "model_vibe"

        return "sharpness"  # 기본값

    except Exception as e:
        print(f"[photo_feedback] 차원 분류 실패 ({e}) → sharpness 기본값")
        return "sharpness"


async def _save_rejection_log(
    shop_id: str, photo_id: str, reason: str, weak_dimension: str
) -> bool:
    try:
        from services.cosmos_db import save_rejection_log
        save_rejection_log(shop_id, {
            "id":             f"{shop_id}:{photo_id}",
            "shop_id":        shop_id,
            "photo_id":       photo_id,
            "reason":         reason,
            "weak_dimension": weak_dimension,
            "rejected_at":    datetime.now(KST).isoformat()
        })
        return True
    except Exception as e:
        print(f"[photo_feedback] 탈락 로그 저장 실패 ({e})")
        return False


async def _load_rejection_logs(shop_id: str) -> list:
    try:
        from services.cosmos_db import get_rejection_logs
        return get_rejection_logs(shop_id, limit=50)
    except Exception as e:
        print(f"[photo_feedback] 탈락 로그 로드 실패 ({e})")
        return []


def _init_kernel() -> Kernel:
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT_MINI") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        service_id="azure_openai",
        deployment_name=deployment,
        endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    ))
    return kernel
