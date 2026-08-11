"""
블라인드 베이스라인 — 채점 결과 집계

out/results/*.json (채점자별 결과) + out/key.json (정답표)을 합쳐
조건별 평균과 채점자 간 일치도를 낸다.

실행:
  python tools/baseline/aggregate.py
"""

import glob
import json
import os
import sys

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
RESULTS_DIR = os.path.join(OUT_DIR, "results")

COND_LABEL = {
    "ai_rag":    "AI-RAG (현재 상태)",
    "human_rag": "사람원본 RAG",
    "cold":      "RAG 없음",
    "anchor":    "실제 사람 캡션",
}
SCALE_ITEMS = ["ai_feel", "shop_fit", "specific", "hook"]
VERDICT_LABEL = ["그대로 올린다", "조금 고쳐서", "안 올린다"]


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _fmt(v, nd=2):
    return "  -  " if v is None else f"{v:.{nd}f}"


def main():
    key_path = os.path.join(OUT_DIR, "key.json")
    if not os.path.exists(key_path):
        sys.exit("key.json이 없습니다. generate_items.py를 먼저 실행하세요.")
    key = {k["id"]: k for k in json.load(open(key_path, encoding="utf-8"))}

    files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")))
    if not files:
        sys.exit(f"채점 결과가 없습니다. 받은 파일을 {RESULTS_DIR}/ 에 넣어주세요.")

    raters = []
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        raters.append(data)
        print(f"불러옴: {data.get('rater', '?')}  ({len(data.get('answers', {}))}문항)  {os.path.basename(path)}")

    # 조건별 집계
    print("\n" + "=" * 72)
    print("조건별 평균 (1~5점, 높을수록 좋음)")
    print("=" * 72)
    header = f"{'조건':<22}{'사람같음':>9}{'이샵답':>9}{'구체성':>9}{'첫문장':>9}{'결함수':>9}{'n':>6}"
    print(header)
    print("-" * 72)

    by_cond = {}
    for data in raters:
        for item_id, ans in data.get("answers", {}).items():
            k = key.get(item_id)
            if not k:
                continue
            c = by_cond.setdefault(k["condition"], {q: [] for q in SCALE_ITEMS + ["defects", "verdict"]})
            for q in SCALE_ITEMS:
                if ans.get(q) is not None:
                    c[q].append(ans[q] + 1)      # 0-index → 1~5
            if ans.get("defects") is not None:
                c["defects"].append(ans["defects"])   # 0=없음 … 3=3군데 이상
            if ans.get("verdict") is not None:
                c["verdict"].append(ans["verdict"])

    for cond in ["anchor", "human_rag", "ai_rag", "cold"]:
        c = by_cond.get(cond)
        if not c:
            continue
        row = f"{COND_LABEL[cond]:<22}"
        for q in SCALE_ITEMS:
            row += f"{_fmt(_mean(c[q])):>9}"
        row += f"{_fmt(_mean(c['defects'])):>9}{len(c['verdict']):>6}"
        print(row)

    # 종합 판정
    print("\n" + "=" * 72)
    print("이 글을 그대로 올리겠는가 (%)")
    print("=" * 72)
    print(f"{'조건':<22}{'그대로':>10}{'조금 고쳐서':>13}{'안 올림':>10}")
    print("-" * 72)
    for cond in ["anchor", "human_rag", "ai_rag", "cold"]:
        c = by_cond.get(cond)
        if not c or not c["verdict"]:
            continue
        n = len(c["verdict"])
        pct = [100 * c["verdict"].count(i) / n for i in range(3)]
        print(f"{COND_LABEL[cond]:<22}{pct[0]:>9.0f}%{pct[1]:>12.0f}%{pct[2]:>9.0f}%")

    # 채점자별 편차 (후함/박함 확인)
    if len(raters) > 1:
        print("\n" + "=" * 72)
        print("채점자별 평균 (후함/박함 편차 확인용)")
        print("=" * 72)
        for data in raters:
            vals = []
            for ans in data.get("answers", {}).values():
                vals += [ans[q] + 1 for q in SCALE_ITEMS if ans.get(q) is not None]
            print(f"  {data.get('rater', '?'):<16} 전체 평균 {_fmt(_mean(vals))}")

    # 시나리오별 짝 비교 (같은 상황에서 조건 간 차이)
    print("\n" + "=" * 72)
    print("시나리오별 '이샵답' 점수 — 같은 상황에서 조건 비교")
    print("=" * 72)
    print(f"{'시나리오':<16}{'사람원본':>10}{'AI-RAG':>10}{'RAG없음':>10}")
    print("-" * 72)
    pairs = {}
    for data in raters:
        for item_id, ans in data.get("answers", {}).items():
            k = key.get(item_id)
            if not k or k["condition"] == "anchor" or ans.get("shop_fit") is None:
                continue
            pairs.setdefault(k["scenario"], {}).setdefault(k["condition"], []).append(ans["shop_fit"] + 1)
    for sc, conds in sorted(pairs.items()):
        print(f"{sc:<16}{_fmt(_mean(conds.get('human_rag', []))):>10}"
              f"{_fmt(_mean(conds.get('ai_rag', []))):>10}"
              f"{_fmt(_mean(conds.get('cold', []))):>10}")

    print("\n주의: anchor(실제 사람 캡션)는 형식이 달라 완전한 블라인드가 아닙니다.")
    print("      절대 기준선으로만 읽고, 조건 간 비교는 human_rag / ai_rag / cold로 하세요.")


if __name__ == "__main__":
    main()
