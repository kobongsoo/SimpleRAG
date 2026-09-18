#------------------------------------------------------------------
# 가짜 simplerag index — 자동 인덱싱의 "워커가 내려가 있을 때" 길을 시험한다
#=> 진짜처럼 "@index-summary {json}" 한 줄을 낸다. 받은 인자를 그대로 적어 두어
#   시험이 --max-add·--allow-delete 가 제대로 붙었는지 볼 수 있게 한다.
#
#   환경변수
#    FAKE_ARGS_LOG : 받은 인자를 이 파일에 한 줄씩 덧붙인다
#    FAKE_NEW      : 새 문서 수 — --max-add 보다 많으면 held_new 로 알린다
#    FAKE_FAIL     : 1 이면 잠금 오류처럼 실패한다(종료 코드 1)
#------------------------------------------------------------------
import json
import os
import sys


def main(argv):
    log = os.environ.get("FAKE_ARGS_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv, ensure_ascii=False) + "\n")
    if os.environ.get("FAKE_FAIL"):
        print("@index-summary " + json.dumps({"op": "index", "ok": False, "why": "잠금"}))
        return 1
    new = int(os.environ.get("FAKE_NEW", "0"))
    max_add = int(argv[argv.index("--max-add") + 1]) if "--max-add" in argv else None
    held = new if (max_add is not None and new > max_add) else 0
    added = 0 if held else new
    print("문서 처리 중...")
    print("@index-summary " + json.dumps({"op": "index", "ok": True, "added": added, "modified": 1,
                                          "removed": 0, "same": 0, "held_new": held,
                                          "delete_blocked": "", "failed": 0}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
