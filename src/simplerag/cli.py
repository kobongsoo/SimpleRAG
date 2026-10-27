#------------------------------------------------------------------
# CLI 진입점 (설계서 §7)
#=> index / ask / status / warmup 네 명령.
#
#   ask 는 2단계 응답을 그대로 화면에 옮긴다 — 근거를 먼저 찍고, 그 아래
#   답변을 토큰 단위로 흘린다. 파이프라인이 UI 에 독립적이라 나중에 웹/데스크톱
#   UI 로 바꿔도 이 흐름을 그대로 쓴다.
#------------------------------------------------------------------

import argparse
import json
import os
import sys
import time

# 패키지로 설치하지 않고 실행할 수 있게 src 를 경로에 얹는다.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.abspath(os.path.join(_HERE, ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from simplerag.settings import ConfigError           # noqa: E402

try:
    from simplerag import config                      # noqa: E402
except ConfigError as _e:
    # config.yaml 오타·범위 밖 값 — 스택 대신 무엇을 고칠지만 알린다(§35).
    # 리다이렉트(2> 파일)면 stderr 가 cp949 라 오류문의 '—' 에서 다시 죽는다(§7-2 와 같은 함정)
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.stderr.write("설정 오류: {}\n".format(_e))
    raise SystemExit(2)
from simplerag import console                         # noqa: E402


#------------------------------------------------------------------
# 출력 인코딩 고정 (핵심 — 한국어 Windows 필수)
#=> 출력이 파이프나 파일로 리다이렉트되면 Python 은 콘솔 대신 로케일 인코딩
#   (한국어 Windows 는 cp949)을 쓴다. 그런데 화면 구분선에 쓰는 '—'(U+2014)는
#   cp949 에 없어서 UnicodeEncodeError 로 죽는다.
#
#     UnicodeEncodeError: 'cp949' codec can't encode character '—'
#
#   실제로 exe 로 빌드한 뒤 `simplerag.exe ask ... > out.txt` 에서 터졌다.
#   콘솔에 직접 쓸 때는 WriteConsoleW 를 타서 문제가 없어 발견이 늦었다.
#
#   errors="replace" 까지 걸어, 어떤 문자가 오더라도 출력 때문에 죽지 않게 한다.
#------------------------------------------------------------------
def _force_utf8_output():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass          # 재설정 불가한 스트림이면 그대로 둔다
from simplerag.generate.prompts import clean_answer, recite, short_clean, short_echo_hold, soft_clean  # noqa: E402
from simplerag.index.indexer import index_folder      # noqa: E402
from simplerag.warmup import App                      # noqa: E402
from simplerag.generate import backend as gen_backend   # noqa: E402


#------------------------------------------------------------------
# 진행 안내 출력 (stderr)
#=> 첫 실행 백엔드 측정처럼 오래 걸리는 준비 단계를 알린다. 답변은 stdout 으로
#   나가므로 `ask ... > out.txt` 결과 파일에 섞이지 않게 stderr 로 보낸다.
#
# -in: msg = 출력할 문장
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _log(msg):
    print(msg, file=sys.stderr, flush=True)


#------------------------------------------------------------------
# 백그라운드 리랭커 적재가 끝나면 알리기 (chat 전용)
#=> chat 은 리랭커를 "준비 완료" 뒤로 미뤄 올린다. 시작할 때는 "켬(백그라운드 적재 중)" 이라고만
#   알 수 있어서, RAGSearchBox 정보 창이 그 상태에 머물러 있었다. 끝나는 순간을 한 줄로 알려 준다.
#    1) 작은 데몬 스레드가 app.wait_rerank() 로 적재가 끝나기를 기다린다
#    2) 결과를 stderr 에 "리랭커 적재 완료 (…ms)" 또는 "리랭커 적재 실패 — …" 로 찍는다
#   stdout 은 질문 프롬프트·답변이 오가는 길이라 끼어들면 안 되므로 stderr 로 보낸다.
#
# -in: app = 예열을 마친 App (warmup 결과에 _rerank_deferred 가 있었던 경우에만 부른다)
#
# -out: 없음 (데몬 스레드 시작)
# -out: error = 없음 (기다리다 예외가 나도 로그 한 줄만 남긴다)
#------------------------------------------------------------------
def _announce_rerank(app):
    import threading

    # 백그라운드 본체 — 적재가 끝날 때까지 기다렸다가 결과를 한 줄로 알린다
    def job():
        try:
            res = app.wait_rerank()
        except Exception as e:
            res = "{}: {}".format(type(e).__name__, str(e)[:200])
        # 문자열이면 실패 사유, 숫자(ms)면 성공 — warmup.wait_rerank 의 약속을 그대로 따른다
        if isinstance(res, str):
            _log("리랭커 적재 실패 — {}".format(res))
        else:
            _log("리랭커 적재 완료 ({:.0f}ms)".format(res or 0))

    threading.Thread(target=job, name="rerank-announce", daemon=True).start()


#------------------------------------------------------------------
# 인덱싱 명령
#=> 폴더의 변경된 문서만 다시 인덱싱한다. --rebuild 면 전부 다시.
#------------------------------------------------------------------
def cmd_index(args):
    from simplerag.index import commands as index_commands
    app = App()
    try:
        # 인덱싱에는 생성 모델도 리랭커도 필요 없다 — 괜히 올리지 않는다.
        app.warmup(wait=True, skip_llm=True, rerank=False)
        try:
            summary = index_folder(args.dir, app.embedder, app.store, app.bm25,
                                   rebuild=args.rebuild, on_log=print,
                                   recursive=not args.no_recursive,
                                   allow_delete=args.allow_delete, max_add=args.max_add)
        except RuntimeError as e:
            # 폴더 없음·청킹 설정 불일치 — 사용자가 고칠 수 있는 오류라 스택 없이 알린다
            print("\n❌ {}".format(e), file=sys.stderr)
            print(index_commands.format_result({"op": "index", "ok": False, "why": str(e)[:200]},
                                               prefix="@index-summary "))
            return 1
        # 자동 인덱싱(RAGSearchBox)이 결과를 읽을 한 줄 — 사람이 읽는 요약은 아래에 따로 있다
        print(index_commands.format_result(dict(
            {k: v for k, v in summary.items() if k != "failed"},
            op="index", ok=True, failed=len(summary.get("failed") or [])),
            prefix="@index-summary "))
        # 이동(하위 폴더로 옮김)은 경로만 바꾸고, 복사(붙여 넣은 같은 파일)는 원본 벡터를 베낀 건수다
        print("\n문서 {}건 / 추가 {}건 · 수정 {}건 · 내용 같음 {}건 · 이동 {}건 · 복사 {}건 · 삭제 {}건"
              " / 청크 {:,}개 / {:.1f}s"
              .format(summary["files"], summary["added"], summary["modified"], summary["same"],
                      summary.get("moved", 0), summary.get("copied", 0), summary["removed"],
                      summary["chunks"], summary["sec"]))
        if summary.get("failed"):
            print("실패 {}건 (실패한 문서는 옛 버전이 그대로 남아 있습니다)".format(
                len(summary["failed"])))
        if summary.get("delete_blocked"):
            # 지우지 않고 멈춘 것은 따로 알린다 — 스크립트가 조용히 넘어가지 않게 종료 코드 3
            print("⚠️ 삭제를 멈췄습니다: {}. 정말 지운 것이면 --allow-delete 를 붙여 다시 실행하세요."
                  .format(summary["delete_blocked"]), file=sys.stderr)
            return 3
        return 0
    finally:
        app.close()


#------------------------------------------------------------------
# 질의 명령 (핵심) — 2단계 출력
#=> ① 근거를 즉시 출력  ② 답변을 스트리밍  ③ 타이밍 요약
#------------------------------------------------------------------
def cmd_ask(args):
    # 질문을 생략하면 대화형으로 들어간다 — 여러 개 물어볼 때 예열을 아낀다.
    if not args.query:
        return cmd_chat(args)

    app = App(_pick_model(args))
    try:
        app.warmup(wait=True, on_log=_log)
        # 1회용 질의는 준비 직후 바로 묻는다. 백그라운드 리랭커 적재(GIL 점유)와 검색이 겹치면
        # 총시간은 같은데 첫 글자 시간만 늘어 보인다(§34) — 미룰 이득이 없으니 기다렸다 묻는다.
        app.wait_rerank()
        ok = render_answer(app, args.query, top_k=args.top_k,
                           max_tokens=args.max_tokens,
                           stream=not args.no_stream,
                           folder=getattr(args, "folder", None))
        return 0 if ok else 130
    finally:
        app.close()


#------------------------------------------------------------------
# 스트리밍 출력 버퍼
#=> 토큰마다 write+flush 하면 답변 77자에 콘솔 쓰기가 52번 발생한다(평균 1.5자).
#   이렇게 잦은 소량 쓰기는 Windows 콘솔에서 렌더링이 깨지는 원인이 될 수 있다
#   (출력이 안 보이다가 드래그하면 나타나는 증상).
#   개행이 오거나 일정 길이·시간이 지날 때만 내보내 쓰기 횟수를 크게 줄인다.
#   체감 스트리밍은 유지된다 — 최소 interval 마다는 반드시 내보내기 때문.
#
# -in: min_chars = 이만큼 모이면 내보낸다
# -in: interval  = 이 시간이 지나면 길이와 무관하게 내보낸다(초)
# -in: hold      = 끝에서 붙들어 두는 글자 수(아래 설명)
# -in: cleaner   = 본문 정리 함수(되뱉기 제거) — 주면 내보내기 전에 걸리는 곳이 없는지 본다
# -in: soft      = 내보낼 조각에 바로 적용할 정리(굵게 별표·없는 인용 번호) — 공백은 건드리지 않는 것
#------------------------------------------------------------------
class StreamWriter:
    #--------------------------------------------------------------
    # hold — 꼬리 보류 구간 (지시문 되뱉기 제거용)
    #=> 0.6B 는 답변 끝에 시스템 지시문을 그대로 옮겨 적는 일이 있다
    #   ("… 지급합니다. [1] 근거 번호 [1] 형식으로 표기합니다.").
    #   스트리밍은 토큰을 받는 즉시 찍기 때문에, 다 만든 뒤 지워 봐야
    #   화면에는 이미 나간 뒤다. 그래서 **끝에서 hold 글자만큼을 붙들어 둔다.**
    #
    #   예전에는 90자를 붙들어, 답의 첫 90자가 모일 때까지 화면이 비어 있었다
    #   (1.7B 에서 워커 TTFT 보다 화면 첫 글자가 약 3.5초 늦었다 — 골든셋 100문항 실측).
    #   지금은 아래 셋으로 같은 걸러 내기를 지키면서 덜 붙든다.
    #    1) 붙드는 길이 56자 — 되뱉기 패턴 가운데 가장 긴 "…(40자) 바탕으로 답변하겠습니"
    #       (아직 안 끝난 꼴)가 52자다. 이보다 앞의 글자는 앞으로 올 되뱉기에 걸릴 수 없다.
    #    2) 문장 끝(. ! ? 뒤 공백, 줄바꿈)까지는 바로 내보낸다 — 되뱉기 패턴은 문장부호·줄바꿈을
    #       넘지 않으므로 끝난 문장은 뒤에 무엇이 와도 걸리지 않는다. 뒤따르는 인용 번호는
    #       "[1] 형식으로 표기" 되뱉기의 머리일 수 있어 붙든다.
    #    3) 내보낼 조각까지 정리해 봐서 글자가 바뀌면(되뱉기·머리 표식 등) 내보내지 않고
    #       마무리(finish)에 맡긴다. 굵게 별표·없는 인용 번호는 soft 로 걷어 내고 내보낸다.
    #--------------------------------------------------------------
    #--------------------------------------------------------------
    # short — 짧은 되뱉기 거르기 (설계서 ②③, 되뱉기 거르기 끔일 때 render_answer 가 켠다)
    #=> 긴 되뱉기 대신 "근거 [1]" · "[1] 형식으로 표기" 같은 짧은 잡음과 인용 덩어리 중복만 거른다.
    #   받아 둔 글에서 다 온 것은 지우고(short_clean), 끝이 그 앞부분일 수 있을 때만 그 꼬리
    #   (보통 2~6자, 최대 40자)를 붙든다 — 그렇지 않으면 바로 내보내 첫 글자가 늦지 않는다.
    #--------------------------------------------------------------
    #--------------------------------------------------------------
    # evidence — 인용 번호 바로잡기(설계서 ⑤)용 근거 본문 목록. render_answer 가 근거 사건에서 채운다.
    #   문장 끝 뒤의 번호("…다. [1]")는 문장이 다 쓰인 뒤에 오므로 내보내기 직전에 바로잡을 수 있다.
    #--------------------------------------------------------------
    def __init__(self, min_chars=40, interval=0.06, hold=56, cleaner=None, soft=None, short=False):
        self.min_chars = min_chars
        self.interval = interval
        self.hold = hold
        self.cleaner = cleaner
        self.soft = soft
        self.short = short
        self.evidence = None
        self._final = False      # finish() 이후로는 꼬리를 붙들지 않는다
        self._pending = ""       # 아직 화면에 안 나간 부분
        self._emitted = ""       # 이미 화면에 나간 부분
        self._n = 0
        self._last = time.perf_counter()
        self.writes = 0

    def write(self, text):
        self._pending += text
        self._n += len(text)
        now = time.perf_counter()
        if ("\n" in text or self._n >= self.min_chars
                or now - self._last >= self.interval):
            self.flush()

    #--------------------------------------------------------------
    # 꼬리를 남기고 내보내기
    #=> 보류 구간(hold)과 아직 안 끝난 문장의 끝부분은 남겨 둔다. 마지막 정리는 finish() 가 한다.
    #    1) 내보낼 길이 = max(끝에서 hold 를 뺀 곳, 마지막 문장 끝)
    #    2) 짝이 안 맞는 굵게 별표 "**" · 닫히지 않은 "[" 앞에서 끊는다(나중에 짝이 올 수 있다)
    #    3) cleaner 로 정리해 봐서 글자가 바뀌면 이번에는 내보내지 않는다
    #
    # -in: 없음
    # -out: 없음 (화면에 쓴다)
    # -out: error = 없음
    #--------------------------------------------------------------
    def flush(self):
        if self._final:
            out = self._pending
            self._pending = ""
        else:
            if self.short:
                # 아직 화면에 안 나간 글에서 다 온 짧은 되뱉기는 미리 지운다(끝맺음이 와야 지우는 판)
                self._pending = short_clean(self._pending, final=False)
            out = self._ready_part()
            if not out:
                return
            self._pending = self._pending[len(out):]
            if self.soft:
                out = self.soft(out)
            if self.evidence:
                out = self._recite_out(out)
        if not out:
            return
        self._emitted += out
        sys.stdout.write(out)
        sys.stdout.flush()
        self._n = 0
        self._last = time.perf_counter()
        self.writes += 1

    #--------------------------------------------------------------
    # 내보낼 조각의 문장 끝 뒤 인용 번호 바로잡기
    #=> 이미 나간 글 + 이 조각 + 아직 붙든 글을 함께 보고(뒤에 낱말이 오는지 알아야 문장 앞 번호와
    #   가릴 수 있다) 이 조각 안의 번호만 고친다. 글자 수가 바뀌면(두 자리 번호 등) 고치지 않는다.
    #
    # -in: out = 내보낼 조각(정리 뒤)
    #
    # -out: 바로잡은 조각
    # -out: error = 없음
    #--------------------------------------------------------------
    def _recite_out(self, out):
        head = self._emitted
        full = head + out + self._pending
        fixed, fixes = recite(full, self.evidence, trailing_only=True, start=len(head))
        if not fixes or len(fixed) != len(full):
            return out
        return fixed[len(head):len(head) + len(out)]

    #--------------------------------------------------------------
    # 마무리 — 되뱉기를 지운 뒤 남은 꼬리를 내보낸다
    #=> 이미 화면에 나간 부분은 되돌릴 수 없다. 그래서 정리 결과가 그 부분과
    #   이어질 때만 나머지를 붙이고, 아니면 꼬리만 따로 손본다.
    #
    #   🔴 예전에는 `cleaned.startswith(_emitted)` 로 판정했는데, 이것이
    #      **거의 항상 거짓**이었다. 정리 함수가 공백을 한 칸으로 접기 때문에
    #      (`\\s+` → `" "`), 이미 나간 부분에 줄바꿈이나 두 칸 띄기가 하나라도
    #      있으면 글자는 같은데 공백이 달라 판정이 실패한다. 모델은 근거마다
    #      줄을 바꾸므로 사실상 모든 여러 줄 답변에서 정리가 조용히 건너뛰어졌다.
    #      (REPORT §18 은 이 경로가 동작한다고 적었으나 한 줄 답변에서만 맞다.)
    #      → 공백을 무시하고 **글자만으로** 이어지는지 본다.
    #
    #   앞부분에서 글자 자체가 지워진 경우(예: 이미 나간 `[12]` 인용 제거)는
    #   되돌릴 방법이 없다. 이때는 손댈 수 있는 꼬리만 정리한다 — 보류 구간이
    #   90자이고 관측된 되뱉기가 30자 이내라 되뱉기는 꼬리 안에 들어온다.
    #
    #   final(생성기가 정한 '남길 답')을 주면 받은 글 대신 그것을 기준으로 삼는다.
    #   생성기가 반복·상한으로 잘라 낸 꼬리(반복 문장의 첫머리 등)가 아직 여기 붙들려
    #   있으면 화면에 내보내지 않고 버린다. 이미 나간 뒤라면 더 붙이지 않는다.
    #
    # -in: cleaner = 본문 정리 함수(없으면 그대로)
    # -in: final   = 남길 답(없으면 받은 글 전체)
    #--------------------------------------------------------------
    def finish(self, cleaner=None, final=None):
        self._final = True
        full = final if final is not None else self._emitted + self._pending
        cleaned = cleaner(full) if cleaner else full

        tail = self._tail_after(cleaned)
        if tail is None and final is not None and _chars(self._emitted).startswith(_chars(cleaned)):
            # 남길 답보다 이미 더 나갔다(잘라 낸 꼬리가 나간 뒤) — 되돌릴 수 없으니 더 붙이지 않는다
            tail = ""
        if tail is None and cleaner:
            tail = cleaner(self._pending)
        if tail is not None:
            self._pending = tail
        self.flush()

    #--------------------------------------------------------------
    # 지금 내보내도 되는 앞부분 (flush 의 1~3)
    #
    # -in: 없음
    # -out: 내보낼 원문 조각("" 이면 아직 없음)
    # -out: error = 없음
    #--------------------------------------------------------------
    def _ready_part(self):
        p = self._pending
        if self.short:
            # 짧은 되뱉기의 앞부분일 수 있는 꼬리만 붙든다(없으면 전부 내보낸다)
            cut = len(p) - short_echo_hold(p)
        else:
            cut = max(len(p) - self.hold, _last_sentence_end(p))
        out = p[:max(cut, 0)]
        # 끝의 "*" 는 붙든다 — 모델이 "**" 를 "*" 두 토큰으로 내면 짝 세기가 어긋난다(골든셋 q025)
        out = out.rstrip("*")
        # 짝이 안 맞는 "**" 가 있으면 그 앞까지 — 닫는 별표가 오면 함께 걷어 낸다
        if out.count("**") % 2:
            out = out[:out.rfind("**")]
        # 닫히지 않은 "[" 는 인용 번호의 앞부분일 수 있다
        b = out.rfind("[")
        if b >= 0 and "]" not in out[b:]:
            out = out[:b]
        if not out or not self.cleaner:
            return out
        soft = self.soft or (lambda t: t)
        need = _chars(self._emitted + soft(out))
        # 받아 둔 글 전체를 정리해 보고 내보낼 앞부분이 그대로인지 본다 — 뒤를 봐야 판정되는
        #   되뱉기("근거 [1]" 의 '근거' 는 뒤에 [1] 이 와야 지운다, 골든셋 q096)도 놓치지 않게
        if not _chars(self.cleaner(self._emitted + soft(p))).startswith(need):
            return ""
        return out

    #--------------------------------------------------------------
    # 이미 나간 부분을 건너뛴 나머지 구하기
    #=> 공백은 세지 않고 글자만 맞춰 본다. 정리 함수가 공백을 접어도
    #   글자가 그대로면 이어 붙일 수 있다.
    #
    # -in: cleaned = 정리된 전체 본문
    #
    # -out: 아직 안 나간 부분. 앞부분 글자가 달라졌으면 None
    #--------------------------------------------------------------
    def _tail_after(self, cleaned):
        need = [c for c in self._emitted if not c.isspace()]
        i = k = 0
        while k < len(need) and i < len(cleaned):
            if not cleaned[i].isspace():
                if cleaned[i] != need[k]:
                    return None       # 이미 나간 자리의 글자가 바뀌었다
                k += 1
            i += 1
        return None if k < len(need) else cleaned[i:]


#------------------------------------------------------------------
# 마지막 문장 끝 — 그 앞의 글은 되뱉기 패턴에 걸릴 수 없다
#=> '.' '!' '?' 바로 뒤에 공백이 온 자리, 또는 줄바꿈 바로 뒤.
#   소수점("3.5")·글 끝의 마침표는 문장 끝으로 보지 않는다(다음 글자를 봐야 안다).
#
# -in: s = 글
#
# -out: 위치(없으면 0)
# -out: error = 없음
#------------------------------------------------------------------
def _last_sentence_end(s):
    for i in range(len(s) - 1, -1, -1):
        c = s[i]
        if c == "\n":
            return i + 1
        if c in ".!?" and i + 1 < len(s) and s[i + 1].isspace():
            return i + 1
    return 0


# 공백을 뺀 글자들 — 정리 함수가 공백을 접어도 같은 글로 보려고
def _chars(s):
    return "".join(c for c in s if not c.isspace())


#------------------------------------------------------------------
# 답변 설정 한 줄 — 소요 블록에 "어떤 모델·근거 방식으로 만든 답인가"를 적는다
#=> 같은 질문이라도 모델(--model)과 parent 설정에 따라 답이 달라진다. 화면만 보고도
#   알 수 있게 소요 블록에 한 줄로 남긴다.
#    1) 답변모델 — 이번 명령에서 실제로 쓰는 생성기 별칭과 모드 이름(빠름/정밀/한국어)
#    2) parent — off 또는 section(상한 N토큰)
#   형식은 RAGSearchBox chat_parser.RE_SETUP 과 맞춰 둔다(바꾸면 양쪽 함께).
#
# -in: app = 실행 중인 App (pipeline.generator.alias 를 읽는다)
#
# -out: str = 예) "답변모델 qwen3-1.7b-q4 (정밀) · parent off"
# -out: error = 없음 (생성기 별칭을 못 읽으면 설정 기본 모델로 적는다)
#------------------------------------------------------------------
def _answer_setup_line(app):
    gen = getattr(getattr(app, "pipeline", None), "generator", None)
    # --model / --precise 로 바꾼 경우까지 반영하려고 생성기에 실제로 붙은 별칭을 쓴다
    alias = getattr(gen, "alias", None) or config.DEFAULT_GEN_MODEL
    parent = config.PARENT_MODE
    if parent == "section":
        parent = "section(상한 {}토큰)".format(config.PARENT_MAX_TOKENS)
    return "답변모델 {} ({}) · parent {}".format(alias, _model_mode(alias), parent)


#------------------------------------------------------------------
# 질의 1건 화면 출력 (ask / chat 공용)
#=> 2단계 응답을 그대로 화면에 옮긴다 — 근거를 먼저 찍고 답변을 흘린다.
#   생성 중 Ctrl+C 는 그 답변만 취소하고 호출자에게 알린다(앱은 안 죽는다).
#
# -in: app, query, top_k, max_tokens, show_timing
# -in: stream = False 면 답변을 다 만든 뒤 한 번에 출력(콘솔 렌더링 문제 회피용)
#
# -out: True = 정상 완료, False = 사용자가 중단
#------------------------------------------------------------------
def render_answer(app, query, top_k=None, max_tokens=None, show_timing=True,
                  stream=True, folder=None):
    from simplerag.pipeline import NO_DOCS_IN_FOLDER, NO_RELEVANT
    parts = []
    # 근거 개수. evidence 이벤트에서 정해지고, 마무리 정리 때 쓰인다 —
    # 없는 번호를 인용으로 찍는 결함을 막는다(prompts.drop_invalid_cites).
    n_ev = [None]

    # 붙들어 둔 꼬리를 마무리할 때 쓰는 정리기. 호출 시점의 n_ev 를 본다.
    #   되뱉기 거르기를 끄면(기본) 되뱉기 패턴만 건너뛰고 별표·인용 번호·공백 정리는 그대로 한다.
    def clean(text):
        return clean_answer(text, n_ev[0], echo=config.ECHO_FILTER)

    # 내보내는 조각에 바로 적용하는 정리 — 굵게 별표·없는 인용 번호(공백은 그대로)
    def soft(text):
        return soft_clean(text, n_ev[0])

    if config.ECHO_FILTER:
        # 켬: 정리기를 함께 넘겨 내보내기 전에 되뱉기에 걸리는지 본다 — 첫 문장(최대 56자)을
        #   붙들어 화면 첫 글자가 약 2초 늦어진다
        writer = StreamWriter(cleaner=clean, soft=soft)
    else:
        # 끔(기본): 꼬리를 붙들지 않고 바로 내보낸다. 짧은 되뱉기·인용 덩어리의 앞부분일 수 있는
        #   꼬리(보통 2~6자)와 짝 없는 "**" · 닫히지 않은 "[" 만 잠깐 붙든다
        writer = StreamWriter(hold=0, soft=soft, short=True)

    try:
        for event in app.pipeline.answer(query, top_k=top_k, max_tokens=max_tokens,
                                         folder=folder):
            kind = event[0]

            if kind == "evidence":
                chunks, timing = event[1], event[2]
                n_ev[0] = len(chunks)
                # 문장 끝 뒤 인용 번호를 내보내기 전에 바로잡으려고 근거 본문을 넘긴다(설계서 ⑤)
                writer.evidence = [c["text"] for c in chunks]
                if folder and timing.get("scope_docs") == 0:
                    # 폴더에 인덱싱된 문서가 없다 — 근거 머리 없이 안내만 낸다.
                    # (RAGSearchBox 는 근거 머리가 없는 출력을 그대로 패널에 보여 준다)
                    print("\n" + NO_DOCS_IN_FOLDER + "\n")
                    return True
                if timing.get("no_relevant"):
                    # 관련 근거가 없다 — 근거도 보이지 않고 답할 수 없다고만 알린다
                    print("\n" + NO_RELEVANT + "\n")
                    return True
                # 폴더 한정이면 머리에 범위를 덧붙인다 — 나중에 보아도 어느 범위의 답인지 안다
                scope = ""
                if folder:
                    scope = " · 범위 {}(문서 {}건)".format(
                        os.path.basename(folder.rstrip("\\/")) or folder, timing.get("scope_docs", "?"))
                print("\n── 근거 {}건 ({:.0f}ms){} ─────────────────────".format(
                    len(chunks), timing["total_ms"], scope))
                # RAGSearchBox 가 근거 [...] 팝업에 전체 본문을 보여 주려고 켠다(콘솔에서는 꺼져 있다)
                full = os.environ.get("SIMPLERAG_EVIDENCE_FULL") == "1"
                for i, c in enumerate(chunks, 1):
                    body = c["text"].replace("\n", " ").strip()
                    print("  [{}] {}".format(i, c["doc_name"]))
                    print("      {}{}".format(body[:160], "…" if len(body) > 160 else ""))
                    if full:
                        # 한 줄 ASCII JSON — 줄바꿈·cp949 로 못 쓰는 글자가 있어도 깨지지 않는다
                        print("  @ev " + json.dumps({"no": i, "path": c.get("doc_path", ""),
                                                     "text": c["text"]}, ensure_ascii=True))
                print("\n── 답변 (AI 요약 — 위 근거로 확인하세요) ──")

            elif kind == "token":
                if stream:
                    writer.write(event[1])
                else:
                    parts.append(event[1])       # 다 모아서 마지막에 한 번에

            elif kind == "done":
                d = event[1]
                if stream:
                    # 붙들어 둔 꼬리에서 지시문 되뱉기를 걷어내고 마무리한다.
                    #   생성기가 정한 남길 답(d["answer"])을 기준으로 — 잘라 낸 반복 첫머리가 나가지 않게
                    #   문장 앞 번호는 이미 나갔으니 문장 끝 뒤 번호만 고친 답(answer_stream)으로 맞춘다
                    writer.finish(clean, final=d.get("answer_stream", d.get("answer")))
                else:
                    # 비스트리밍은 파이프라인이 이미 정리한 본문을 그대로 쓴다.
                    print(d["answer"].strip(), end="")
                # 반복·상한으로 잘라 냈으면 본문 뒤에 표시(인용 번호 정리가 끝난 뒤라 따로 붙인다)
                if d.get("note"):
                    print(" " + d["note"], end="")
                if not stream:
                    print()

                t = d["timing"]
                if show_timing:
                    print("\n\n── 소요 ─────────────────────────────────")
                    rr = " + 리랭킹 {:.0f}".format(t["rerank_ms"]) if "rerank_ms" in t else ""
                    print("  검색 {:.0f}ms (임베딩 {:.0f} + dense {:.0f} + BM25 {:.0f}{})".format(
                        t["total_ms"], t["embed_ms"], t["dense_ms"], t["bm25_ms"], rr))
                    if "rerank_error" in t:
                        print("  ⚠️ 리랭킹 실패 — RRF 순서로 답했습니다: {}".format(
                            t["rerank_error"]))
                    print("  첫 글자 {:.2f}s / 완료 {:.2f}s".format(
                        d["ttft_s"], d["total_s"]))
                    if t.get("synonyms"):
                        # 동의어 사전으로 키워드 검색을 넓혔으면 무엇을 넓혔는지(동의어 설계서 §5)
                        print("  검색어 넓힘: {}".format(", ".join(t["synonyms"])))
                    # 어떤 모델·근거 방식으로 만든 답인지 함께 남긴다(RAGSearchBox chat_parser 가 읽는다)
                    print("  " + _answer_setup_line(app))
                    if d["cited"]:
                        # 바로잡은 번호가 있으면 무엇을 바꿨는지 함께 — 본문에 이미 나간 문장 앞 번호는
                        #   못 고쳤을 수 있어 이 줄이 기준이다(설계서 ⑤)
                        fx = ", ".join("[{}]→[{}]".format(a, b) for a, b in d.get("cite_fixes") or [])
                        print("  인용 근거: {}{}".format(
                            ", ".join("[{}]".format(n) for n in d["cited"]),
                            " (인용 번호 바로잡음: {})".format(fx) if fx else ""))
                else:
                    print()
    except KeyboardInterrupt:
        writer.finish(clean)
        print("\n\n[중단됨]")
        return False
    finally:
        # 예외로 빠져나온 경우에도 붙들어 둔 꼬리를 버리지 않는다.
        writer.finish(clean)
        # llama.cpp 가 콘솔 텍스트 속성을 바꾸는 경우에 대비한 방어(관측된 환경에서는
        # 바뀌지 않았으나, 다른 콘솔/버전에서 발생할 수 있어 유지한다).
        console.restore()
    return True


#------------------------------------------------------------------
# 대화형 명령 (핵심)
#=> `ask` 는 1회용이라 질문마다 모델을 다시 올린다(약 4.5초). 여러 개를 물어볼
#   때는 이 모드를 쓴다 — 모델을 한 번만 올리고 계속 질문한다.
#   두 번째 질문부터는 예열 비용 없이 바로 검색·생성으로 들어간다.
#------------------------------------------------------------------
def cmd_chat(args):
    model = _pick_model(args)
    app = App(model)
    top_k = args.top_k
    rr_warned = False        # 백그라운드 리랭커 적재 실패를 한 번만 알리기 위한 표시
    from simplerag.index import commands as index_commands
    indexer_cmds = None      # 인덱싱 명령 처리기 — 처음 쓸 때 만든다(추출기 적재가 무겁다)
    chat_folder = None       # /folder 로 정한 검색 범위(폴더 한정 검색). None = 인덱스 전체

    try:
        print("모델 적재 중...")
        warm = app.warmup(wait=True, on_log=_log)
        stats = app.store.stats()
        alias = model or config.DEFAULT_GEN_MODEL
        print("준비 완료 ({:.1f}초) — {:,}청크 / {} ({} 모드)".format(
            warm["_total_ms"] / 1000, stats["count"],
            config.GEN_MODELS[alias], _model_mode(alias)))
        print("생성 {} / 리랭킹 {}".format(
            gen_backend.label(warm.get("_backend")),
            ("켬(백그라운드 적재 중)" if warm.get("_rerank_deferred") else "켬")
            if warm.get("_rerank") else "끔"))
        # 백그라운드로 올리는 중이면, 끝났을 때 RAGSearchBox 가 알 수 있게 stderr 로 한 줄 알린다
        if warm.get("_rerank") and warm.get("_rerank_deferred"):
            _announce_rerank(app)
        # RAGSearchBox 정보 창이 임베딩·리랭커 모델 이름을 보여 주려고 이 줄을 읽는다(폴더 이름 = 모델 이름)
        print("모델: 임베딩 {} / 리랭커 {}".format(
            os.path.basename(config.EMBED.model_dir.rstrip("\\/")),
            os.path.basename(config.RERANK_DIR.rstrip("\\/"))))
        # RAGSearchBox 가 이 줄로 워커가 아는 명령을 알아본다(옛 워커와 구분)
        print("기능: ask-folder index-commands")
        print("질문을 입력하세요.  종료: exit 또는 Ctrl+D   도움말: /help\n")

        while True:
            try:
                query = input("질문> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not query:
                continue
            if query.lower() in ("exit", "quit", "종료", "/exit", "/quit"):
                break

            # /ask {"q": …, "folder": …} — 폴더 한정 질문 한 줄(RAGSearchBox 폴더 패널).
            # 질문을 ASCII JSON 으로 받으므로 cp949 로 못 쓰는 글자도 깨지지 않는다.
            ask_folder = chat_folder
            if query.startswith("/ask ") or query == "/ask":
                try:
                    req = json.loads(query[4:].strip() or "{}")
                    query = str(req.get("q", "")).strip()
                    ask_folder = req.get("folder") or None
                except (ValueError, AttributeError):
                    print("  /ask 형식이 잘못됐습니다: /ask {\"q\": \"질문\", \"folder\": \"폴더\"}\n")
                    continue
                if not query:
                    continue

            # 슬래시 명령 — 모델을 다시 올리지 않고 설정만 바꾼다.
            elif query.startswith("/"):
                parts = query.split()
                cmd = parts[0].lower()
                if cmd == "/help":
                    print("  /topk N     근거 개수 변경 (현재 {})".format(
                        top_k or config.TOP_K))
                    print("  /status     인덱스 상태")
                    print("  /folder <폴더>       이후 질문은 그 폴더(하위 포함) 문서에서만 찾기. 비우면 해제")
                    print("  /index-doc <경로>    문서 한 건 반영(추가·수정)")
                    print("  /remove-doc <경로>   지운 문서를 인덱스에서 빼기")
                    print("  /bm25                키워드 색인 다시 만들기")
                    print("  /index-plan <폴더>   폴더와 인덱스 비교(바꾸지 않음)")
                    print("  /suggest-synonyms    놓친 질문으로 동의어 사전 후보 뽑기")
                    print("  exit        종료\n")
                elif cmd == "/topk" and len(parts) > 1 and parts[1].isdigit():
                    top_k = int(parts[1])
                    print("  근거 개수를 {}개로 변경했습니다.\n".format(top_k))
                elif cmd == "/status":
                    s = app.store.stats()
                    print("  {:,}청크 / {:.1f}MB\n".format(s["count"], s["disk_mb"]))
                elif cmd == "/folder":
                    arg = query[len(parts[0]):].strip().strip('"')
                    chat_folder = arg or None
                    print("  검색 범위: {}\n".format(chat_folder or "인덱스 전체"))
                elif cmd == "/suggest-synonyms":
                    # 동의어 후보 만들기(동의어 설계서 4단계) — 인덱스를 쥔 이 워커가 올려 둔 모델로 직접 한다.
                    #   명령 시간 제한(RAGSearchBox 300초) 안에 끝나게 최근 질문 WORKER_RECENT 개만 본다
                    from simplerag.retrieve import synonym_suggest as ss
                    summary, _ = ss.run(app.retriever, llm_complete=app.pipeline.generator.complete,
                                        recent=ss.WORKER_RECENT)
                    print(index_commands.format_result(summary), flush=True)
                elif cmd in index_commands.COMMANDS:
                    # 자동 인덱싱(설계서 §9) — 결과는 "@index {json}" 한 줄.
                    # 인덱스를 쥔 이 워커가 직접 반영하므로 `index` 를 따로 돌릴 필요가 없다.
                    if indexer_cmds is None:
                        indexer_cmds = index_commands.IndexCommands(app)
                    arg = query[len(parts[0]):].strip()
                    print(index_commands.format_result(indexer_cmds.run(cmd, arg)), flush=True)
                else:
                    print("  알 수 없는 명령입니다. /help 를 입력하세요.\n")
                continue

            render_answer(app, query, top_k=top_k, max_tokens=args.max_tokens,
                          stream=not args.no_stream, folder=ask_folder)
            print()
            # 백그라운드 리랭커 적재가 실패했으면 한 번만 알린다(이후 질의는 RRF 순서)
            if isinstance(app.rerank_load, str) and not rr_warned:
                print("  ⚠️ 리랭커 적재 실패 — 리랭킹 없이 답합니다: {}\n".format(app.rerank_load))
                rr_warned = True

        print("종료합니다.")
        return 0
    finally:
        app.close()


#------------------------------------------------------------------
# 추출 미리보기 명령
#=> 인덱싱 '전에' 문서에서 텍스트가 제대로 나오는지 눈으로 확인한다.
#   인덱싱은 오래 걸리므로, 새 포맷·새 문서군을 넣기 전에 이걸로 먼저 본다.
#   표가 있는 문서는 `| a | b |` 형태로 복원됐는지 확인할 것.
#------------------------------------------------------------------
def cmd_extract(args):
    from simplerag.extract import build_extractor, extract_text

    if not os.path.isfile(args.path):
        print("파일 없음: " + args.path, file=sys.stderr)
        return 2

    if getattr(args, "blocks", False):
        return _extract_blocks_preview(args)

    ext = build_extractor()
    try:
        text, n_rows = extract_text(ext, args.path)
    except Exception as e:
        print("추출 실패: {}: {}".format(type(e).__name__, e), file=sys.stderr)
        return 1

    if not text:
        print("추출 텍스트 없음 — 스캔 이미지 PDF 이거나 지원하지 않는 포맷입니다.")
        return 1

    print("파일    : {}".format(os.path.basename(args.path)))
    print("문자수  : {:,}".format(len(text)))
    print("표 복원 : {}행".format(n_rows) if n_rows else "표 복원 : 없음")

    # 청크 수까지 보여 주면 인덱싱 결과를 미리 가늠할 수 있다.
    from simplerag.chunk import split_text
    from simplerag.embed.onnx_embedder import OnnxEmbedder
    emb = OnnxEmbedder()
    emb.ensure_loaded()
    chunks = split_text(text, emb.tokenizer)
    print("청크수  : {}개 ({}토큰 기준)".format(len(chunks), config.CHUNK_TOKENS))
    print("-" * 66)
    print(text[:args.chars])
    if len(text) > args.chars:
        print("\n... (이하 {:,}자 생략)".format(len(text) - args.chars))
    return 0


#------------------------------------------------------------------
# 구조화 추출 미리보기 (extract --blocks)
#=> blocks 엔진이 문서를 어떻게 나눴는지 본다 — 블록 종류별 개수, 제목 경로(절),
#   그리고 실제 청크 몇 개. 설정의 extract.engine 과 상관없이 blocks 엔진으로 돌린다.
#    1) docmodel 로 블록을 만든다(파서가 없으면 기존 추출기로 폴백 — 그렇다고 표시)
#    2) 블록 통계와 절 목록(경로 · 쪽 · 블록 수)을 찍는다
#    3) split_blocks 로 청크를 만들어 개수·길이 분포와 앞부분을 보여 준다
#
# -in: args = argparse 결과 (path, chars, dump)
#
# -out: int = 종료 코드(0 성공, 1 실패)
# -out: error = 없음 (실패는 메시지와 종료 코드로)
#------------------------------------------------------------------
def _extract_blocks_preview(args):
    import collections

    from simplerag.chunk import _tlen, split_blocks
    from simplerag.docmodel import dump_json, extract_document
    from simplerag.docmodel.assemble import build_sections
    from simplerag.embed.onnx_embedder import OnnxEmbedder
    from simplerag.extract import build_extractor, extract_text

    ext = build_extractor()
    try:
        # 파서가 없는 포맷은 기존 추출기로 폴백 — 경로 줄에 그렇다고 표시된다
        doc = extract_document(args.path, legacy=lambda p: extract_text(ext, p))
    except Exception as e:
        print("추출 실패: {}: {}".format(type(e).__name__, e), file=sys.stderr)
        return 1

    print("파일    : {}".format(os.path.basename(args.path)))
    print("경로    : {}".format("블록 (" + doc.format + ")" if doc.is_blocks
                                  else "폴백 — 기존 추출 + 정규식 절 추측"))
    for w in doc.warnings:
        print("경고    : " + w)
    if args.dump:
        print("블록JSON: " + dump_json(doc, args.dump, os.path.basename(args.path)))
    if doc.is_blocks:
        kinds = collections.Counter("{}/{}".format(b.type, b.source) for b in doc.blocks)
        print("블록    : {}개  {}".format(len(doc.blocks), dict(kinds)))
        secs = build_sections(doc)
        print("절      : {}개".format(len(secs)))
        for s in secs[:30]:
            pages = "" if s.page_from is None else " p{}-{}".format(s.page_from, s.page_to)
            print("   [{:>3}] {}{} · 블록 {}".format(s.id, " > ".join(s.path) or "(머리말)",
                                                  pages, len(s.blocks)))
        if len(secs) > 30:
            print("   ... (이하 {}개 생략)".format(len(secs) - 30))

    emb = OnnxEmbedder()
    emb.ensure_loaded()
    chunks = split_blocks(doc, emb.tokenizer)
    lens = sorted(_tlen(emb.tokenizer, c["text"]) for c in chunks)
    if lens:
        print("청크    : {}개 (토큰 최소 {} · 중앙 {} · 최대 {}, 예산 {})".format(
            len(chunks), lens[0], lens[len(lens) // 2], lens[-1], config.CHUNK_TOKENS))
    else:
        print("청크    : 없음")
    print("-" * 66)
    shown = 0
    for i, c in enumerate(chunks):
        if shown >= args.chars:
            print("\n... (청크 {}개 더)".format(len(chunks) - i))
            break
        print("── 청크 {} ──".format(i))
        print(c["text"])
        shown += len(c["text"])
    return 0


#------------------------------------------------------------------
# 검색 전용 명령
#=> 생성 모델을 올리지 않고 검색 결과만 본다(1GB 로딩을 건너뛰어 빠르다).
#   "답이 이상하다" 는 문제가 검색 탓인지 생성 탓인지 가르는 데 쓴다.
#------------------------------------------------------------------
def cmd_search(args):
    app = App()
    try:
        app.warmup(wait=True, skip_llm=True)     # LLM 미적재
        app.wait_rerank()        # 1회용 — 검색 시간에 리랭커 적재가 섞이지 않게(§34)
        folder = getattr(args, "folder", None)
        chunks, timing = app.retriever.search(args.query, top_k=args.top_k, folder=folder)

        print("\n질의: {}".format(args.query))
        if folder:
            print("범위: {} (문서 {}건, {})".format(folder, timing.get("scope_docs"),
                                                  "벡터 한 벌" if timing.get("scope_via") == "matrix"
                                                  else "Qdrant 필터"))
        rr = " + 리랭킹 {:.0f}".format(timing["rerank_ms"]) if "rerank_ms" in timing else ""
        print("검색 {:.0f}ms (임베딩 {:.0f} + dense {:.0f} + BM25 {:.0f} + 융합 {:.0f}{})\n"
              .format(timing["total_ms"], timing["embed_ms"], timing["dense_ms"],
                      timing["bm25_ms"], timing["fuse_ms"], rr))

        if not chunks:
            if folder and timing.get("scope_docs") == 0:
                print("이 폴더(하위 포함)에는 인덱싱된 문서가 없습니다.")
            else:
                print("검색 결과 없음 — 인덱스가 비었거나 질의어가 코퍼스에 없습니다.")
            return 1

        for i, c in enumerate(chunks, 1):
            print("[{}] {}  (폴더: {})".format(i, c["doc_name"], c.get("folder", "-")))
            body = c["text"].replace("\n", " ").strip()
            print("    {}{}\n".format(body[:args.chars],
                                      "…" if len(body) > args.chars else ""))
        return 0
    finally:
        app.close()


#------------------------------------------------------------------
# 동의어 후보 제안 명령 (동의어 설계서 3단계)
#=> 놓친 질문 기록(synonym_misses.jsonl)을 읽어 "일상어 → 문서 용어" 후보를 synonym_candidates.json 에 쓴다.
#    1) 검색 구성요소를 올린다(1.7B 는 --no-llm 이 아니면 함께 — 후보 하나를 고르게 한다)
#    2) 질문마다 후보를 뽑아 낱말별로 모은다(synonym_suggest.suggest)
#    3) 결과 파일을 쓰고 상위 후보를 화면에 보여 준다
#    4) 마지막 줄에 결과 요약 "@index {json}" — RAGSearchBox 가 읽는다(chat 워커 /suggest-synonyms 와 같은 모양)
#   ⚠️ 인덱스는 한 프로세스만 열 수 있다 — chat 워커가 떠 있으면 이 명령은 실패하므로, RAGSearchBox 는
#      그때 워커에게 /suggest-synonyms 를 보낸다.
#
# -in: args.from_ = 질문 목록 jsonl(기본 <데이터 폴더>/synonym_misses.jsonl, 바꿔 말한 질문 세트도 됨)
# -in: args.out   = 결과 경로(기본 <데이터 폴더>/synonym_candidates.json)
# -in: args.no_llm = True 면 e5 순서만 쓴다(모델을 올리지 않아 빠르다)
#
# -out: 종료 코드 (0 = 성공, 1 = 기록된 질문 없음·실패)
# -out: error = 없음 (적재·검색 실패도 요약 줄의 why 로)
#------------------------------------------------------------------
def cmd_suggest(args):
    from simplerag.index import commands as index_commands
    from simplerag.retrieve import synonym_suggest as ss

    src = args.from_ or os.path.join(config.ROOT, ss.MISSES_NAME)
    if not ss.read_misses(src):
        print("기록된 질문이 없습니다: {}".format(src))
        print(index_commands.format_result({"op": "suggest-synonyms", "ok": False,
                                            "why": "기록된 질문이 없습니다: {}".format(src)}))
        return 1
    app = None
    try:
        app = App()
        app.warmup(wait=True, skip_llm=args.no_llm, on_log=_log)
        app.wait_rerank()
        llm = None if args.no_llm else app.pipeline.generator.complete
        summary, result = ss.run(app.retriever, llm_complete=llm, src=src, out=args.out, log=_log)
    except Exception as e:
        # 인덱스를 다른 프로세스(실행 중인 chat 워커)가 쥐고 있으면 여기로 온다 — 이유를 결과 줄에 남긴다
        summary, result = {"op": "suggest-synonyms", "ok": False, "why": "{}: {}".format(type(e).__name__, str(e)[:200])}, None
    finally:
        if app is not None:
            app.close()
    if result:
        print("\n질문 {}개 → 후보 낱말 {}개 ({}) — {}".format(
            result["n_questions"], len(result["candidates"]), "1.7B 고르기" if result["llm"] else "e5 순서", summary["path"]))
        for c in result["candidates"][:10]:
            terms = " · ".join("{}{} {:.2f}".format(t["term"], "★" if t["llm"] else "", t["score"]) for t in c["terms"])
            print("  {} ({}회) → {}".format(c["word"], c["count"], terms))
    else:
        print("후보 제안 실패: {}".format(summary["why"]))
    # RAGSearchBox 가 읽는 결과 한 줄 — chat 워커의 /suggest-synonyms 와 같은 모양
    print(index_commands.format_result(summary))
    return 0 if summary["ok"] else 1


#------------------------------------------------------------------
# 상태 명령
#=> 인덱스 규모와 마지막 갱신 상황을 보여 준다.
#------------------------------------------------------------------
def cmd_status(args):
    from simplerag.index.indexer import chunk_params, load_state

    app = App()
    try:
        stats = app.store.stats()
        state = load_state()
        print("인덱스   : {:,}청크 / {:.1f}MB".format(stats["count"], stats["disk_mb"]))
        print("문서     : {}건".format(len(state["docs"])))
        print("BM25캐시 : {}".format(
            "있음" if os.path.isfile(config.BM25_PATH) else "없음(dense 단독 폴백)"))
        print("설정파일 : {}".format(config.CONFIG_PATH or "없음(코드 기본값)"))
        print("청킹     : {}토큰 / 겹침 {} / {} / 머리글 상한 {}".format(
            config.CHUNK_TOKENS, config.CHUNK_OVERLAP, config.CHUNK_MODE,
            config.TABLE_HEADER_MAX_TOKENS))
        rec = state.get("chunk_params")
        # 추출 엔진 — 설정값과 인덱스를 만든 엔진을 함께 보여 준다(기록에 키가 없으면 legacy 로 만든 것)
        built = (rec or {}).get("extract_engine", "legacy") if rec is not None else "기록 없음"
        print("추출엔진 : {} (인덱스: {})".format(config.EXTRACT_ENGINE, built))
        if state["docs"] and rec is None:
            print("  ⚠️ 인덱스에 청킹 설정 기록 없음(이전 버전에서 만듦)")
        elif rec is not None and rec != chunk_params():
            print("  ⚠️ 인덱스는 다른 청킹 설정으로 만들어짐 — --rebuild 필요: {}".format(rec))
        print("검색     : dense {} + BM25 {} → RRF(k={:g}) → 리랭커 입력 {} → sLLM 근거 {}건".format(
            config.DENSE_TOP_K, config.BM25_TOP_K, config.RRF_K, config.RERANK_POOL, config.TOP_K))
        print("생성모델 : {} ({} 모드)   ← {}".format(
            config.GEN_MODELS[config.DEFAULT_GEN_MODEL], _model_mode(config.DEFAULT_GEN_MODEL),
            "한 번만 정밀하게: ask --precise" if config.DEFAULT_GEN_MODEL == config.FAST_GEN_MODEL
            else "다른 모델: ask --model {}".format(" | ".join(
                m for m in config.USER_GEN_MODELS if m != config.DEFAULT_GEN_MODEL))))
        cached = gen_backend.peek()
        print("생성백엔드: {}".format(
            "{} — {}".format(gen_backend.label(cached["backend"]), cached.get("reason", ""))
            if cached else "미측정 — 첫 질문 때 자동 측정(`backend` 명령으로 미리 가능)"))
        print("리랭커   : {}".format(
            "있음" if os.path.isfile(os.path.join(config.RERANK_DIR, "model_int8.onnx"))
            else "없음(리랭킹 비활성)"))

        if args.verbose:
            print("\n문서별 청크 수:")
            for path, meta in sorted(state["docs"].items(),
                                     key=lambda kv: -kv[1]["chunks"]):
                print("  {:>5}  {}".format(meta["chunks"], meta.get("name", path)))
        return 0
    finally:
        app.close()


#------------------------------------------------------------------
# 인덱스 삭제 명령
#=> 인덱스를 지운다. 되돌릴 수 없으므로 기본은 확인을 받고, 무엇을 지우는지
#   먼저 보여 준다. --yes 로만 무인 실행(배치 스크립트)을 허용한다.
#
#   삭제 대상 세 가지는 한 묶음이다. 하나만 지우면 상태가 어긋난다 —
#   예를 들어 index_state.json 만 남기면 증분 갱신이 "이미 처리함"으로
#   착각해 재인덱싱을 건너뛴다.
#
#   --doc 을 주면 그 문서의 청크만 지운다(전체 재인덱싱 없이 한 건 제거).
#------------------------------------------------------------------
def cmd_clear(args):
    import shutil

    from simplerag.index.indexer import load_state, save_state

    # ── 문서 1건만 제거 ────────────────────────────────
    if args.doc:
        target = os.path.abspath(args.doc)
        state = load_state()
        meta = state["docs"].get(target)
        if not meta:
            print("인덱스에 없는 문서입니다: {}".format(target), file=sys.stderr)
            print("  (경로가 인덱싱 당시와 정확히 같아야 합니다. "
                  "`status -v` 로 확인하세요)", file=sys.stderr)
            return 1

        if not args.yes:
            print("다음 문서의 청크 {}개를 인덱스에서 제거합니다:".format(
                meta.get("chunks", "?")))
            print("  {}".format(target))
            if input("진행할까요? [y/N] ").strip().lower() not in ("y", "yes"):
                print("취소했습니다.")
                return 1

        app = App()
        try:
            app.warmup(wait=True, skip_llm=True, rerank=False)
            # remove_doc 이 "인덱스가 바뀌었다" 를 적어 두므로, 다음 index 가 바뀐 문서가
            # 없어도 BM25 를 다시 만든다(예전에는 변경 0건이면 BM25 를 건너뛰었다)
            from simplerag.index.indexer import remove_doc
            remove_doc(target, state, app.store)
            print("제거했습니다. BM25 는 다음 `index --dir <폴더>` 때 다시 만들어집니다 "
                  "(그 전에도 지운 문서는 근거로 나오지 않습니다).")
            return 0
        finally:
            app.close()

    # ── 인덱스 전체 삭제 ───────────────────────────────
    targets = []
    for path, label in ((config.QDRANT_DIR, "벡터 인덱스"),
                        (config.BM25_PATH, "BM25 캐시"),
                        (config.STATE_PATH, "증분 상태")):
        if os.path.isdir(path):
            size = sum(os.path.getsize(os.path.join(r, f))
                       for r, _, fs in os.walk(path) for f in fs)
            targets.append((path, label, size))
        elif os.path.isfile(path):
            targets.append((path, label, os.path.getsize(path)))

    if not targets:
        print("삭제할 인덱스가 없습니다. (데이터 루트: {})".format(config.ROOT))
        return 0

    state = load_state()
    print("데이터 루트: {}".format(config.ROOT))
    print("문서 {}건 / 다음 항목을 삭제합니다:".format(len(state["docs"])))
    for path, label, size in targets:
        print("  {:<12} {:>8.1f} MB  {}".format(
            label, size / 1024 / 1024, os.path.basename(path)))

    if not args.yes:
        print("\n⚠️ 되돌릴 수 없습니다. 다시 인덱싱하려면 문서 수에 따라 수십 분이 걸립니다.")
        if input("정말 삭제할까요? [y/N] ").strip().lower() not in ("y", "yes"):
            print("취소했습니다.")
            return 1

    failed = []
    for path, label, _ in targets:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            print("  삭제: {}".format(label))
        except Exception as e:
            failed.append((label, str(e)[:80]))

    if failed:
        print("\n일부 삭제 실패 — 다른 프로세스가 사용 중일 수 있습니다:",
              file=sys.stderr)
        for label, why in failed:
            print("  {}: {}".format(label, why), file=sys.stderr)
        return 1

    print("\n삭제 완료. `index --dir <폴더>` 로 다시 인덱싱하세요.")
    return 0


#------------------------------------------------------------------
# 모델 별칭 → 사람이 읽는 모드 이름
#=> 화면에 "빠름/정밀/한국어" 로 보여 준다. 측정용 모델 등 그 밖의 것은 별칭을 그대로 쓴다.
#
# -in: alias = 생성 모델 별칭
#
# -out: "빠름" | "정밀" | "한국어" | 별칭 그대로
# -out: error = 없음
#------------------------------------------------------------------
def _model_mode(alias):
    return {config.FAST_GEN_MODEL: "빠름", config.PRECISE_GEN_MODEL: "정밀",
            config.KOREAN_GEN_MODEL: "한국어"}.get(alias, alias)


#------------------------------------------------------------------
# 이번 명령에서 쓸 생성 모델 고르기 (정밀 모드 — 계획서 D8 ③ / REPORT §44)
#=> 기본은 설정 generation.model(1.7B), `--precise` 를 붙이면 그 실행만 1.7B, `--model` 은 고른 모델로 답한다.
#    1) `--precise` 면 정밀 모델 별칭
#    2) `--model` 로 직접 고른 것이 있으면 그것
#    3) 둘 다 없으면 None — App 이 설정 기본값을 쓴다
#   두 옵션을 서로 다르게 함께 주면 어느 쪽인지 알 수 없으므로 오류로 멈춘다.
#
# -in: args = argparse 결과 (precise·model 을 가질 수 있다)
#
# -out: 모델 별칭 문자열 또는 None(설정 기본값)
# -out: error = --precise 와 --model 이 충돌하면 SystemExit
#------------------------------------------------------------------
def _pick_model(args):
    precise = getattr(args, "precise", False)
    chosen = getattr(args, "model", None)
    if precise:
        if chosen and chosen != config.PRECISE_GEN_MODEL:
            raise SystemExit("--precise 와 --model {} 를 함께 쓸 수 없습니다".format(chosen))
        return config.PRECISE_GEN_MODEL
    return chosen


#------------------------------------------------------------------
# 생성 백엔드 명령
#=> 이 PC 에서 CPU 와 iGPU 중 무엇으로 답변을 만드는지, 왜 그렇게 골랐는지
#   보여 준다. --reprobe 면 저장된 측정을 무시하고 다시 잰다(드라이버 갱신,
#   전원 설정 변경 후). 인덱스를 열지 않으므로 다른 명령과 겹쳐 실행해도 된다.
#
# -in: args.reprobe = True 면 다시 측정
#
# -out: 0 (항상) — CPU 폴백은 실패가 아니라 정상 동작이다
# -out: error = 없음
#------------------------------------------------------------------
def cmd_backend(args):
    info = gen_backend.select(reprobe=args.reprobe, on_log=print)
    src = {"cache": "저장된 측정", "probe": "방금 측정", "config": "환경변수 지정",
           "check": "사전 점검", "loaded": "이미 적재됨"}.get(info.get("source"), "-")

    print("\n생성 백엔드 : {}".format(gen_backend.label(info["backend"])))
    print("선택 근거   : {} — {}".format(src, info.get("reason", "")))
    for kind, lab in (("cpu", "CPU"), ("vulkan", "iGPU")):
        m = info.get(kind)
        if not m:
            continue
        if m.get("ok"):
            print("  {:<5} prefill {:>5.0f} t/s  ({}토큰 {:.2f}초 / 적재 {:.1f}초 / 첫회 {:.1f}초)".format(
                lab, m["prefill_tps"], m["prompt_tokens"], m["prefill_s"],
                m.get("load_s", 0), m.get("warm_s", 0)))
        else:
            print("  {:<5} 사용 불가 — {}".format(lab, m.get("error", "")))

    use_rr = gen_backend.rerank_enabled(info["backend"])
    has_rr = os.path.isfile(os.path.join(config.RERANK_DIR, "model_int8.onnx"))
    print("리랭킹      : {}{}".format(
        "켬" if use_rr else "끔",
        " (⚠️ 모델 없음 → 실제로는 끔)" if use_rr and not has_rr else ""))
    print("저장 위치   : {}".format(config.BACKEND_CACHE_PATH))
    print("\n강제 지정: 환경변수 SIMPLERAG_GEN_BACKEND=cpu|vulkan, SIMPLERAG_RERANK=1|0")
    print("다시 측정: backend --reprobe")
    return 0


#------------------------------------------------------------------
# 예열 명령
#=> 콜드스타트 실측용. 병렬 예열이 실제로 최장 항목으로 수렴하는지 확인한다.
#------------------------------------------------------------------
def cmd_warmup(args):
    app = App(_pick_model(args))
    try:
        r = app.warmup(wait=True, on_log=_log)
        print("  생성      {} / 리랭킹 {}".format(
            gen_backend.label(r.get("_backend")), "켬" if r.get("_rerank") else "끔"))
        for k in ("embedder", "qdrant", "bm25", "reranker", "llm", "llm_warm"):
            if k not in r:
                continue
            v = r[k]
            if isinstance(v, str):
                print("  {:<9} ❌ {}".format(k, v))
            elif v < 0:
                print("  {:<9} — 캐시 없음".format(k))
            else:
                print("  {:<9} {:.0f}ms".format(k, v))
        print("  {:<9} {:.0f}ms (병렬 총합 = 준비 완료)".format("합계", r["_total_ms"]))
        if r.get("_rerank_deferred"):
            v = app.wait_rerank()
            if isinstance(v, str):
                print("  {:<9} ❌ {} (백그라운드)".format("reranker", v))
            else:
                print("  {:<9} {:.0f}ms (준비 완료 뒤 백그라운드)".format("reranker", v or 0))
        return 0
    finally:
        app.close()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    # 백엔드 측정용 자식 프로세스 진입점(generate/backend.py). 사용자 명령이 아니라
    # 도움말에 싣지 않는다. 출력(JSON 한 줄)이 오염되지 않게 argparse 전에 가로챈다.
    if argv and argv[0] == "_probe-backend":
        from simplerag.generate.backend import probe_main
        return probe_main(argv[1:])

    p = argparse.ArgumentParser(prog="simplerag", description="로컬 문서 RAG")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="폴더 인덱싱(증분)")
    pi.add_argument("--dir", required=True)
    pi.add_argument("--rebuild", action="store_true", help="전체 재인덱싱")
    pi.add_argument("--no-recursive", action="store_true", help="하위 폴더 제외")
    pi.add_argument("--allow-delete", action="store_true",
                    help="사라진 문서가 많아도(30%% 또는 50건 이상) 인덱스에서 뺀다")
    pi.add_argument("--max-add", type=int, default=None,
                    help="새 문서가 이보다 많으면 새 문서는 넣지 않고 미룬다(자동 인덱싱용)")
    pi.set_defaults(func=cmd_index)

    pa = sub.add_parser("ask", help="질의 (질문 생략 시 대화형)")
    pa.add_argument("query", nargs="?", default=None)
    pa.add_argument("--top-k", type=int, default=None)
    pa.add_argument("--max-tokens", type=int, default=None)
    pa.add_argument("--folder", default=None,
                    help="이 폴더(하위 포함) 문서만 근거로 쓴다(폴더 한정 검색)")
    pa.add_argument("--model", default=None,
                    choices=list(config.USER_GEN_MODELS), help="생성 모델 직접 지정")
    pa.add_argument("--precise", action="store_true",
                    # argparse 는 help 를 %-서식으로 처리한다 — "%p" 를 쓰면 --help 가 죽는다(%% 로 적는다)
                    help="정밀 모드(1.7B) — 506문항 정답 +4.4%%p, 첫 글자까지 3초를 넘을 수 있다")
    pa.add_argument("--no-stream", action="store_true",
                    help="답변을 한 번에 출력(콘솔 렌더링 문제 회피)")
    pa.set_defaults(func=cmd_ask)

    pc = sub.add_parser("chat", help="대화형 질의 (모델 1회 적재)")
    pc.add_argument("--top-k", type=int, default=None)
    pc.add_argument("--max-tokens", type=int, default=None)
    pc.add_argument("--model", default=None, choices=list(config.USER_GEN_MODELS))
    pc.add_argument("--precise", action="store_true", help="정밀 모드(1.7B)로 대화")
    pc.add_argument("--no-stream", action="store_true",
                    help="답변을 한 번에 출력(콘솔 렌더링 문제 회피)")
    pc.set_defaults(func=cmd_chat, query=None)

    pe = sub.add_parser("extract", help="추출 미리보기(인덱싱 전 확인)")
    pe.add_argument("path", help="문서 파일 경로")
    pe.add_argument("--chars", type=int, default=1200, help="출력할 문자 수")
    pe.add_argument("--blocks", action="store_true",
                    help="구조화 추출(blocks 엔진) 결과 — 블록·절·청크를 보여 준다")
    pe.add_argument("--dump", metavar="DIR",
                    help="--blocks 와 함께: 블록 JSON 을 이 폴더에 저장")
    pe.set_defaults(func=cmd_extract)

    pse = sub.add_parser("search", help="검색만 수행(LLM 미적재)")
    pse.add_argument("query")
    pse.add_argument("--top-k", type=int, default=None)
    pse.add_argument("--chars", type=int, default=200, help="청크 미리보기 길이")
    pse.add_argument("--folder", default=None, help="이 폴더(하위 포함) 문서에서만 찾는다")
    pse.set_defaults(func=cmd_search)

    pcl = sub.add_parser("clear", help="인덱스 삭제")
    pcl.add_argument("--doc", default=None,
                     help="이 문서의 청크만 제거(경로). 생략 시 인덱스 전체")
    pcl.add_argument("-y", "--yes", action="store_true", help="확인 없이 삭제")
    pcl.set_defaults(func=cmd_clear)

    ps = sub.add_parser("status", help="인덱스 상태")
    ps.add_argument("-v", "--verbose", action="store_true")
    ps.set_defaults(func=cmd_status)

    pb = sub.add_parser("backend", help="생성 백엔드(CPU/iGPU) 확인·재측정")
    pb.add_argument("--reprobe", action="store_true", help="저장된 측정을 무시하고 다시 잰다")
    pb.set_defaults(func=cmd_backend)

    psg = sub.add_parser("suggest-synonyms", help="놓친 질문으로 동의어 사전 후보 뽑기")
    psg.add_argument("--from", dest="from_", default=None, help="질문 목록 jsonl (기본: 데이터 폴더 synonym_misses.jsonl)")
    psg.add_argument("--out", default=None, help="결과 경로 (기본: 데이터 폴더 synonym_candidates.json)")
    psg.add_argument("--no-llm", action="store_true", help="1.7B 고르기 없이 e5 순서만")
    psg.set_defaults(func=cmd_suggest)

    pw = sub.add_parser("warmup", help="예열만 수행(콜드스타트 측정)")
    pw.add_argument("--model", default=None, choices=list(config.USER_GEN_MODELS))
    pw.add_argument("--precise", action="store_true", help="정밀 모드(1.7B) 예열")
    pw.set_defaults(func=cmd_warmup)

    _force_utf8_output()       # cp949 로 못 쓰는 문자 때문에 죽지 않게(리다이렉트 대비)
    for w in config.CONFIG_WARNINGS:
        print("설정 경고: " + w, file=sys.stderr)
    args = p.parse_args(argv)
    console.install()          # 콘솔 텍스트 속성 저장 → 종료 시 자동 복구
    try:
        return args.func(args)
    finally:
        console.restore()


if __name__ == "__main__":
    raise SystemExit(main())
