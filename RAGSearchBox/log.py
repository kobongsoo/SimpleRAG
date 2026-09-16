#------------------------------------------------------------------
# 로그 (MDriveSearchBox 의 MLogger 자리)
#=> 상주 프로그램이라 화면에 남는 것이 없으므로, 무엇을 왜 했는지는 전부 파일에 남긴다.
#   파일 위치는 원본과 같은 규칙: %LOCALAPPDATA%\RAGSearchBox\log\
#    - RAGSearchBox.log : 이 프로그램의 판단 기록
#    - worker.log       : SimpleRAG 워커가 stderr 로 뱉는 것(llama.cpp 로그 등)
#
#   DIAG 단계는 파이썬 기본 단계가 아니라 직접 만든다(원본의 [DIAG] 추적 로그와 같은 용도).
#   검색창 판정처럼 초당 여러 번 찍히는 것은 이 단계에서만 남긴다.
#------------------------------------------------------------------

import logging
import logging.handlers
import os

DIAG = 5                      # DEBUG(10) 보다 낮은 자체 단계
logging.addLevelName(DIAG, "DIAG")

_LOG_DIR = None


#------------------------------------------------------------------
# 로그 폴더 경로
#=> %LOCALAPPDATA%\RAGSearchBox\log 를 쓴다. 환경변수가 없으면(드문 경우)
#   사용자 홈 아래로 떨어뜨린다 — 로그를 못 남겨 프로그램이 죽는 일은 없어야 한다.
#
# -in: 없음
#
# -out: 폴더 절대 경로(없으면 만든다)
# -out: error = 폴더를 만들지 못하면 예외 전파
#------------------------------------------------------------------
def log_dir():
    global _LOG_DIR
    if _LOG_DIR is None:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        _LOG_DIR = os.path.join(base, "RAGSearchBox", "log")
        os.makedirs(_LOG_DIR, exist_ok=True)
    return _LOG_DIR


#------------------------------------------------------------------
# 회전 파일 핸들러 만들기
#=> 5MB 씩 3개까지 돌려 쓴다. 상주 프로그램이라 그냥 두면 로그가 끝없이 커진다.
#
# -in: name = 파일 이름(예: "RAGSearchBox.log")
#
# -out: RotatingFileHandler
# -out: error = 파일을 열지 못하면 예외 전파
#------------------------------------------------------------------
def _handler(name):
    h = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir(), name), maxBytes=5 * 1024 * 1024,
        backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    return h


#------------------------------------------------------------------
# 로그 초기화 (프로그램 시작 때 한 번)
#=> 설정의 Level 을 반영해 로거를 만든다. 두 번 불러도 핸들러가 겹치지 않게 지우고 단다.
#
# -in: level = "INFO" | "DEBUG" | "DIAG" (대소문자 무관, 모르는 값이면 INFO)
#
# -out: 없음
# -out: error = 로그 파일을 열지 못하면 예외 전파 (시작 단계에서 알아야 한다)
#------------------------------------------------------------------
def setup(level="INFO"):
    lv = {"DIAG": DIAG, "DEBUG": logging.DEBUG, "INFO": logging.INFO,
          "WARNING": logging.WARNING, "ERROR": logging.ERROR}.get(
              str(level).upper(), logging.INFO)

    root = logging.getLogger("rsb")
    root.handlers.clear()
    root.setLevel(lv)
    root.addHandler(_handler("RAGSearchBox.log"))
    root.propagate = False

    # 워커가 뱉는 것은 양이 많고 성격이 달라 파일을 나눈다
    worker = logging.getLogger("rsbworker")
    worker.handlers.clear()
    worker.setLevel(logging.DEBUG)
    worker.addHandler(_handler("worker.log"))
    worker.propagate = False


#------------------------------------------------------------------
# 로거 얻기
#=> 모듈마다 get("monitor") 처럼 부른다. 로그 줄에 어느 부분인지 남는다.
#
# -in: name = 부분 이름(없으면 최상위)
#
# -out: Logger
# -out: error = 없음
#------------------------------------------------------------------
def get(name=None):
    return logging.getLogger("rsb." + name if name else "rsb")


#------------------------------------------------------------------
# 워커 출력 전용 로거
#=> stderr 로 오는 llama.cpp 로그 등을 worker.log 에만 남긴다.
#
# -in: 없음
#
# -out: Logger
# -out: error = 없음
#------------------------------------------------------------------
def worker_log():
    return logging.getLogger("rsbworker")


#------------------------------------------------------------------
# DIAG 단계 기록 (자주 찍히는 판정 기록용)
#=> logger.diag(...) 가 없으므로 도우미로 둔다.
#
# -in: logger = get() 으로 얻은 로거
# -in: msg    = 형식 문자열
# -in: *args  = 형식 인자
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def diag(logger, msg, *args):
    if logger.isEnabledFor(DIAG):
        logger.log(DIAG, msg, *args)
