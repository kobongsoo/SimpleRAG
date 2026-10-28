#------------------------------------------------------------------
# 정밀 모드 켜고 끄기 — SimpleRAG config.yaml 의 generation.precision 한 줄만 바꾼다
#=> 정밀 모드 = Qwen3-4B 모델 + 근거에 앞뒤 이웃 청크(코드수정 설계서 §7 ④). 골든 100문항 정답 50 → 59,
#   대신 첫 글자(TTFT) 약 3배·답 완료 약 2배 늦다. 워커는 시작할 때 config.yaml 을 읽으므로
#   바꾼 뒤에는 모델을 다시 올려야 한다(app 이 맡는다).
#   yaml 로 읽어 다시 쓰면 설명 주석이 모두 날아가므로, generation 구역의 precision 줄 값만 갈아 끼운다.
#   Rust 판 rust/ragsearchbox/src/precision.rs 와 같은 규칙.
#   같은 방식으로 멀티턴(generation.multiturn, 기본 켬)도 켜고 끈다 — read/save 의 key 로 고른다.
#------------------------------------------------------------------

import io
import os
import re

MODEL_FILE = "Qwen3-4B-Q4_K_M.gguf"
# 설정 창에 늘 보이는 주의 글
WARN = "⚠️ 켜면 느려집니다 — 첫 글자(TTFT)가 2배 이상(약 2.2 → 7.1초), 답 완료가 약 2배(7.6 → 14.7초). 바꾸면 모델을 다시 올립니다."
# 멀티턴 안내 글
MULTITURN_NOTE = ("따라 묻는 질문(\"침구류는?\")의 빠진 주제를 같은 대화의 바로 앞 질문에서 채워 찾습니다. "
                  "새 대화(✎)를 누르면 끊깁니다. 바꾸면 모델을 다시 올립니다.")
# 설정 창에서 켜고 끄는 키와 기본값
DEFAULTS = {"precision": False, "multiturn": True}


#------------------------------------------------------------------
# generation 구역 안의 "  <key>: 값   # 주석" 줄 정규식 — 값만 바꾸고 들여쓰기·주석 정렬은 그대로 둔다
#
# -in: key = 키 이름(precision · multiturn)
#
# -out: 정규식
# -out: error = 없음
#------------------------------------------------------------------
def _line(key):
    return re.compile(r"^(\s+" + re.escape(key) + r":\s*)(\S+)(.*)$")


#------------------------------------------------------------------
# SimpleRAG 가 읽는 config.yaml 찾기
#=> SimpleRAG 와 같은 순서 — SIMPLERAG_CONFIG → 데이터 폴더 → 프로그램 폴더.
#
# -in: data_home = 워커 데이터 폴더(SIMPLERAG_HOME 또는 워커 옆) / app_dir = 워커 프로그램 폴더
#
# -out: 경로, 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def config_path(data_home, app_dir):
    env = (os.environ.get("SIMPLERAG_CONFIG") or "").strip()
    if env and env != "-":
        return os.path.abspath(env) if os.path.isfile(env) else None
    for d in (data_home, app_dir):
        if d and os.path.isfile(os.path.join(d, "config.yaml")):
            return os.path.join(d, "config.yaml")
    return None


#------------------------------------------------------------------
# generation 구역의 precision 줄 번호
#=> 맨 앞에 붙은 "generation:" 부터 다음 맨 앞 구역 이름 전까지에서 찾는다.
#
# -in: lines = 파일 줄 목록 / key = 키 이름(기본 precision)
#
# -out: (generation 줄 번호, 그 키 줄 번호) — 없으면 각각 -1
# -out: error = 없음
#------------------------------------------------------------------
def _find(lines, key="precision"):
    pat = _line(key)
    sec, at = -1, -1
    for i, line in enumerate(lines):
        if line and not line[0].isspace() and not line.startswith("#"):
            if sec >= 0:
                break                        # generation 구역이 끝났다
            if line.split("#", 1)[0].strip() == "generation:":
                sec = i
            continue
        if sec >= 0 and pat.match(line):
            at = i
            break
    return sec, at


#------------------------------------------------------------------
# 지금 켜져 있나 (정밀 모드 · 멀티턴)
#
# -in: path = config.yaml 경로 / key = 키 이름(기본 precision)
#
# -out: True | False (줄이 없거나 못 읽으면 그 키의 기본값 — precision 끔 · multiturn 켬)
# -out: error = 없음
#------------------------------------------------------------------
def read(path, key="precision"):
    default = DEFAULTS.get(key, False)
    try:
        with io.open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except Exception:
        return default
    _sec, at = _find(lines, key)
    if at < 0:
        return default
    # PyYAML 과 같이 true/yes/on 을 참으로 본다
    return _line(key).match(lines[at]).group(2).lower() in ("true", "yes", "on", "y")


#------------------------------------------------------------------
# 켜기/끄기 저장 (정밀 모드 · 멀티턴)
#=> 그 키 줄의 값만 바꾼다. 줄이 없으면 generation: 바로 아래에 만들어 넣는다.
#   원래 줄 끝(CRLF/LF)을 지킨다.
#
# -in: path = config.yaml 경로 / on = 켤지 / key = 키 이름(기본 precision) / label = 안내 글의 이름
#
# -out: (True, 안내 글) 또는 (False, 사유)
# -out: error = 없음 (읽기·쓰기 실패도 사유로 돌려준다)
#------------------------------------------------------------------
def save(path, on, key="precision", label="정밀 모드"):
    if not path or not os.path.isfile(path):
        return False, "config.yaml 을 찾지 못해 바꾸지 않았습니다"
    try:
        with io.open(path, encoding="utf-8", newline="") as f:
            raw = f.read()
    except Exception as e:
        return False, "config.yaml 을 읽지 못했습니다: {}".format(e)
    nl = "\r\n" if "\r\n" in raw else "\n"
    bom = raw.startswith("﻿")
    lines = (raw[1:] if bom else raw).split(nl)
    value = "true" if on else "false"
    sec, at = _find(lines, key)
    if at >= 0:
        m = _line(key).match(lines[at])
        # 값 길이가 바뀌어도 뒤 주석 칸이 밀리지 않게 공백으로 맞춘다(true 4자 / false 5자)
        gap = m.group(3)
        if gap.startswith(" "):
            gap = " " * max(1, len(m.group(2)) + len(gap) - len(gap.lstrip(" ")) - len(value)) + gap.lstrip(" ")
        lines[at] = m.group(1) + value + gap
    elif sec >= 0:
        lines.insert(sec + 1, "  {}: {}".format(key, value))
    else:
        return False, "config.yaml 에 generation 구역이 없어 바꾸지 않았습니다"
    try:
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write(("﻿" if bom else "") + nl.join(lines))
    except Exception as e:
        return False, "config.yaml 에 쓰지 못했습니다: {}".format(e)
    return True, "{}를 {}".format(label, "켰습니다" if on else "껐습니다")


#------------------------------------------------------------------
# 4B 모델 파일이 있나
#=> SimpleRAG 가 찾는 곳(SIMPLERAG_MODELS_DIR → 데이터 폴더 models → 프로그램 폴더 models
#   → 옆 Python 배포본 models)을 본다.
#
# -in: data_home, app_dir = config_path 와 같음
#
# -out: True | False
# -out: error = 없음
#------------------------------------------------------------------
def model_present(data_home, app_dir):
    dirs = [os.environ.get("SIMPLERAG_MODELS_DIR")]
    for d in (data_home, app_dir):
        if d:
            dirs.append(os.path.join(d, "models"))
    if app_dir:
        up = os.path.dirname(os.path.abspath(app_dir))
        dirs += [os.path.join(up, "simplerag", "models"), os.path.join(up, "dist", "simplerag", "models")]
    return any(d and os.path.isfile(os.path.join(d, MODEL_FILE)) for d in dirs)


#------------------------------------------------------------------
# 설정 창 정밀 모드 안내 줄
#
# -in: on = 켜져 있나 / present = 4B 파일이 있나 / found = config.yaml 을 찾았나 / msg = 마지막 결과
#
# -out: 안내 글
# -out: error = 없음
#------------------------------------------------------------------
def note(on, present, found=True, msg=""):
    parts = [WARN]
    if not found:
        parts.append("config.yaml 을 찾지 못해 바꿀 수 없습니다.")
    elif not present:
        parts.append("models 폴더에 {} 가 없어 켜도 기존 모델로 답합니다.".format(MODEL_FILE))
    if msg:
        parts.append(msg)
    return "\n".join(parts)
