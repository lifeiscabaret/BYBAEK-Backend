#!/usr/bin/env python
"""
photo_filter 회귀 테스트 러너 (골든셋 기반).

목적: 필터 프롬프트/임계값/게이트를 수정할 때 배포 전에 이 스크립트를 돌려
      오통과(false pass) / 오탈락(false fail) 을 분리 측정하고 결과를 PR에 첨부한다.
      "기준점 고정"의 실체.

사용:
  python scripts/eval_photo_filter.py                # 1회 실행, 정답 대비 리포트
  python scripts/eval_photo_filter.py --runs 2       # 2회 실행 → 결정성(재현성) 측정 (PART 1.5)
  python scripts/eval_photo_filter.py --with-refs    # 샵 few-shot 레퍼런스 포함(기본: 미포함=재현성↑)
  python scripts/eval_photo_filter.py --only-confirmed  # label_status=draft_confirmed 항목만

설계 노트:
- stage1(밝기/흔들림, OpenCV=결정적) → stage2(GPT Vision) 를 프로덕션과 동일 순서로 태운다.
- 기본은 few-shot 레퍼런스 없이(rules-only) 실행 → 샵/시점에 따라 흔들리지 않는 안정 기준선.
  프로덕션 충실도가 필요하면 --with-refs.
- 판정 = 최종 pass/fail. 정답(expected_pass)과 비교.
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from agents import photo_filter as pf  # noqa: E402

GOLDEN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "golden", "photo_filter_golden.json",
)
SHOP_ID = "00000000-0000-0000-0718-3a306722d45c"


async def _judge_one(item, good_refs, bad_refs):
    """단일 사진을 stage1→stage2로 태워 최종 판정 반환."""
    blob = item["blob_url"]
    image_id = item["id"]

    # stage1 (결정적)
    s1, s1_reason = await pf._analyze_stage1(blob)
    if s1 != "Pass":
        return {
            "id": image_id, "pass": False, "category": None,
            "total": None, "stage": "stage1_fail", "reason": s1_reason,
            "scores": {}, "fade_cut_score": None, "detected_angle": None,
        }

    # stage2 (GPT Vision) — 일시적 다운로드/네트워크 오류는 재시도 (결정성 측정 오염 방지).
    # 주의: 이 재시도는 '측정 하네스' 전용이다. 프로덕션의 '오류→영구 failed 저장' 버그(BUG C)는
    #       별도 과제로 다룬다 (여기서 감추지 않는다).
    TRANSIENT_SIGNS = ("download", "timed out", "unable to download",
                       "evaluation_error", "timeout", "temporarily")
    last = None
    for attempt in range(3):
        res = await pf._evaluate_photo(image_id, blob, good_refs, bad_refs)
        reason = (res.get("reason") or "").lower()
        total = res.get("total_score")
        transient = (total in (0, None)) and any(s in reason for s in TRANSIENT_SIGNS)
        if not transient:
            return {
                "id": image_id, "pass": bool(res.get("stage2_pass")),
                "category": res.get("photo_category"), "total": total,
                "stage": "stage2", "reason": res.get("reason", ""),
                "scores": res.get("scores", {}),
                "fade_cut_score": res.get("fade_cut_score"),
                "detected_angle": res.get("detected_angle"),
            }
        last = res
        await asyncio.sleep(1.5 * (attempt + 1))
    # 3회 모두 일시적 오류 → 측정에서 제외 표시
    return {
        "id": image_id, "pass": None, "category": None, "total": None,
        "stage": "transient_error", "reason": last.get("reason", "") if last else "",
        "scores": {}, "fade_cut_score": None, "detected_angle": None,
    }


async def run_once(items, good_refs, bad_refs, concurrency=3):
    sem = asyncio.Semaphore(concurrency)

    async def guarded(it):
        async with sem:
            try:
                return await _judge_one(it, good_refs, bad_refs)
            except Exception as e:
                return {"id": it["id"], "pass": False, "category": None,
                        "total": None, "stage": "error", "reason": str(e)}

    results = await asyncio.gather(*[guarded(it) for it in items])
    return {r["id"]: r for r in results}


def report(items, run_result):
    by_id = {it["id"]: it for it in items}
    false_pass, false_fail, cat_wrong = [], [], []
    correct = 0
    cat_correct = 0
    cat_total = 0
    evaluated = 0
    excluded = []   # transient_error 등 측정 제외
    confusion = defaultdict(Counter)

    for iid, r in run_result.items():
        it = by_id[iid]
        got_pass = r["pass"]
        if got_pass is None:   # 일시적 오류 → 측정에서 제외
            excluded.append((iid, it["original_name"], r["stage"]))
            continue
        evaluated += 1
        exp_pass = it["expected_pass"]
        if got_pass == exp_pass:
            correct += 1
        elif got_pass and not exp_pass:
            false_pass.append((iid, it["original_name"], it["expected_category"], r["category"], r["total"]))
        elif not got_pass and exp_pass:
            false_fail.append((iid, it["original_name"], it["expected_category"], r["category"], r["total"], r["stage"]))

        exp_cat = it["expected_category"]
        if exp_cat is not None:
            cat_total += 1
            confusion[exp_cat][r["category"]] += 1
            if r["category"] == exp_cat:
                cat_correct += 1
            else:
                cat_wrong.append((iid, it["original_name"], exp_cat, r["category"]))

    n = evaluated
    print("=" * 72)
    acc = (correct / n * 100) if n else 0.0
    cacc = (cat_correct / cat_total * 100) if cat_total else 0.0
    print(f"[전체 정확도] pass/fail 정답 {correct}/{n} = {acc:.1f}%")
    print(f"[카테고리 정확도] {cat_correct}/{cat_total} = {cacc:.1f}% (expected_category!=null 항목만)")
    if excluded:
        print(f"[측정 제외] 일시적 오류 {len(excluded)}건: " +
              ", ".join(f"{iid}({st})" for iid, _, st in excluded))

    print(f"\n[오통과 FALSE PASS] {len(false_pass)}건 (탈락해야 하는데 통과):")
    for iid, name, ec, gc, tot in false_pass:
        print(f"  {iid} {name} | 정답cat={ec} → 판정cat={gc} total={tot}")

    print(f"\n[오탈락 FALSE FAIL] {len(false_fail)}건 (통과해야 하는데 탈락):")
    for iid, name, ec, gc, tot, stage in false_fail:
        print(f"  {iid} {name} | 정답cat={ec} → 판정cat={gc} total={tot} ({stage})")

    print(f"\n[카테고리 오분류] {len(cat_wrong)}건:")
    for iid, name, ec, gc in cat_wrong:
        print(f"  {iid} {name} | {ec} → {gc}")

    print("\n[혼동 행렬] expected → {got: n}")
    for ec in sorted(confusion):
        print(f"  {ec}: {dict(confusion[ec])}")
    print("=" * 72)
    return {"accuracy": (correct / n) if n else 0.0, "false_pass": false_pass, "false_fail": false_fail}


def compare_runs(items, runs):
    """여러 실행 간 판정이 갈린 사진 = 비결정성(노이즈) 측정."""
    by_id = {it["id"]: it for it in items}
    ids = list(runs[0].keys())
    threshold = pf.STAGE2_PASS_THRESHOLD

    flip_pass, flip_cat = [], []
    skipped_transient = []
    # 지표3: 사진별 total_score 편차 (max-min across runs). stage1 탈락 등 None은 제외.
    score_spans = []   # (iid, name, [scores], span)
    for iid in ids:
        # 어느 run에서든 일시적 오류(pass=None)면 결정성 분석에서 제외
        if any(run[iid]["pass"] is None for run in runs):
            skipped_transient.append(iid)
            continue
        passes = {run[iid]["pass"] for run in runs}
        cats = {run[iid]["category"] for run in runs}
        if len(passes) > 1:
            flip_pass.append((iid, by_id[iid]["original_name"], [run[iid]["pass"] for run in runs]))
        if len(cats) > 1:
            flip_cat.append((iid, by_id[iid]["original_name"], [run[iid]["category"] for run in runs]))
        scores = [run[iid]["total"] for run in runs if run[iid]["total"] is not None]
        if len(scores) >= 2:
            span = max(scores) - min(scores)
            score_spans.append((iid, by_id[iid]["original_name"], scores, span))

    print("\n" + "#" * 72)
    print(f"[결정성 측정] {len(runs)}회 실행 비교  (통과선 threshold={threshold})")
    if skipped_transient:
        print(f"  [측정 제외] 일시적 오류로 제외된 사진 {len(skipped_transient)}장: {skipped_transient}")

    print(f"\n  [지표1] pass/fail 판정이 갈린 사진: {len(flip_pass)}/{len(ids)}")
    for iid, name, seq in flip_pass:
        print(f"    {iid} {name}: {seq}")

    print(f"\n  [지표2] 카테고리 판정이 갈린 사진: {len(flip_cat)}/{len(ids)}")
    for iid, name, seq in flip_cat:
        print(f"    {iid} {name}: {seq}")

    # 지표3: total_score 편차 평균/최대
    print(f"\n  [지표3] total_score run간 편차 (측정 대상 {len(score_spans)}장)")
    if score_spans:
        spans = [s[3] for s in score_spans]
        avg_span = sum(spans) / len(spans)
        max_span = max(spans)
        print(f"    평균 편차: {avg_span:.2f}점 | 최대 편차: {max_span}점")
        print(f"    편차 큰 순 top5:")
        for iid, name, scores, span in sorted(score_spans, key=lambda x: -x[3])[:5]:
            print(f"      {iid} {name}: {scores} (span={span})")
    else:
        print("    (2회 이상 점수가 잡힌 사진 없음)")

    # 지표4: 통과선(15)으로부터의 거리 분포 — 흔들리면 뒤집히는 위치에 몰려 있는가
    print(f"\n  [지표4] 통과선({threshold})으로부터 거리 분포 (각 사진의 run 평균 총점 기준)")
    danger = []   # |avg - threshold| <= 2  → 편차에 취약
    buckets = {"0-1 (초위험)": 0, "2-3 (경계)": 0, "4-6 (보통)": 0, "7+ (안전)": 0}
    for iid, name, scores, span in score_spans:
        avg = sum(scores) / len(scores)
        dist = abs(avg - threshold)
        if dist <= 1:
            buckets["0-1 (초위험)"] += 1
        elif dist <= 3:
            buckets["2-3 (경계)"] += 1
        elif dist <= 6:
            buckets["4-6 (보통)"] += 1
        else:
            buckets["7+ (안전)"] += 1
        if dist <= 2:
            danger.append((iid, name, round(avg, 1), round(dist, 1), span))
    for k, v in buckets.items():
        print(f"    {k}: {v}장")
    print(f"    통과선 ±2점 이내(편차에 취약) {len(danger)}장:")
    for iid, name, avg, dist, span in sorted(danger, key=lambda x: x[3]):
        print(f"      {iid} {name}: 평균={avg} 거리={dist} 편차={span}")

    # 지표5: 사진 단위 안정성 — 각 사진이 runs 중 몇 회 '최빈 판정'과 같았나 (N/총회차).
    #        5/5 아닌 사진 = 불안정 사진 (그 자체가 진단 정보).
    R = len(runs)
    print(f"\n  [지표5] 사진 단위 판정 안정성 (pass/fail 기준, {R}회 중 일치 횟수)")
    stable_counts = Counter()
    unstable = []
    for iid in ids:
        if any(run[iid]["pass"] is None for run in runs):
            continue
        passes = [run[iid]["pass"] for run in runs]
        top = Counter(passes).most_common(1)[0][1]  # 최빈 판정의 등장 횟수
        stable_counts[top] += 1
        if top < R:
            unstable.append((iid, by_id[iid]["original_name"], passes))
    for k in sorted(stable_counts, reverse=True):
        print(f"    {k}/{R} 일치: {stable_counts[k]}장")
    print(f"    불안정 사진(전회 일치 아님) {len(unstable)}장:")
    for iid, name, passes in unstable:
        print(f"      {iid} {name}: {passes}")
    print("#" * 72)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--with-refs", action="store_true")
    ap.add_argument("--only-confirmed", action="store_true")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()

    golden = json.load(open(GOLDEN_PATH, encoding="utf-8"))
    items = golden["items"]
    if args.only_confirmed:
        items = [i for i in items if i.get("label_status") == "draft_confirmed"]
    # 라벨 미확정(보류) 항목 제외: expected_pass=null (예: 원장 회신 대기)
    held = [i["id"] for i in items if i.get("expected_pass") is None]
    if held:
        print(f"[라벨 보류 제외] expected_pass=null {len(held)}건: {held}")
        items = [i for i in items if i.get("expected_pass") is not None]

    if not golden["_meta"].get("labels_final", False):
        print("⚠️  labels_final=false — 아직 초안 라벨입니다. 지현님 확인 후 최종화하세요.\n")

    good_refs, bad_refs = [], []
    if args.with_refs:
        refs = await pf._load_reference_photos(SHOP_ID)
        good_refs = [p for p in refs if p.get("label") == "good"][:pf.MAX_GOOD_EXAMPLES]
        bad_refs = [p for p in refs if p.get("label") == "bad"][:pf.MAX_BAD_EXAMPLES]
    print(f"대상 {len(items)}장 | refs good={len(good_refs)} bad={len(bad_refs)} | runs={args.runs}\n")

    runs = []
    for i in range(args.runs):
        print(f"\n----- RUN {i+1}/{args.runs} -----")
        rr = await run_once(items, good_refs, bad_refs, args.concurrency)
        report(items, rr)
        runs.append(rr)

    if args.runs > 1:
        compare_runs(items, runs)

    # 원시 판정 덤프 — 라벨 바뀌어도 GPT 재호출 없이 재채점 가능하게 넉넉히 저장.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tests", "golden", "runs")
    os.makedirs(dump_dir, exist_ok=True)
    dump_path = os.path.join(dump_dir, f"raw_{ts}.json")
    dump = {
        "meta": {
            "timestamp": datetime.now().isoformat(),
            "runs": args.runs,
            "with_refs": args.with_refs,
            "good_refs": len(good_refs),
            "bad_refs": len(bad_refs),
            "concurrency": args.concurrency,
            "threshold": pf.STAGE2_PASS_THRESHOLD,
            "model_note": "gpt-5-mini, temperature 미지정(모델 기본=1.0, 조절불가). 현재 게이트 구조 기준.",
            "golden_labels_final": golden["_meta"].get("labels_final", False),
            "excluded_expected_pass_null": held,
        },
        "runs_data": [
            {
                iid: {
                    "pass": r["pass"], "category": r["category"], "total": r["total"],
                    "scores": r.get("scores", {}), "fade_cut_score": r.get("fade_cut_score"),
                    "detected_angle": r.get("detected_angle"),
                    "stage": r["stage"], "reason": r["reason"],
                    "is_error": r["stage"] in ("stage1_fail", "transient_error", "error"),
                }
                for iid, r in rr.items()
            }
            for rr in runs
        ],
    }
    json.dump(dump, open(dump_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n[원시 덤프 저장] {dump_path}")


if __name__ == "__main__":
    asyncio.run(main())
