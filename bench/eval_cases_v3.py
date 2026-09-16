#------------------------------------------------------------------
# 확대 평가셋 v3 — 리랭킹 채택 판정을 위한 표본 확대
#=> §27 에서 리랭킹을 204문항으로 평가했더니 McNemar p=0.150 이 나왔다.
#   잃음 11 / 얻음 20(순 +9)으로 방향은 뚜렷했지만 204문항으로는 이 크기의
#   효과를 통계적으로 확정할 수 없었다.
#
#   그래서 사용자가 "D:\분류함 원문을 읽어서 늘릴 수 있는 만큼 늘려 달라"
#   고 요청했다. 목표 560문항(McNemar 검정력 확보에 필요한 대략적 추정치)
#   중 실제로 확보한 것은 v1+v2 204 + 신규 312 = **516문항**이다.
#
#   만드는 방법 — v1/v2 와 다르다
#     ① 자동 테이블 파싱을 먼저 시도했으나(bench/gen_cases_v3.py) 실제
#        생성물을 검수하니 대부분 못 쓸 수준이었다 — Word 변환 잔재
#        (TOC/HYPERLINK)가 많아 헤더-셀 매칭이 자주 틀렸다. 예:
#          "실비(상한액 130) 등급는 무엇인가요?"  ← 셀 위치 오판
#          "직책 작업자는 며칠인가요?" must=["수정일"]  ← 단위 오판
#        전량 폐기했다.
#     ② bench/select_passages.py 로 지문 384개(총 260개 대표문서, 사본
#        정리 후)를 다시 뽑았다(D:\Project\SimpleRAG\results\passages_v3.json,
#        exp7_v3cases\batch_*.json 으로 10개 배치 분할).
#     ③ 10개 서브에이전트가 각자 배치를 **실제로 읽고** 자연스러운
#        한국어 문항을 직접 작성했다(exp7_v3cases\out_*.json). v1/v2 와
#        같은 수동 저작 방식이고, 사람 대신 다수의 독립 판단자가 나눠
#        맡았다는 점만 다르다.
#     ④ bench/validate_cases.py 로 기계 검증(src 존재·must 원문 일치·
#        과다빈출·중복)을 거쳐 통과한 것만 남겼다.
#
#   ⚠️ v1/v2 보다 신뢰도가 한 단계 낮다 — 사람이 직접 481개 문서를 다
#      읽은 것이 아니라 여러 에이전트가 나눠 읽었다. 그래서 tag="v3" 로
#      구분해 둔다. 나중에 이상 결과가 나오면 v3 부터 의심할 수 있게.
#
#   필드는 v1/v2 와 같다.
#------------------------------------------------------------------

import io
import json
import os

from eval_cases_v2 import CASES as _V2

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_NEW = json.load(io.open(os.path.join(_ROOT, "exp7_v3cases", "all_new.json"), encoding="utf-8"))

CASES = _V2 + [dict(c, tag="v3", kind="추출") for c in _NEW]
