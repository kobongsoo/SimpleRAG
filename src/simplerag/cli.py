#------------------------------------------------------------------
# CLI 진입점 (설계서 §7)
#=> index / ask / status / warmup 네 명령.
#
#   ask 는 2단계 응답을 그대로 화면에 옮긴다 — 근거를 먼저 찍고, 그 아래
#   답변을 토큰 단위로 흘린다. 파이프라인이 UI 에 독립적이라 나중에 웹/데스크톱
#   UI 로 바꿔도 이 흐름을 그대로 쓴다.
#------------------------------------------------------------------

import argparse
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
from simplerag.generate.prompts import strip_instruction_echo  # noqa: E402
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
# 인덱싱 명령
#=> 폴더의 변경된 문서만 다시 인덱싱한다. --rebuild 면 전부 다시.
#------------------------------------------------------------------
def cmd_index(args):
    app = App()
    try:
        # 인덱싱에는 생성 모델도 리랭커도 필요 없다 — 괜히 올리지 않는다.
        app.warmup(wait=True, skip_llm=True, rerank=False)
        try:
            summary = index_folder(args.dir, app.embedder, app.store, app.bm25,
                                   rebuild=args.rebuild, on_log=print,
                                   recursive=not args.no_recursive)
        except RuntimeError as e:
            # 폴더 없음·청킹 설정 불일치 — 사용자가 고칠 수 있는 오류라 스택 없이 알린다
            print("\n❌ {}".format(e), file=sys.stderr)
            return 1
        print("\n문서 {}건 / 변경 {}건 / 청크 {:,}개 / {:.1f}s".format(
            summary["files"], summary["changed"], summary["chunks"], summary["sec"]))
        if summary.get("failed"):
            print("실패 {}건".format(len(summary["failed"])))
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
                           stream=not args.no_stream)
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
#------------------------------------------------------------------
class StreamWriter:
    #--------------------------------------------------------------
    # hold — 꼬리 보류 구간 (지시문 되뱉기 제거용)
    #=> 0.6B 는 답변 끝에 시스템 지시문을 그대로 옮겨 적는 일이 있다
    #   ("… 지급합니다. [1] 근거 번호 [1] 형식으로 표기합니다.").
    #   스트리밍은 토큰을 받는 즉시 찍기 때문에, 다 만든 뒤 지워 봐야
    #   화면에는 이미 나간 뒤다. 그래서 **끝에서 hold 글자만큼을 붙들어 둔다.**
    #   관측된 되뱉기는 가장 긴 것이 약 30자라 90자면 충분하다.
    #--------------------------------------------------------------
    def __init__(self, min_chars=40, interval=0.06, hold=90):
        self.min_chars = min_chars
        self.interval = interval
        self.hold = hold
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
    #=> 보류 구간(hold)은 남겨 둔다. 마지막 정리는 finish() 가 한다.
    #--------------------------------------------------------------
    def flush(self):
        keep = self.hold if not self._final else 0
        out = self._pending[:-keep] if keep else self._pending
        if not out:
            return
        self._pending = self._pending[len(out):]
        self._emitted += out
        sys.stdout.write(out)
        sys.stdout.flush()
        self._n = 0
        self._last = time.perf_counter()
        self.writes += 1

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
    # -in: cleaner = 본문 정리 함수(없으면 그대로)
    #--------------------------------------------------------------
    def finish(self, cleaner=None):
        self._final = True
        full = self._emitted + self._pending
        cleaned = cleaner(full) if cleaner else full

        tail = self._tail_after(cleaned)
        if tail is None and cleaner:
            tail = cleaner(self._pending)
        if tail is not None:
            self._pending = tail
        self.flush()

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
                  stream=True):
    writer = StreamWriter()
    parts = []
    # 근거 개수. evidence 이벤트에서 정해지고, 마무리 정리 때 쓰인다 —
    # 없는 번호를 인용으로 찍는 결함을 막는다(prompts.drop_invalid_cites).
    n_ev = [None]

    # 붙들어 둔 꼬리를 마무리할 때 쓰는 정리기. 호출 시점의 n_ev 를 본다.
    def clean(text):
        return strip_instruction_echo(text, n_ev[0])

    try:
        for event in app.pipeline.answer(query, top_k=top_k, max_tokens=max_tokens):
            kind = event[0]

            if kind == "evidence":
                chunks, timing = event[1], event[2]
                n_ev[0] = len(chunks)
                print("\n── 근거 {}건 ({:.0f}ms) ─────────────────────".format(
                    len(chunks), timing["total_ms"]))
                for i, c in enumerate(chunks, 1):
                    body = c["text"].replace("\n", " ").strip()
                    print("  [{}] {}".format(i, c["doc_name"]))
                    print("      {}{}".format(body[:160], "…" if len(body) > 160 else ""))
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
                    writer.finish(clean)
                else:
                    # 비스트리밍은 파이프라인이 이미 정리한 본문을 그대로 쓴다.
                    print(d["answer"].strip())

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
                    if d["cited"]:
                        print("  인용 근거: {}".format(
                            ", ".join("[{}]".format(n) for n in d["cited"])))
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

            # 슬래시 명령 — 모델을 다시 올리지 않고 설정만 바꾼다.
            if query.startswith("/"):
                parts = query.split()
                cmd = parts[0].lower()
                if cmd == "/help":
                    print("  /topk N     근거 개수 변경 (현재 {})".format(
                        top_k or config.TOP_K))
                    print("  /status     인덱스 상태")
                    print("  exit        종료\n")
                elif cmd == "/topk" and len(parts) > 1 and parts[1].isdigit():
                    top_k = int(parts[1])
                    print("  근거 개수를 {}개로 변경했습니다.\n".format(top_k))
                elif cmd == "/status":
                    s = app.store.stats()
                    print("  {:,}청크 / {:.1f}MB\n".format(s["count"], s["disk_mb"]))
                else:
                    print("  알 수 없는 명령입니다. /help 를 입력하세요.\n")
                continue

            render_answer(app, query, top_k=top_k, max_tokens=args.max_tokens,
                          stream=not args.no_stream)
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
# 검색 전용 명령
#=> 생성 모델을 올리지 않고 검색 결과만 본다(1GB 로딩을 건너뛰어 빠르다).
#   "답이 이상하다" 는 문제가 검색 탓인지 생성 탓인지 가르는 데 쓴다.
#------------------------------------------------------------------
def cmd_search(args):
    app = App()
    try:
        app.warmup(wait=True, skip_llm=True)     # LLM 미적재
        app.wait_rerank()        # 1회용 — 검색 시간에 리랭커 적재가 섞이지 않게(§34)
        chunks, timing = app.retriever.search(args.query, top_k=args.top_k)

        print("\n질의: {}".format(args.query))
        rr = " + 리랭킹 {:.0f}".format(timing["rerank_ms"]) if "rerank_ms" in timing else ""
        print("검색 {:.0f}ms (임베딩 {:.0f} + dense {:.0f} + BM25 {:.0f} + 융합 {:.0f}{})\n"
              .format(timing["total_ms"], timing["embed_ms"], timing["dense_ms"],
                      timing["bm25_ms"], timing["fuse_ms"], rr))

        if not chunks:
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
        if state["docs"] and rec is None:
            print("  ⚠️ 인덱스에 청킹 설정 기록 없음(이전 버전에서 만듦)")
        elif rec is not None and rec != chunk_params():
            print("  ⚠️ 인덱스는 다른 청킹 설정으로 만들어짐 — --rebuild 필요: {}".format(rec))
        print("검색     : dense {} + BM25 {} → RRF(k={:g}) → 리랭커 입력 {} → sLLM 근거 {}건".format(
            config.DENSE_TOP_K, config.BM25_TOP_K, config.RRF_K, config.RERANK_POOL, config.TOP_K))
        print("생성모델 : {} ({} 모드)   ← {}".format(
            config.GEN_MODELS[config.DEFAULT_GEN_MODEL], _model_mode(config.DEFAULT_GEN_MODEL),
            "한 번만 정밀하게: ask --precise" if config.DEFAULT_GEN_MODEL == config.FAST_GEN_MODEL
            else "빠르게: ask --model {}".format(config.FAST_GEN_MODEL)))
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
            app.store.delete_doc(target)
            del state["docs"][target]
            save_state(state)
            print("제거했습니다. ⚠️ BM25 인덱스는 그대로이므로 "
                  "`index --dir <폴더>` 로 갱신하세요.")
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
#=> 화면에 "빠름/정밀" 로 보여 준다. 설정으로 다른 모델을 넣었을 때는 별칭을 그대로 쓴다.
#
# -in: alias = 생성 모델 별칭
#
# -out: "빠름" | "정밀" | 별칭 그대로
# -out: error = 없음
#------------------------------------------------------------------
def _model_mode(alias):
    return {config.FAST_GEN_MODEL: "빠름", config.PRECISE_GEN_MODEL: "정밀"}.get(alias, alias)


#------------------------------------------------------------------
# 이번 명령에서 쓸 생성 모델 고르기 (정밀 모드 — 계획서 D8 ③ / REPORT §44)
#=> 기본은 빠른 0.6B(설정 generation.model), `--precise` 를 붙이면 그 실행만 1.7B 로 답한다.
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
    pi.set_defaults(func=cmd_index)

    pa = sub.add_parser("ask", help="질의 (질문 생략 시 대화형)")
    pa.add_argument("query", nargs="?", default=None)
    pa.add_argument("--top-k", type=int, default=None)
    pa.add_argument("--max-tokens", type=int, default=None)
    pa.add_argument("--model", default=None,
                    choices=list(config.GEN_MODELS), help="생성 모델 직접 지정")
    pa.add_argument("--precise", action="store_true",
                    # argparse 는 help 를 %-서식으로 처리한다 — "%p" 를 쓰면 --help 가 죽는다(%% 로 적는다)
                    help="정밀 모드(1.7B) — 506문항 정답 +4.4%%p, 첫 글자까지 3초를 넘을 수 있다")
    pa.add_argument("--no-stream", action="store_true",
                    help="답변을 한 번에 출력(콘솔 렌더링 문제 회피)")
    pa.set_defaults(func=cmd_ask)

    pc = sub.add_parser("chat", help="대화형 질의 (모델 1회 적재)")
    pc.add_argument("--top-k", type=int, default=None)
    pc.add_argument("--max-tokens", type=int, default=None)
    pc.add_argument("--model", default=None, choices=list(config.GEN_MODELS))
    pc.add_argument("--precise", action="store_true", help="정밀 모드(1.7B)로 대화")
    pc.add_argument("--no-stream", action="store_true",
                    help="답변을 한 번에 출력(콘솔 렌더링 문제 회피)")
    pc.set_defaults(func=cmd_chat, query=None)

    pe = sub.add_parser("extract", help="추출 미리보기(인덱싱 전 확인)")
    pe.add_argument("path", help="문서 파일 경로")
    pe.add_argument("--chars", type=int, default=1200, help="출력할 문자 수")
    pe.set_defaults(func=cmd_extract)

    pse = sub.add_parser("search", help="검색만 수행(LLM 미적재)")
    pse.add_argument("query")
    pse.add_argument("--top-k", type=int, default=None)
    pse.add_argument("--chars", type=int, default=200, help="청크 미리보기 길이")
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

    pw = sub.add_parser("warmup", help="예열만 수행(콜드스타트 측정)")
    pw.add_argument("--model", default=None, choices=list(config.GEN_MODELS))
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
