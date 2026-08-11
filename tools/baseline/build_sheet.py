"""
블라인드 베이스라인 — 채점지(웹 페이지) 생성

out/items.json을 읽어 브라우저에서 바로 열 수 있는 단일 HTML 파일을 만든다.
서버 불필요, 인터넷 불필요. 채점자에게 이 파일 하나만 보내면 된다.

특징:
  - 한 화면에 한 문항 (긴 목록을 스크롤하며 지치지 않게)
  - 자동 임시저장 (브라우저를 닫아도 이어서 채점 가능)
  - 조건 라벨을 페이지 어디에도 넣지 않음 (블라인드 유지)
  - 마지막에 결과 JSON 파일 내려받기 → 지현님께 전달

실행:
  python tools/baseline/build_sheet.py
  → out/채점지.html
"""

import json
import os

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")

SHOP_PROFILE = """US바버샵 용산본점 (서울 용산구 삼각지)
· Since 2002, 용산 미군부대 영내 바버샵 경력
· 아메리칸 페이드컷 20년 이상 경력 정통 바버샵
· 펌·염색 시술 안 함 — 컷, 드라이, 스타일링으로만 완성
· 주 고객: 용산·삼각지 인근 직장인, 주한 외국인
· 자주 하는 스타일: 아메리칸 페이드컷, 사이드파트, 포마드컷, 스킨페이드"""

QUESTIONS = [
    {"id": "ai_feel", "label": "사람이 쓴 글 같나요?",
     "help": "1 = AI가 쓴 티가 확 난다 · 5 = 사람이 직접 쓴 것 같다",
     "type": "scale5", "low": "AI 같다", "high": "사람 같다"},
    {"id": "shop_fit", "label": "이 샵이 쓸 법한 글인가요?",
     "help": "위의 샵 정보를 기준으로. 1 = 아무 샵에나 붙는 글 · 5 = 이 샵답다",
     "type": "scale5", "low": "이 샵 같지 않다", "high": "이 샵답다"},
    {"id": "specific", "label": "내용이 구체적인가요?",
     "help": "1 = 뻔한 일반론뿐 · 5 = 실제 시술·스타일 얘기가 구체적",
     "type": "scale5", "low": "두루뭉술", "high": "구체적"},
    {"id": "hook", "label": "첫 문장이 눈길을 끄나요?",
     "help": "인스타에서 스크롤하다 멈출 만한 첫 문장인지",
     "type": "scale5", "low": "그냥 넘긴다", "high": "멈추게 된다"},
    {"id": "defects", "label": "어색하거나 잘못된 곳이 있나요?",
     "help": "문장이 깨졌거나, 같은 말이 반복되거나, 사실과 다른 내용",
     "type": "count", "options": ["없음", "1군데", "2군데", "3군데 이상"]},
    {"id": "verdict", "label": "이 글을 그대로 올리시겠어요?",
     "help": "실제로 우리 샵 인스타에 올린다고 생각하고",
     "type": "choice", "options": ["그대로 올린다", "조금 고쳐서 올린다", "안 올린다"]},
]

TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>바이백 캡션 평가</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;
     background:#f4f5f7;color:#1a1a1a;line-height:1.65;-webkit-text-size-adjust:100%}
.wrap{max-width:640px;margin:0 auto;padding:16px 16px 96px}
header{position:sticky;top:0;background:#f4f5f7;padding:12px 0 8px;z-index:10}
.bar{height:6px;background:#e2e5ea;border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;background:#2f6fed;width:0;transition:width .25s}
.count{font-size:13px;color:#666;margin-top:6px}
.card{background:#fff;border-radius:14px;padding:20px;margin-top:14px;box-shadow:0 1px 3px rgba(0,0,0,.07)}
.cap{white-space:pre-wrap;font-size:17px;line-height:1.8}
.tags{margin-top:14px;font-size:14px;color:#2f6fed;word-break:break-all}
.cta{margin-top:10px;font-size:14px;color:#555;padding-top:10px;border-top:1px solid #eee}
h3{font-size:16px;margin:22px 0 2px}
.help{font-size:13.5px;color:#666;margin-bottom:10px}
.opts{display:flex;gap:8px}
.opts.col{flex-direction:column}
button.opt{flex:1;padding:14px 8px;font-size:15px;border:1.5px solid #d5d9e0;background:#fff;
      border-radius:10px;cursor:pointer;font-family:inherit;color:#1a1a1a}
button.opt:active{transform:scale(.98)}
button.opt.on{background:#2f6fed;border-color:#2f6fed;color:#fff;font-weight:600}
.ends{display:flex;justify-content:space-between;font-size:12px;color:#888;margin-top:6px}
textarea{width:100%;min-height:64px;padding:12px;border:1.5px solid #d5d9e0;border-radius:10px;
      font-family:inherit;font-size:15px;margin-top:8px;resize:vertical}
.nav{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #e2e5ea;
     padding:12px 16px;display:flex;gap:10px;justify-content:center}
.nav>button{flex:1;max-width:300px;padding:15px;font-size:16px;border-radius:10px;border:none;
     cursor:pointer;font-family:inherit;font-weight:600}
#prev{background:#e8eaee;color:#333}
#next{background:#2f6fed;color:#fff}
#next:disabled{background:#c3cbd8}
details{background:#fff;border-radius:12px;padding:14px 18px;margin-top:12px}
summary{cursor:pointer;font-weight:600;font-size:15px}
details p{white-space:pre-wrap;font-size:14.5px;color:#444;margin:12px 0 0}
.intro{background:#fff;border-radius:14px;padding:24px;margin-top:14px}
.intro h1{font-size:21px;margin:0 0 12px}
.intro li{margin:7px 0;font-size:15px}
input[type=text]{width:100%;padding:13px;border:1.5px solid #d5d9e0;border-radius:10px;
     font-size:16px;font-family:inherit;margin-top:8px}
.done{text-align:center;padding:40px 20px}
.done h2{font-size:22px}
.hidden{display:none}
</style></head><body><div class="wrap">

<div id="intro" class="intro">
  <h1>바이백 캡션 평가</h1>
  <p style="font-size:15px;color:#444">
  인스타그램에 올릴 글 <b>__N__개</b>를 하나씩 보시고 평가해 주세요.
  어떤 글이 어떻게 만들어졌는지는 알려드리지 않습니다 — 순수하게 글만 보고 판단해 주시면 됩니다.</p>
  <ul style="color:#444;padding-left:20px">
    <li>정답은 없습니다. 첫인상대로 골라주세요.</li>
    <li>한 문항에 30초~1분이면 충분합니다.</li>
    <li>중간에 창을 닫아도 이어서 하실 수 있습니다.</li>
    <li>다 하시면 마지막에 파일이 하나 저장됩니다. 그 파일을 보내주세요.</li>
  </ul>
  <label style="font-size:15px;font-weight:600">평가하시는 분 성함</label>
  <input type="text" id="rater" placeholder="예: 홍길동" autocomplete="off">
  <button id="start" class="opt" style="margin-top:16px;background:#2f6fed;color:#fff;
     border-color:#2f6fed;font-weight:600;padding:15px">시작하기</button>
</div>

<div id="app" class="hidden">
  <header>
    <div class="bar"><i id="prog"></i></div>
    <div class="count" id="count"></div>
  </header>

  <details id="profile">
    <summary>이 샵은 어떤 곳인가요? (누르면 열립니다)</summary>
    <p>__PROFILE__</p>
  </details>

  <div class="card">
    <div class="cap" id="cap"></div>
    <div class="tags" id="tags"></div>
    <div class="cta" id="cta"></div>
  </div>

  <div id="qs"></div>

  <div class="nav">
    <button id="prev">이전</button>
    <button id="next">다음</button>
  </div>
</div>

<div id="done" class="hidden done">
  <h2>다 하셨습니다. 감사합니다!</h2>
  <p style="color:#555">아래 버튼을 누르면 파일이 저장됩니다.<br>그 파일을 지현님께 보내주세요.</p>
  <button id="dl" class="opt" style="background:#2f6fed;color:#fff;border-color:#2f6fed;
     font-weight:600;padding:16px;margin-top:14px">결과 파일 저장하기</button>
  <p style="color:#888;font-size:13px;margin-top:24px">저장이 안 되면 이 화면을 캡처해서 보내주셔도 됩니다.</p>
  <pre id="raw" style="text-align:left;background:#fff;padding:12px;border-radius:10px;
     font-size:11px;overflow:auto;max-height:240px;margin-top:12px"></pre>
</div>

</div>
<script>
const ITEMS = __ITEMS__;
const QS = __QUESTIONS__;
const KEY = "bybaek_baseline_v1";
let state = JSON.parse(localStorage.getItem(KEY) || '{"rater":"","answers":{},"idx":0}');
let idx = state.idx || 0;

const $ = id => document.getElementById(id);
const save = () => { state.idx = idx; localStorage.setItem(KEY, JSON.stringify(state)); };

function render() {
  const it = ITEMS[idx];
  $("cap").textContent = it.caption;
  $("tags").textContent = (it.hashtags || []).join(" ");
  $("tags").style.display = (it.hashtags || []).length ? "block" : "none";
  $("cta").textContent = it.cta || "";
  $("cta").style.display = it.cta ? "block" : "none";
  $("count").textContent = `${idx + 1} / ${ITEMS.length}`;
  $("prog").style.width = ((idx) / ITEMS.length * 100) + "%";

  const ans = state.answers[it.id] || {};
  $("qs").innerHTML = "";
  QS.forEach(q => {
    const box = document.createElement("div");
    const opts = q.type === "scale5" ? ["1","2","3","4","5"] : q.options;
    box.innerHTML = `<h3>${q.label}</h3><div class="help">${q.help}</div>`;
    const row = document.createElement("div");
    row.className = "opts" + (q.type === "choice" ? " col" : "");
    opts.forEach((o, i) => {
      const b = document.createElement("button");
      b.className = "opt" + (ans[q.id] === i ? " on" : "");
      b.textContent = o;
      b.onclick = () => {
        state.answers[it.id] = Object.assign({}, state.answers[it.id], {[q.id]: i});
        save(); render();
      };
      row.appendChild(b);
    });
    box.appendChild(row);
    if (q.type === "scale5") {
      const e = document.createElement("div");
      e.className = "ends";
      e.innerHTML = `<span>${q.low}</span><span>${q.high}</span>`;
      box.appendChild(e);
    }
    $("qs").appendChild(box);
  });

  const note = document.createElement("div");
  note.innerHTML = `<h3>하고 싶은 말 (선택)</h3>
    <div class="help">어디가 어색한지, 뭐가 좋았는지 짧게 적어주셔도 됩니다.</div>`;
  const ta = document.createElement("textarea");
  ta.value = ans.note || "";
  ta.oninput = () => {
    state.answers[it.id] = Object.assign({}, state.answers[it.id], {note: ta.value});
    save();
  };
  note.appendChild(ta);
  $("qs").appendChild(note);

  const done = QS.every(q => (state.answers[it.id] || {})[q.id] !== undefined);
  $("next").disabled = !done;
  $("next").textContent = (idx === ITEMS.length - 1) ? "제출하기" : "다음";
  $("prev").style.visibility = idx === 0 ? "hidden" : "visible";
  window.scrollTo(0, 0);
}

$("start").onclick = () => {
  const n = $("rater").value.trim();
  if (!n) { alert("성함을 입력해 주세요."); return; }
  state.rater = n; save();
  $("intro").classList.add("hidden");
  $("app").classList.remove("hidden");
  render();
};
$("prev").onclick = () => { if (idx > 0) { idx--; save(); render(); } };
$("next").onclick = () => {
  if (idx < ITEMS.length - 1) { idx++; save(); render(); }
  else { finish(); }
};

function payload() {
  return JSON.stringify({
    rater: state.rater,
    submitted_at: new Date().toISOString(),
    questions: QS.map(q => ({id: q.id, type: q.type,
                             options: q.type === "scale5" ? ["1","2","3","4","5"] : q.options})),
    answers: state.answers
  }, null, 2);
}

function finish() {
  $("app").classList.add("hidden");
  $("done").classList.remove("hidden");
  $("raw").textContent = payload();
}

$("dl").onclick = () => {
  const blob = new Blob([payload()], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `평가결과_${state.rater || "익명"}.json`;
  a.click();
};

if (state.rater && Object.keys(state.answers).length) {
  $("rater").value = state.rater;
}
</script></body></html>
"""


def main():
    items = json.load(open(os.path.join(OUT_DIR, "items.json"), encoding="utf-8"))
    html = (TEMPLATE
            .replace("__ITEMS__", json.dumps(items, ensure_ascii=False))
            .replace("__QUESTIONS__", json.dumps(QUESTIONS, ensure_ascii=False))
            .replace("__PROFILE__", SHOP_PROFILE)
            .replace("__N__", str(len(items))))
    path = os.path.join(OUT_DIR, "채점지.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"채점지 생성 → {path}  ({len(items)}문항, {len(QUESTIONS)}개 항목)")
    print("이 파일 하나만 채점자에게 보내면 됩니다 (브라우저로 열기, 서버 불필요).")


if __name__ == "__main__":
    main()
