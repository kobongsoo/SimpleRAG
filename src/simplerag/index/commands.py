#------------------------------------------------------------------
# 워커 인덱싱 명령 (자동 인덱싱 설계서 §8·§9)
#=> 모델을 올려 둔 채 도는 워커(`simplerag chat`)가 stdin 으로 받은 인덱싱 명령을 처리한다.
#
#   왜 워커가 직접 하나
#     Qdrant local 은 인덱스 폴더를 한 프로세스만 연다. 워커가 떠 있는 동안에는
#     `simplerag index` 가 폴더를 열 수 없으므로, 인덱스를 쥔 워커에게 부탁한다.
#     워커의 입력 루프는 한 줄씩 차례로 처리하므로 질문과 인덱싱이 저절로 번갈아 돌고,
#     둘이 동시에 인덱스를 만지는 일이 없다 — 잠금·스레드가 필요 없다.
#
#   주고받는 모양
#     보내기: /index-doc {"path": "D:\\분류함\\규정.docx"}      (경로만 써도 된다)
#     받기  : @index {"op": "index-doc", "ok": true, ...}      (한 줄 JSON)
#     경로를 JSON(ASCII)으로 보내면 워커 stdin 인코딩(cp949)으로 못 쓰는 글자가 든
#     파일명도 깨지지 않는다(\uXXXX 로 적힌다).
#
#   명령
#     /index-doc   문서 한 건 추가·수정(새것 넣고 옛것 지우기, indexer.sync_doc)
#     /remove-doc  문서 한 건 빼기 — 파일이 실제로 없을 때만
#     /bm25        BM25 다시 만들기 + 고아 청소. 워커가 쓰는 BM25 가 그 자리에서 바뀐다
#     /index-plan  폴더와 상태 파일을 대조한 할 일 목록(인덱스는 안 건드린다)
#------------------------------------------------------------------

import json
import os
import time

from . import indexer

PREFIX = "@index "
COMMANDS = ("/index-doc", "/remove-doc", "/bm25", "/index-plan")


#------------------------------------------------------------------
# 명령 인자에서 경로 꺼내기
#=> JSON({"path": …})이면 그 값을, 아니면 인자 전체를 경로로 본다.
#
# -in: arg = 명령 뒤의 글자
#
# -out: 경로 문자열 (없으면 "")
# -out: error = 없음 (JSON 이 깨졌으면 글자 그대로를 경로로 본다)
#------------------------------------------------------------------
def parse_path(arg):
    arg = (arg or "").strip()
    if arg.startswith("{"):
        try:
            return str(json.loads(arg).get("path", "")).strip()
        except (ValueError, AttributeError):
            pass
    # 탐색기에서 복사한 경로는 따옴표로 싸여 있기도 하다
    return arg.strip('"')


#------------------------------------------------------------------
# 결과 한 줄 만들기
#=> RAGSearchBox 가 답변 출력과 헷갈리지 않게 "@index " 로 시작하는 한 줄 JSON 이다.
#   ensure_ascii 라 cp949 콘솔에서도 깨지지 않는다.
#
# -in: result = dict
#
# -out: 한 줄 문자열
# -out: error = 없음
#------------------------------------------------------------------
def format_result(result):
    return PREFIX + json.dumps(result, ensure_ascii=True, separators=(",", ":"))


#------------------------------------------------------------------
# 워커 인덱싱 명령 처리기
#=> 워커의 App 을 받아 그 안의 임베더·저장소·BM25 를 그대로 쓴다(추가 메모리 없음).
#   문서 추출기는 처음 쓸 때 한 번만 만든다.
#
# -필드: app = warmup.App (embedder, store, bm25 가 올라가 있는 것)
#------------------------------------------------------------------
class IndexCommands:
    #--------------------------------------------------------------
    # 생성자
    #
    # -in: app = warmup.App
    #
    # -out: 없음
    # -out: error = 없음
    #--------------------------------------------------------------
    def __init__(self, app):
        self.app = app
        self._ext = None

    #--------------------------------------------------------------
    # 명령 한 줄 처리 (입구)
    #=> 무슨 일이 있어도 예외를 밖으로 내지 않는다 — 워커의 대화 루프가 죽으면 안 된다.
    #   결과 dict 에는 언제나 op·ok·ms 가 있다.
    #
    # -in: cmd = "/index-doc" 등
    # -in: arg = 나머지 글자
    #
    # -out: 결과 dict
    # -out: error = 없음 (예외는 ok=False, why 로 바꾼다)
    #--------------------------------------------------------------
    def run(self, cmd, arg):
        t0 = time.perf_counter()
        op = cmd.lstrip("/")
        try:
            if cmd == "/index-doc":
                res = self.index_doc(parse_path(arg))
            elif cmd == "/remove-doc":
                res = self.remove_doc(parse_path(arg))
            elif cmd == "/bm25":
                res = self.rebuild_bm25()
            elif cmd == "/index-plan":
                res = self.plan(parse_path(arg))
            else:
                res = {"ok": False, "why": "알 수 없는 명령"}
        except Exception as e:
            res = {"ok": False, "why": "{}: {}".format(type(e).__name__, str(e)[:120])}
        res = dict({"op": op}, **res)
        res["ms"] = round((time.perf_counter() - t0) * 1000)
        return res

    #--------------------------------------------------------------
    # 상태 파일 읽고 청킹 설정 확인
    #=> 매 명령마다 새로 읽는다 — 워커가 내려가 있던 동안 index 명령이 바꿨을 수 있다.
    #   청킹 설정이 인덱스와 다르면 증분으로 섞지 않는다(indexer.check_chunk_params).
    #
    # -in: 없음
    #
    # -out: 상태 dict
    # -out: error = 청킹 설정 불일치는 RuntimeError
    #--------------------------------------------------------------
    def _state(self):
        state = indexer.load_state()
        indexer.check_chunk_params(state, False, lambda m: None)
        return state

    #--------------------------------------------------------------
    # 문서 한 건 추가·수정
    #=> 옛 버전을 지우고 새 버전을 넣는 일은 indexer.sync_doc 이 안전한 순서로 한다.
    #   BM25 는 여기서 만들지 않는다 — 여러 건을 반영한 뒤 /bm25 한 번이면 된다.
    #
    # -in: path = 문서 경로
    #
    # -out: {ok, result("added"|"modified"|"same"|"unchanged"|"failed_before"), chunks, why}
    # -out: error = 저장소 쓰기 실패는 예외(run 이 받는다)
    #--------------------------------------------------------------
    def index_doc(self, path):
        if not path:
            return {"ok": False, "why": "경로가 없습니다"}
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return {"ok": False, "path": path, "why": "파일이 없습니다"}
        if os.path.splitext(path)[1].lower() not in indexer.TEXT_EXTS or indexer.is_transient(path):
            return {"ok": False, "path": path, "why": "인덱싱 대상이 아닌 파일입니다"}

        state = self._state()
        fp = indexer.file_fingerprint(path)
        prev = state["docs"].get(path)
        if prev and prev.get("fingerprint") == fp:
            return {"ok": True, "path": path, "result": "unchanged", "chunks": prev.get("chunks", 0)}
        f = state.get("failed", {}).get(path)
        if f and f.get("fingerprint") == fp:
            # 지난번에 실패했고 그 뒤로 안 바뀌었다 — 같은 실패를 되풀이하지 않는다
            return {"ok": False, "path": path, "result": "failed_before", "why": f.get("why", "")}

        if self._ext is None:
            self._ext = indexer.build_extractor()
        self.app.embedder.ensure_loaded()
        r = indexer.sync_doc(path, fp, state, self.app.embedder, self.app.store, self._ext)
        out = {"ok": r["ok"], "path": path, "result": r["op"], "chunks": r.get("chunks", 0)}
        if not r["ok"]:
            out["why"] = r.get("why", "")
            out["kept_old"] = bool(r.get("kept_old"))
        return out

    #--------------------------------------------------------------
    # 문서 한 건 빼기
    #=> ⚠️ 파일이 실제로 있으면 거절한다. 잘못된 명령 하나로 멀쩡한 문서가 인덱스에서
    #   빠지면 안 된다. 여러 건을 한꺼번에 뺄지는 /index-plan 의 안전 확인이 정한다.
    #
    # -in: path = 문서 경로(상태 파일의 키와 대소문자가 달라도 된다)
    #
    # -out: {ok, removed(청크 수), why}
    # -out: error = 저장소 쓰기 실패는 예외(run 이 받는다)
    #--------------------------------------------------------------
    def remove_doc(self, path):
        if not path:
            return {"ok": False, "why": "경로가 없습니다"}
        if os.path.exists(path):
            return {"ok": False, "path": path, "why": "파일이 아직 있어 빼지 않습니다"}
        state = self._state()
        want = indexer.norm_path(path)
        key = next((k for k in state["docs"] if indexer.norm_path(k) == want), None)
        if key is None:
            return {"ok": True, "path": path, "removed": 0, "result": "not_indexed"}
        n = indexer.remove_doc(key, state, self.app.store)
        return {"ok": True, "path": key, "removed": n, "result": "removed"}

    #--------------------------------------------------------------
    # BM25 다시 만들기 (+ 고아 청소)
    #=> Bm25Index.build 는 같은 객체의 내용을 바꾼다. 검색기(HybridRetriever)가 그 객체를
    #   들고 있으므로 다음 질문부터 곧바로 새 BM25 로 찾는다(따로 바꿔 끼울 것이 없다).
    #
    # -in: 없음
    #
    # -out: {ok, chunks, orphans, build_s}
    # -out: error = 저장소 읽기 실패는 예외(run 이 받는다)
    #--------------------------------------------------------------
    def rebuild_bm25(self):
        state = self._state()
        logs = []
        texts, ids, n_orphans = indexer.collect_for_bm25(self.app.store, state, logs.append)
        self.app.embedder.ensure_loaded()
        build_s = self.app.bm25.build(texts, ids, self.app.embedder.tokenizer) if texts else 0.0
        state["bm25_built_at"] = time.time()
        indexer.save_state(state)
        out = {"ok": True, "chunks": len(texts), "orphans": n_orphans,
               "build_s": round(build_s, 2)}
        if logs:
            out["note"] = " / ".join(l.strip() for l in logs)[:300]
        return out

    #--------------------------------------------------------------
    # 할 일 목록 (인덱스는 안 건드린다)
    #=> RAGSearchBox 가 "무엇을 보낼지" 정하는 데 쓴다. 대조 규칙은 index 명령과 같다
    #   (indexer.plan_changes). 폴더가 안 보이면 삭제 후보를 만들지 않는다.
    #
    # -in: folder = 지정 폴더
    #
    # -out: {ok, add:[경로], modify:[경로], remove:[키], delete_blocked, retry_skip, bm25_stale}
    # -out: error = 없음 (폴더가 없으면 ok=False)
    #--------------------------------------------------------------
    def plan(self, folder):
        if not folder or not os.path.isdir(folder):
            # 드라이브가 빠졌을 수 있다 — "전부 사라졌다" 로 보고하면 안 된다
            return {"ok": False, "folder": folder, "why": "폴더가 보이지 않습니다"}
        state = self._state()
        p = indexer.plan_changes(folder, state)
        add = [path for path, _ in p["todo"] if os.path.abspath(path) not in state["docs"]]
        modify = [path for path, _ in p["todo"] if os.path.abspath(path) in state["docs"]]
        return {"ok": True, "folder": os.path.abspath(folder), "files": len(p["files"]),
                "add": add, "modify": modify, "remove": p["removed"],
                "delete_blocked": p["delete_blocked"], "retry_skip": p["retry_skip"],
                "bm25_stale": indexer.bm25_stale(state)}
