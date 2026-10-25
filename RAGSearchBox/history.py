#------------------------------------------------------------------
# 대화 기록 (폴더 패널의 ⋯ 메뉴)
#=> 패널에서 주고받은 대화를 Gemini 처럼 "대화 단위" 로 남겨 두고, ⋯ 메뉴에서 다시 연다.
#   최근 10개만 둔다 — 더 오래된 것은 지운다.
#
#   파일: %LOCALAPPDATA%\RAGSearchBox\history.json (로그 폴더 옆)
#   모양: [{"id", "title", "folder", "updated", "turns": [마디, ...]}, ...]  최근 것이 앞
#   마디: {"q": 질문, "scope": 범위 설명, "evidence": [근거], "answer": 답,
#          "meta": 소요 줄, "raw": 근거 없는 안내, "error": 오류}
#
#   화면과 무관한 순수 함수만 둔다(시험에서 쓴다). 위젯은 chat_panel 이 그린다.
#------------------------------------------------------------------

import io
import json
import os
import time

MAX_CONVS = 10          # 남겨 둘 대화 수
MAX_TURNS = 20          # 대화 하나에 남겨 둘 마디 수(패널 화면 한도와 같다)
TITLE_CHARS = 40        # 메뉴에 보일 제목 길이


#------------------------------------------------------------------
# 기본 기록 파일 경로
#=> 로그 폴더(%LOCALAPPDATA%\RAGSearchBox\log)의 한 단계 위에 둔다.
#
# -in: log_dir = 로그 폴더
#
# -out: history.json 경로
# -out: error = 없음
#------------------------------------------------------------------
def default_path(log_dir):
    return os.path.join(os.path.dirname(os.path.abspath(log_dir)), "history.json")


#------------------------------------------------------------------
# 기록 읽기
#=> 파일이 없거나 깨졌으면 빈 기록으로 시작한다(기록 때문에 패널이 안 뜨면 안 된다).
#
# -in: path = 기록 파일
#
# -out: 대화 목록(최근 것이 앞, 최대 MAX_CONVS)
# -out: error = 없음 (읽기 실패 시 [])
#------------------------------------------------------------------
def load(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    # 모양이 어긋난 항목은 버린다 — 손으로 고친 파일이어도 패널이 죽지 않게
    convs = [c for c in data if isinstance(c, dict) and isinstance(c.get("turns"), list)]
    return convs[:MAX_CONVS]


#------------------------------------------------------------------
# 기록 쓰기
#=> 임시 파일에 먼저 쓰고 바꿔 끼운다 — 쓰다가 꺼져도 예전 기록이 남는다.
#
# -in: path  = 기록 파일
# -in: convs = 대화 목록
#
# -out: True = 저장함
# -out: error = 없음 (쓰기 실패 시 False)
#------------------------------------------------------------------
def save(path, convs):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(convs[:MAX_CONVS], f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


#------------------------------------------------------------------
# 새 대화 만들기
#
# -in: question = 첫 질문(제목이 된다)
# -in: folder   = 그때 보고 있던 폴더
# -in: now      = 지금 시각(초, 시험용). 비우면 실제 시각
#
# -out: 대화 dict (마디는 비어 있다)
# -out: error = 없음
#------------------------------------------------------------------
def new_conv(question, folder, now=None):
    now = time.time() if now is None else now
    title = " ".join((question or "").split())
    if len(title) > TITLE_CHARS:
        title = title[:TITLE_CHARS - 1] + "…"
    # 같은 초에 두 개를 만들어도 겹치지 않게 소수점까지 쓴다
    return {"id": "{:.6f}".format(now), "title": title or "(제목 없음)",
            "folder": folder or "", "updated": now, "turns": []}


#------------------------------------------------------------------
# 대화를 기록 맨 앞에 올리기 (새로 묻거나 답이 끝났을 때)
#=> 같은 id 는 빼고 맨 앞에 넣은 뒤 MAX_CONVS 개로 자른다. 마디도 MAX_TURNS 개로 자른다.
#
# -in: convs = 기록(이 목록을 직접 고친다)
# -in: conv  = 올릴 대화
# -in: now   = 지금 시각(초, 시험용)
#
# -out: 고친 기록(convs 와 같은 목록)
# -out: error = 없음
#------------------------------------------------------------------
def touch(convs, conv, now=None):
    conv["updated"] = time.time() if now is None else now
    del conv["turns"][:-MAX_TURNS]
    convs[:] = [c for c in convs if c.get("id") != conv.get("id")]
    convs.insert(0, conv)
    del convs[MAX_CONVS:]
    return convs


#------------------------------------------------------------------
# 메뉴에 보일 한 줄
#=> "제목 · 10/23 13:40" 처럼 언제 나눈 대화인지 함께 보인다.
#
# -in: conv = 대화
#
# -out: 글자
# -out: error = 없음 (시각이 이상하면 제목만)
#------------------------------------------------------------------
def menu_label(conv):
    try:
        when = time.strftime("%m/%d %H:%M", time.localtime(float(conv.get("updated", 0))))
    except Exception:
        return conv.get("title", "")
    return "{}  ·  {}".format(conv.get("title", ""), when)
