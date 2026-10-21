#------------------------------------------------------------------
# SimpleRAG 전용 구조화 추출기 (plan/구조화추출_청킹_재설계서.html)
#=> 추출 결과를 문자열이 아니라 블록 목록(종류·제목 수준·쪽·표 행렬)으로 만든다.
#   MpowerClassify 의 추출기와 공유하지 않는다 — 필요한 처리는 이식해 여기서 고친다.
#------------------------------------------------------------------

from .blocks import Block, Document          # noqa: F401
from .extract import dump_json, extract_document, supports   # noqa: F401
