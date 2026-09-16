#------------------------------------------------------------------
# 폴더 범위 판정 (설계서 §5-1, 원본의 "M드라이브 경로면 실행" 자리)
#=> 지정한 폴더(하위 폴더 포함) 안을 보고 있을 때만 질문을 받는다.
#   원본 MDriveSearchBox 는 M드라이브 접두 비교였고, 여기서는 설정 폴더 비교다.
#
#   경로 비교에서 조심할 점
#    - 대소문자 : Windows 는 구분하지 않는다
#    - 경계     : "D:\문서" 가 "D:\문서함" 을 포함하면 안 된다 → 구분자까지 붙여 비교
#    - 끝 구분자: "D:\문서\" 와 "D:\문서" 는 같은 곳이다
#------------------------------------------------------------------

import os
import time


#------------------------------------------------------------------
# 경로 정규화
#=> 비교하기 좋은 꼴로 바꾼다: 절대 경로 + normcase(소문자·구분자 통일) + 끝 구분자 제거.
#   UNC 경로(\\서버\공유)도 그대로 다룬다.
#
# -in: path = 폴더 경로(빈 값 가능)
#
# -out: 정규화된 경로. 빈 값이면 ""
# -out: error = 없음 (이상한 경로도 문자열로만 다룬다)
#------------------------------------------------------------------
def norm(path):
    if not path:
        return ""
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:
        return ""
    # 루트(C:\ 등)가 아니면 끝의 구분자를 뗀다
    while len(p) > 3 and p.endswith(("\\", "/")):
        p = p[:-1]
    return p


#------------------------------------------------------------------
# 한 폴더 안인가
#=> 같은 폴더이거나 그 하위면 True. 경계는 구분자까지 붙여 확인한다.
#
# -in: path = 확인할 폴더
# -in: root = 기준 폴더
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def is_under(path, root):
    p, r = norm(path), norm(root)
    if not p or not r:
        return False
    if p == r:
        return True
    return p.startswith(r if r.endswith("\\") else r + "\\")


#------------------------------------------------------------------
# 범위 검사기
#=> 설정의 폴더 목록을 들고 있다가 지금 폴더가 그 안인지 알려 준다.
#   없는 폴더(아직 연결되지 않은 네트워크 드라이브)는 빼 두었다가 주기적으로 다시 본다.
#
# -필드: configured  = 설정에 적힌 폴더 그대로
# -필드: active      = 지금 실제로 존재하는 폴더만
# -필드: recheck_sec = 다시 확인할 주기(초)
#------------------------------------------------------------------
class Scope:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: folders     = 설정의 폴더 목록
    # -in: recheck_min = 없는 폴더를 다시 확인할 주기(분)
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, folders, recheck_min=5):
        self.configured = list(folders or [])
        self.recheck_sec = max(1, int(recheck_min)) * 60
        self.active = []
        self._checked_at = 0.0
        self.refresh(force=True)

    #--------------------------------------------------------------
    # 존재하는 폴더만 다시 추리기
    #=> 네트워크 드라이브가 늦게 붙는 경우가 있어 주기적으로 다시 본다.
    #
    # -in: force = True 면 주기와 상관없이 지금 확인
    # -in: now   = 현재 시각(초). 시험용
    #
    # -out: True = 목록이 달라졌다(트레이 상태를 갱신할 거리가 있다)
    # -out: error = 없음
    #--------------------------------------------------------------
    def refresh(self, force=False, now=None):
        now = time.monotonic() if now is None else now
        if not force and (now - self._checked_at) < self.recheck_sec:
            return False
        self._checked_at = now
        found = [p for p in self.configured if os.path.isdir(p)]
        changed = found != self.active
        self.active = found
        return changed

    #--------------------------------------------------------------
    # 설정이 쓸 수 있는 상태인가
    #=> 폴더가 하나도 없으면 감시만 하고 아무것도 실행하지 않는다.
    #   모델도 올리지 않는다 — 쓸 수 없는 모델에 메모리 2.5GB 를 쓰지 않기 위해서다.
    #
    # -in: 없음
    #
    # -out: True = 동작 가능
    # -out: error = 없음
    #--------------------------------------------------------------
    def usable(self):
        return bool(self.active)

    #--------------------------------------------------------------
    # 이 폴더에서 동작해야 하는가 (핵심)
    #
    # -in: path = 탐색기가 보고 있는 폴더(DOS 경로). None 이면 판정 불가로 본다
    #
    # -out: (True, 맞은 기준 폴더) 또는 (False, 사유)
    # -out: error = 없음
    #--------------------------------------------------------------
    def contains(self, path):
        self.refresh()
        if not self.active:
            return False, "범위 폴더 미설정"
        if not path:
            return False, "폴더를 알 수 없음"
        for root in self.active:
            if is_under(path, root):
                return True, root
        return False, "범위 밖"
