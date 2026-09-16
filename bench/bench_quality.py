#------------------------------------------------------------------
# 답변 품질 실측 — 프롬프트 변형이 0.6B 의 사실 혼동을 줄이는가
#=> 속도 벤치에서 Qwen3-0.6B 가 사실을 뒤섞는 문제가 관찰됐다(소멸 시점을
#   산정 기준일과 혼동 등). 모델을 키우면 해결되지만 그러면 속도가 죽으므로,
#   '프롬프트만으로 얼마나 건질 수 있는가' 를 정량으로 확인한다.
#
#   측정 방법
#    - 검색은 완벽하다고 가정하고(정답 근거를 항상 포함) 생성만 평가한다.
#      검색 품질과 생성 품질이 섞이면 원인 분리가 안 되기 때문이다.
#    - 질문마다 정답 키워드(must)와 '이 단어가 나오면 혼동' 오답 키워드(must_not)를
#      미리 정해 두고 자동 채점한다. 사람이 매번 읽지 않아도 비교가 된다.
#    - 반복률(같은 문장 되풀이)도 잰다. 0.6B 의 대표적 실패 양상이라서다.
#
#   사용:  python bench/bench_quality.py --models qwen3-0.6b-q4 qwen3-1.7b-q4
#------------------------------------------------------------------

import argparse
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from bench_llm import MODELS, MODELS_DIR, RESULTS_DIR, build_prompt, load_llm  # noqa: E402

# 근거 청크 풀 — 각 항목이 검색으로 뽑혀 프롬프트에 들어가는 1개 청크에 해당.
CHUNKS = {
    "수당지급": "제26조(연차수당) 미사용 연차유급휴가에 대하여는 통상임금을 기준으로 수당을 지급한다. "
              "연차수당은 휴가청구권이 소멸한 날이 속하는 달의 다음 급여지급일에 지급함을 원칙으로 한다.",
    "소멸":     "제25조 제5항 연차유급휴가는 1년간 행사하지 아니하면 소멸한다. "
              "다만 사용자의 귀책사유로 사용하지 못한 경우에는 그러하지 아니하다.",
    "산정기준": "연차휴가의 산정 기준일은 매년 1월 1일이며 회계연도 기준으로 일괄 관리한다.",
    "가산한도": "3년 이상 계속 근로한 직원에게는 매 2년마다 1일을 가산하며, "
              "가산휴가를 포함한 총 휴가일수는 25일을 한도로 한다.",
    "사용촉진": "사용촉진은 휴가 소멸 6개월 전을 기준으로 직원별 미사용 일수를 "
              "서면으로 통보하는 절차를 포함한다.",
    "재택근무": "제31조(재택근무) 재택근무는 주 2일을 한도로 하며 부서 사정에 따라 조정할 수 있다.",
    "보안신고": "제52조(보안사고) 보안 사고 발생 시 즉시 정보보호 담당 부서에 신고하여야 한다. "
              "인사 부서가 아닌 정보보호 담당 부서가 접수 창구이다.",
    "출장비":   "제40조(출장비) 출장비는 실비 정산을 원칙으로 하며 영수증 제출이 필요하다.",
    "교육비":   "제41조(교육훈련비) 교육훈련비는 연간 한도 내에서 정액으로 지급한다.",
    "미만휴가": "계속 근로기간이 1년 미만인 직원에게는 1개월 개근 시 1일의 유급휴가를 부여한다.",
    "기본휴가": "회사는 1년간 80퍼센트 이상 출근한 직원에게 15일의 유급휴가를 부여한다.",
    "가산주기": "3년 이상 계속 근로한 직원에게는 최초 1년을 초과하는 매 2년마다 1일을 가산한 휴가를 준다.",
    "출산휴가": "출산전후휴가 및 육아휴직 기간은 연차휴가 산정 시 출근한 것으로 본다.",
    "퇴직정산": "퇴직 시에는 미사용 연차일수 전부에 대하여 수당을 정산하여 최종 급여와 함께 지급한다.",
    "시간외":   "제33조(시간외근로) 시간외근로는 부서장의 사전 승인을 받은 경우에만 인정한다.",
}

# 평가 문항 — (질문, 넣을 근거 키 3개, 정답 키워드, 혼동 키워드)
#   근거에는 정답 청크와 '헷갈리기 쉬운' 청크를 일부러 섞는다.
#   예: 수당 지급 시점 질문에 '산정 기준일(1월 1일)' 청크를 함께 넣어 혼동을 유도.
CASES = [
    {
        # 주의: '언제'만 물으면서 정답 조건에 '통상임금'(=얼마)까지 넣으면
        # 맞는 답이 오답으로 깎인다. 두 사실을 모두 요구하도록 질문을 맞춘다.
        "q": "연차휴가 미사용분에 대한 수당은 어떤 임금을 기준으로, 언제 지급되나요?",
        "ctx": ["수당지급", "산정기준", "소멸"],
        "must": ["통상임금", "다음 급여지급일"],
        "must_not": ["1월 1일"],
    },
    {
        "q": "연차유급휴가는 언제 소멸하나요?",
        "ctx": ["소멸", "사용촉진", "산정기준"],
        "must": ["1년"],
        "must_not": ["6개월", "1월 1일"],
    },
    {
        "q": "가산휴가를 포함한 총 휴가일수 한도는 며칠인가요?",
        "ctx": ["가산한도", "소멸", "수당지급"],
        "must": ["25일"],
        "must_not": ["15일", "30일"],
    },
    {
        "q": "사용촉진은 언제를 기준으로 통보하나요?",
        "ctx": ["사용촉진", "소멸", "산정기준"],
        "must": ["6개월"],
        "must_not": ["1월 1일"],
    },
    {
        "q": "재택근무는 주 며칠까지 가능한가요?",
        "ctx": ["재택근무", "출장비", "보안신고"],
        "must": ["2일"],
        "must_not": ["3일", "5일"],
    },
    {
        "q": "보안 사고가 발생하면 어느 부서에 신고해야 하나요?",
        "ctx": ["보안신고", "출장비", "재택근무"],
        "must": ["정보보호"],
        "must_not": ["인사 부서에 신고", "인사부서에 신고"],
    },
    {
        "q": "출장비는 어떤 방식으로 정산하나요?",
        "ctx": ["출장비", "교육비", "시간외"],
        "must": ["실비"],
        "must_not": ["정액"],
    },
    {
        "q": "근속 1년 미만 직원에게는 휴가를 며칠 부여하나요?",
        "ctx": ["미만휴가", "기본휴가", "가산한도"],
        "must": ["1일"],
        "must_not": ["15일", "25일"],
    },
    {
        "q": "3년 이상 근속자에게 가산휴가는 몇 년마다 주어지나요?",
        "ctx": ["가산주기", "소멸", "미만휴가"],
        "must": ["2년"],
        "must_not": ["1개월", "3년마다"],
    },
    {
        "q": "육아휴직 기간은 연차휴가 산정에서 어떻게 처리되나요?",
        "ctx": ["출산휴가", "소멸", "퇴직정산"],
        "must": ["출근한 것으로"],
        "must_not": ["결근"],
    },
    {
        "q": "퇴직할 때 남은 연차는 어떻게 되나요?",
        "ctx": ["퇴직정산", "소멸", "산정기준"],
        "must": ["정산"],
        "must_not": ["소멸한다"],
    },
    {
        "q": "시간외근로는 어떤 절차를 거쳐야 인정되나요?",
        "ctx": ["시간외", "재택근무", "출장비"],
        "must": ["사전 승인"],
        "must_not": ["사후 승인", "사후승인"],
    },
]

# 프롬프트 변형 — 무엇이 사실 혼동을 줄이는지 하나씩 분리해 본다.
PROMPTS = {
    # V0: 지금까지 쓰던 기준선.
    "V0_baseline":
        "당신은 사내 문서 검색 도우미입니다. 아래 [근거] 안의 내용만 사용해 한국어로 "
        "간결하게 답변하세요. 근거에 없는 내용은 추측하지 마세요. "
        "답변은 3~5문장으로 요약하고 마지막에 근거 번호를 [1] 형식으로 표기하세요.",

    # V1: '먼저 원문을 그대로 옮기고 나서 정리' — 모델이 기억으로 지어내지 못하게
    #     근거 문장을 복사하는 단계를 강제한다.
    "V1_인용강제":
        "당신은 사내 문서 검색 도우미입니다. 반드시 아래 순서로만 답하세요.\n"
        "1) '인용:' 으로 시작해, 질문에 답이 되는 문장을 [근거]에서 글자 그대로 1~2개 옮겨 적습니다.\n"
        "2) '답변:' 으로 시작해, 그 인용문만 근거로 한 문장으로 답합니다.\n"
        "인용에 없는 숫자나 날짜는 절대 답변에 쓰지 마세요.",

    # V2: 질문과 무관한 근거를 먼저 배제시키는 지시 — 혼동 유도 청크에 대한 방어.
    "V2_무관근거배제":
        "당신은 사내 문서 검색 도우미입니다. [근거]에는 질문과 무관한 조항이 섞여 있습니다.\n"
        "먼저 질문에 직접 답하는 근거 하나만 고르고, 나머지는 무시하세요.\n"
        "고른 근거에 적힌 내용만으로 한 문장으로 답하세요. "
        "고른 근거에 없는 숫자·날짜·기간은 절대 쓰지 마세요. "
        "답을 찾을 수 없으면 '제공된 문서에서 확인할 수 없습니다'라고만 답하세요.",

    # V3: V1+V2 결합 + 반복 억제(한 문장 제한).
    "V3_결합":
        "당신은 사내 문서 검색 도우미입니다. [근거]에는 질문과 무관한 조항이 섞여 있습니다.\n"
        "1) '인용:' 뒤에 질문에 직접 답하는 문장 하나만 [근거]에서 글자 그대로 옮깁니다.\n"
        "2) '답변:' 뒤에 그 인용문만 사용해 딱 한 문장으로 답합니다. 같은 말을 반복하지 마세요.\n"
        "인용에 없는 숫자·날짜·기간은 절대 쓰지 마세요.",
}


#------------------------------------------------------------------
# 사용자 메시지 구성
#=> [근거] 블록 + 질문. 근거 순서는 케이스에 적힌 그대로 둔다(정답이 항상
#   1번에 오지 않도록 케이스별로 섞어 두었다).
#------------------------------------------------------------------
def build_user(case):
    blocks = []
    for i, key in enumerate(case["ctx"]):
        blocks.append("[{}] {}".format(i + 1, CHUNKS[key]))
    return "[근거]\n" + "\n\n".join(blocks) + "\n\n[질문]\n" + case["q"]


#------------------------------------------------------------------
# 채점 (핵심)
#=> 공백을 지운 문자열에서 부분일치로 본다. 한국어는 띄어쓰기가 흔들려서
#   그대로 비교하면 정답인데도 놓치기 때문이다.
#
# -in: text = 모델 답변
# -in: case = 정답/오답 키워드가 든 케이스
#
# -out: dict = hit(정답 키워드 충족 여부), confuse(혼동 발생), 세부 목록
#------------------------------------------------------------------
def score(text, case):
    # V1/V3 는 '인용:' 으로 근거를 그대로 옮기게 한다. 인용문에는 정답 키워드가
    # 당연히 들어 있으므로, 인용까지 채점하면 점수가 부풀려진다. 실제로 모델이
    # 주장하는 부분인 '답변:' 이후만 채점한다(없으면 전체).
    target = text.split("답변:", 1)[1] if "답변:" in text else text
    flat = re.sub(r"\s+", "", target)

    got = [k for k in case["must"] if re.sub(r"\s+", "", k) in flat]
    bad = [k for k in case["must_not"] if re.sub(r"\s+", "", k) in flat]

    return {
        "must_hit": len(got),
        "must_total": len(case["must"]),
        "correct": len(got) == len(case["must"]) and not bad,
        "confused": bool(bad),
        "confuse_terms": bad,
    }


#------------------------------------------------------------------
# 반복률 측정
#=> 문장 단위로 나눠 중복 비율을 본다. 0.6B 는 같은 문장을 두세 번 되풀이하는
#   실패가 잦아, 이 지표가 체감 품질과 잘 맞는다.
#
# -out: 0.0(반복 없음) ~ 1.0
#------------------------------------------------------------------
def repetition_rate(text):
    sents = [re.sub(r"\s+", "", s) for s in re.split(r"[.!?\n]", text)]
    sents = [s for s in sents if len(s) >= 10]
    if len(sents) < 2:
        return 0.0
    return round(1.0 - len(set(sents)) / len(sents), 3)


#------------------------------------------------------------------
# 모델 1개 x 프롬프트 1개를 전체 문항에 대해 평가
#------------------------------------------------------------------
def eval_combo(llm, pname, system, args):
    rows = []
    for case in CASES:
        prompt = build_prompt(system, build_user(case))
        llm.reset()
        t0 = time.perf_counter()
        r = llm.create_completion(prompt, max_tokens=args.max_tokens,
                                  temperature=0.0, stop=["<|im_end|>"])
        dt = time.perf_counter() - t0
        text = r["choices"][0]["text"]

        s = score(text, case)
        s.update(q=case["q"], prompt_variant=pname, sec=round(dt, 2),
                 gen_tokens=r["usage"]["completion_tokens"],
                 repetition=repetition_rate(text),
                 answer=text.strip().replace("\n", " ")[:300])
        rows.append(s)
    return rows


def main():
    p = argparse.ArgumentParser(description="답변 품질 실측")
    p.add_argument("--models", nargs="+", default=["qwen3-0.6b-q4", "qwen3-1.7b-q4"])
    p.add_argument("--variants", nargs="+", default=list(PROMPTS))
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--max-tokens", type=int, default=160)
    p.add_argument("--n-ctx", type=int, default=2048)
    args = p.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)
    all_rows = []

    for alias in args.models:
        path = os.path.join(MODELS_DIR, MODELS[alias])
        if not os.path.isfile(path):
            print("[skip] 모델 없음: " + path, file=sys.stderr)
            continue

        llm, _ = load_llm(path, args.threads, args.n_ctx)
        print("\n=== {} ===".format(alias))
        for pname in args.variants:
            rows = eval_combo(llm, pname, PROMPTS[pname], args)
            for r in rows:
                r["model"] = alias
            all_rows.extend(rows)

            n = len(rows)
            acc = sum(r["correct"] for r in rows) / n
            conf = sum(r["confused"] for r in rows) / n
            rep = sum(r["repetition"] for r in rows) / n
            sec = sum(r["sec"] for r in rows) / n
            print("  {:<16} 정답 {:>4.0f}%  혼동 {:>4.0f}%  반복 {:>5.2f}  평균 {:>5.2f}s".format(
                pname, acc * 100, conf * 100, rep, sec))
        del llm

    out = os.path.join(RESULTS_DIR, "quality.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, ensure_ascii=False, indent=2)
    print("\n[quality] 저장: " + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
