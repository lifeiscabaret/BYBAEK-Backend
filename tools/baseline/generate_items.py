"""
블라인드 베이스라인 — 채점 대상 캡션 생성

목적:
  "캡션 퀄리티가 좋아졌는가"를 caption_score(AI 자기평가)로 판단하면
  이 프로젝트가 지적해 온 문제를 그대로 반복하게 된다.
  인스타 실참여는 당분간 측정 불가(운영 계정 팔로워 1명, 게시물 200개에 좋아요 총 6)라,
  당분간은 **사람 블라인드 채점**이 유일한 측정 수단이다.

설계:
  같은 시나리오(사진 스타일 + 트렌드 + 브랜드 설정)에 대해 RAG 조건만 바꿔 생성하는
  짝지어 비교(paired design). 시나리오 난이도가 교란 요인이 되지 않는다.

  ai_rag    — RAG에 AI 생성분이 쌓인 상태 (현재 프로덕션이 도달한 상태의 재현)
  human_rag — 사장님이 직접 쓴 캡션이 RAG에 있는 상태
  cold      — RAG 없음 (범용 few-shot만)
  anchor    — 실제 사람이 쓴 캡션 (절대 기준선, 생성 아님)

  ※ anchor는 완전한 블라인드가 되지 않는다. 실제 계정 게시물은 고정 정보블록 구조라
    형식만으로 구분되기 때문에, 인트로 부분만 떼어내 길이를 맞췄어도 티가 날 수 있다.
    다만 핵심 비교(ai_rag vs human_rag vs cold)는 형식이 동일해 블라인드가 유지되므로
    실험의 주 결론에는 영향이 없다. anchor는 "사람 글은 몇 점대인가"의 참조점으로만 읽을 것.

실행:
  python tools/baseline/generate_items.py
  → out/items.json (채점지용, 조건 라벨 없음)
  → out/key.json   (정답표, 채점자에게 주지 말 것)
"""

import asyncio
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seed_us_barbershop import POSTS, BRAND_SETTINGS, INFO_BLOCK  # noqa: E402
from agents.post_writer import post_writer_agent                   # noqa: E402
from agents.rag_tool import _compress_context                      # noqa: E402
from services.vector_db import _container as ragc                  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

# AI 생성분 RAG를 만들 때 참고할 운영 샵 (현재 RagVectors가 전부 authored_by='ai')
AI_RAG_SHOP = "00000000-0000-0000-0718-3a306722d45c"

# 실제 계정 게시물 유형에서 뽑은 8개 시나리오
SCENARIOS = [
    {"key": "skinfade",   "photos": ["스킨페이드", "측면", "클로즈업"],
     "trend": "스킨페이드가 20-40대 남성 사이에서 꾸준히 인기",
     "weather": "맑음, 29도, 늦여름", "promo": "늦여름 짧고 깔끔하게 정리하기 좋은 시기"},
    {"key": "sidepart",   "photos": ["사이드파트", "포마드", "정면"],
     "trend": "사이드파트에 포마드 스타일링이 클래식 수요로 이어짐",
     "weather": "흐림, 26도", "promo": "격식 있는 자리 앞두고 다듬기 좋은 스타일"},
    {"key": "father_son", "photos": ["페이드컷", "부자", "투샷"],
     "trend": "가족 단위 방문이 늘어나는 추세",
     "weather": "맑음, 28도, 주말", "promo": "아버지와 아들이 함께 오는 주말"},
    {"key": "team",       "photos": ["단체샷", "매장내부", "팀"],
     "trend": "바버샵 팀 소개 콘텐츠가 신뢰도에 영향",
     "weather": "맑음, 27도", "promo": "매장 분위기와 팀을 보여주는 날"},
    {"key": "rainy",      "photos": ["스킨페이드", "측면"],
     "trend": "습한 날일수록 짧은 컷 선호가 뚜렷",
     "weather": "비, 24도, 습함", "promo": "장마철 관리 편한 스타일"},
    {"key": "buzz",       "photos": ["버즈컷", "크루컷", "후면"],
     "trend": "한여름 버즈컷·크루컷 수요 증가",
     "weather": "맑음, 33도, 폭염", "promo": "폭염에 시원하게 가는 선택"},
    {"key": "foreign",    "photos": ["아메리칸페이드", "외국인손님", "측면"],
     "trend": "용산 인근 주한 외국인 대상 바버샵 수요",
     "weather": "맑음, 30도", "promo": "미군부대 인근 정통 아메리칸 스타일"},
    {"key": "beforeafter","photos": ["비포애프터", "페이드컷", "정면"],
     "trend": "비포애프터 콘텐츠가 시술력 전달에 효과적",
     "weather": "흐림, 25도", "promo": "변화가 한눈에 보이는 컷"},
]

CONDITIONS = ["ai_rag", "human_rag", "cold"]

# post_writer가 LLM 호출/파싱에 실패하면 _fallback_draft()의 고정 문구를 돌려준다.
# 그게 채점지에 섞이면 그 문항은 조건을 대표하지 못하므로 반드시 재생성해야 한다.
FALLBACK_MARKER = "오늘도 깔끔한 스타일로 새로운 하루를 시작해보세요"
MAX_GEN_ATTEMPTS = 3


def _is_fallback(draft: dict) -> bool:
    return FALLBACK_MARKER in (draft.get("caption") or "")


def _trend_data(sc):
    return {
        "trend": sc["trend"], "weather": sc["weather"], "promo": sc["promo"],
        "raw_snippets": [],
        "competitor_insights": {
            "gap_opportunity": "정통 바버샵의 시술 원칙을 내세운 콘텐츠가 드묾",
            "source": "search"},
    }


def _photos(sc):
    return [{"style_tags": sc["photos"]}]


def _ai_captions(limit=8):
    rows = list(ragc.query_items(
        query=("SELECT TOP @n c.caption FROM c WHERE c.shop_id=@s "
               "AND c.content_type='caption_body' AND c.authored_by='ai'"),
        parameters=[{"name": "@n", "value": limit}, {"name": "@s", "value": AI_RAG_SHOP}],
        partition_key=AI_RAG_SHOP))
    return [r["caption"] for r in rows]


def _anchors():
    """실제 사람이 쓴 게시물 — 원본 전체 텍스트 + 실제 해시태그 그대로.

    [변경] 처음엔 인트로 한 줄만 뽑았다. 고정 정보블록이 형식으로 티가 나서
    블라인드를 지키려던 것인데, 그 결과 앵커만 해시태그·CTA 없이 한 줄로 떠서
    "구체성"·"첫 문장" 항목에서 글 품질과 무관한 이유로 불리해졌다.
    → 채점 공정성이 우선. 원본 그대로 넣는다.
      대신 앵커는 형식만으로 구별되므로 블라인드가 아니며,
      조건 간 비교(ai_rag/human_rag/cold)의 참조점으로만 읽어야 한다.

    인트로 없는 게시물은 본문이 고정 정보블록뿐이라 서로 완전히 같은 항목이 되므로
    (8건 중 5건) 인트로가 있는 것만 앵커로 쓴다.
    """
    out = []
    for p in POSTS:
        if not (p.get("intro") or "").strip():
            continue
        out.append({"caption": p["caption"], "hashtags": p.get("hashtags", []),
                    "likes": p.get("likes"), "comments": p.get("comments"),
                    "note": p.get("note")})
    return out


async def _generate(sc, cond, rag_context):
    """fallback 캡션이 나오면 재시도. 끝까지 실패하면 None."""
    for attempt in range(1, MAX_GEN_ATTEMPTS + 1):
        draft = await post_writer_agent(
            shop_id="baseline", trend_data=_trend_data(sc), selected_photos=_photos(sc),
            brand_settings=BRAND_SETTINGS, recent_posts=[], rag_context=rag_context)
        if not _is_fallback(draft):
            return draft
        print(f"    ⚠️  fallback 감지 ({sc['key']}/{cond}) → 재시도 {attempt}/{MAX_GEN_ATTEMPTS}")
    print(f"    ❌ {sc['key']}/{cond} 생성 실패 — 이 문항은 채점에서 제외해야 함")
    return None


async def _build_rag():
    human_caps = [p["caption"] for p in POSTS if p["caption"].strip()]
    ai_caps = _ai_captions(8)
    print(f"사람 원본 {len(human_caps)}건 / AI 생성분 {len(ai_caps)}건으로 RAG 구성 중...")
    return {
        "human_rag": await _compress_context(
            [{"content_type": "caption_body", "caption": c} for c in human_caps], BRAND_SETTINGS),
        "ai_rag": await _compress_context(
            [{"content_type": "caption_body", "caption": c} for c in ai_caps], BRAND_SETTINGS),
        "cold": {},
    }


async def repair():
    """기존 out/items.json에서 fallback이 섞인 문항만 다시 생성한다 (id/정답표 정렬 유지)."""
    items_path = os.path.join(OUT_DIR, "items.json")
    key_path = os.path.join(OUT_DIR, "key.json")
    items = json.load(open(items_path, encoding="utf-8"))
    key = {k["id"]: k for k in json.load(open(key_path, encoding="utf-8"))}
    by_scenario = {s["key"]: s for s in SCENARIOS}

    broken = [it for it in items if _is_fallback(it)]
    if not broken:
        print("복구 대상 없음 — 모든 문항 정상")
        return

    print(f"복구 대상 {len(broken)}건: {[b['id'] for b in broken]}")
    rag = await _build_rag()

    fixed = 0
    for it in items:
        if not _is_fallback(it):
            continue
        k = key[it["id"]]
        sc, cond = by_scenario[k["scenario"]], k["condition"]
        print(f"  재생성: {it['id']} ({k['scenario']}/{cond})")
        draft = await _generate(sc, cond, rag[cond])
        if draft:
            it["caption"] = draft.get("caption", "")
            it["hashtags"] = draft.get("hashtags", [])
            it["cta"] = draft.get("cta", "")
            fixed += 1

    json.dump(items, open(items_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"복구 완료: {fixed}/{len(broken)}건")


def refresh_anchors():
    """items.json의 앵커 문항만 현재 _anchors() 내용으로 갱신한다.

    문항 ID와 정답표 정렬을 그대로 두므로, 이미 검토한 채점지의 번호가 바뀌지 않는다.
    LLM 호출 없음.
    """
    items_path = os.path.join(OUT_DIR, "items.json")
    key_path = os.path.join(OUT_DIR, "key.json")
    items = json.load(open(items_path, encoding="utf-8"))
    key = {k["id"]: k for k in json.load(open(key_path, encoding="utf-8"))}

    anchor_ids = [it["id"] for it in items if key.get(it["id"], {}).get("condition") == "anchor"]
    anchors = _anchors()
    if len(anchor_ids) != len(anchors):
        sys.exit(f"앵커 개수 불일치: 문항 {len(anchor_ids)}개 vs 원본 {len(anchors)}개. "
                 f"전체 재생성(generate_items.py)이 필요합니다.")

    # [중요] items.json은 섞인 순서라 POSTS 순서와 다르다.
    # 위치로 zip하면 캡션과 정답표의 실제 좋아요/댓글 수가 서로 다른 게시물을 가리키게 된다.
    # → key에 저장된 note로 원본을 되찾는다.
    by_note = {a["note"]: a for a in anchors}
    for item_id in anchor_ids:
        note = key[item_id].get("note")
        a = by_note.get(note)
        if not a:
            sys.exit(f"{item_id}의 note('{note}')에 해당하는 원본을 찾을 수 없습니다. "
                     f"전체 재생성이 필요합니다.")
        it = next(x for x in items if x["id"] == item_id)
        it["caption"] = a["caption"]
        it["hashtags"] = a["hashtags"]
        it["cta"] = ""
        print(f"  {item_id} 갱신 → {len(a['caption'])}자, 해시태그 {len(a['hashtags'])}개 "
              f"| {note}")

    json.dump(items, open(items_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"앵커 {len(anchor_ids)}개 갱신 완료 (문항 ID 유지)")


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    human_caps = [p["caption"] for p in POSTS if p["caption"].strip()]
    ai_caps = _ai_captions(8)
    print(f"사람 원본 {len(human_caps)}건 / AI 생성분 {len(ai_caps)}건으로 RAG 구성 중...")

    rag = {
        "human_rag": await _compress_context(
            [{"content_type": "caption_body", "caption": c} for c in human_caps], BRAND_SETTINGS),
        "ai_rag": await _compress_context(
            [{"content_type": "caption_body", "caption": c} for c in ai_caps], BRAND_SETTINGS),
        "cold": {},
    }

    items, key = [], []
    for sc in SCENARIOS:
        for cond in CONDITIONS:
            print(f"  생성: {sc['key']} / {cond}")
            draft = await _generate(sc, cond, rag[cond])
            if not draft:
                continue
            items.append({"caption": draft.get("caption", ""),
                          "hashtags": draft.get("hashtags", []),
                          "cta": draft.get("cta", "")})
            key.append({"scenario": sc["key"], "condition": cond})

    for a in _anchors():
        items.append({"caption": a["caption"], "hashtags": a["hashtags"], "cta": ""})
        key.append({"scenario": "anchor", "condition": "anchor",
                    "real_likes": a["likes"], "real_comments": a["comments"], "note": a["note"]})

    # 섞기 — 조건이 순서로 드러나지 않게. seed 고정으로 재현 가능하게 둔다.
    order = list(range(len(items)))
    random.Random(20260807).shuffle(order)

    shuffled_items, shuffled_key = [], []
    for new_idx, old_idx in enumerate(order):
        item_id = f"C{new_idx + 1:02d}"
        shuffled_items.append({"id": item_id, **items[old_idx]})
        shuffled_key.append({"id": item_id, **key[old_idx]})

    with open(os.path.join(OUT_DIR, "items.json"), "w", encoding="utf-8") as f:
        json.dump(shuffled_items, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "key.json"), "w", encoding="utf-8") as f:
        json.dump(shuffled_key, f, ensure_ascii=False, indent=2)

    counts = {}
    for k in shuffled_key:
        counts[k["condition"]] = counts.get(k["condition"], 0) + 1
    print(f"\n총 {len(shuffled_items)}개 문항 생성 → {OUT_DIR}")
    print(f"조건별: {counts}")
    print("items.json = 채점지용(라벨 없음) / key.json = 정답표(채점자에게 주지 말 것)")


if __name__ == "__main__":
    if "--repair" in sys.argv:
        asyncio.run(repair())
    elif "--refresh-anchors" in sys.argv:
        refresh_anchors()
    else:
        asyncio.run(main())
