"""표 행을 "머리글: 값" 평문으로 펴는 모듈 (계획서 3-3)."""

import re

# 표 한 줄 — normalize_tables 가 만든 "| 셀 | 셀 |" 형태
_TABLE_LINE = re.compile(r"^[ \t]*\|.*\|[ \t]*$")

# 숫자만 든 칸(금액·일수). 머리글 판정과 밀림 판정에 쓴다.
_NUMERIC = re.compile(r"[-\d,.\s]*[\d][-\d,.\s]*")

# 이보다 긴 칸은 표의 값이 아니라 본문 덩어리다. PDF·Word 추출에서 표 뒤의
# 설명 문단이 마지막 셀에 통째로 딸려 오는 일이 잦다(REPORT §45 근거1 관찰).
_MAX_CELL_CHARS = 40

# 한 행에 이보다 많은 칸이 있으면 표가 아니라 정렬용 탭 덩어리로 본다.
_MAX_COLS = 12


#------------------------------------------------------------------
# 표 한 줄을 칸 목록으로
#=> "| 구 분 | 가족부임시 | 25,000 |" → ["구 분", "가족부임시", "25,000"]
#    1) 양 끝 공백과 바깥쪽 파이프를 떼고
#    2) 파이프로 쪼갠 뒤 칸마다 공백을 정리한다
#
# -in: line = 표 한 줄
#
# -out: cells = 칸 문자열 목록(빈 칸도 "" 로 자리를 지킨다)
# -out: error = 없음 (표가 아니어도 그냥 쪼갠 결과를 낸다)
#------------------------------------------------------------------
def _cells(line):
    inner = line.strip()
    # 바깥쪽 파이프만 뗀다 — 안쪽 파이프는 칸 구분자라 남겨야 한다.
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


#------------------------------------------------------------------
# 숫자만 든 칸인가
#=> "25,000" · "5" · "-" 은 값이고, "대리(선임)" · "가족부임시" 는 이름표다.
#   밀린 행을 되돌릴 때 "앞으로 나온 칸이 이름표인가"를 이걸로 가른다.
#
# -in: cell = 칸 문자열
#
# -out: bool = 숫자(와 구두점)만으로 이루어졌으면 True
# -out: error = 없음
#------------------------------------------------------------------
def _is_number(cell):
    return bool(cell) and bool(_NUMERIC.fullmatch(cell))


#------------------------------------------------------------------
# 머리글 줄인가
#=> 머리글 칸은 이름표라 숫자가 들어가지 않는다(structure._looks_like_header 와 같은 기준).
#
# -in: cells = 칸 목록
#
# -out: bool = 머리글처럼 보이면 True
# -out: error = 없음
#------------------------------------------------------------------
def _is_header(cells):
    named = [c for c in cells if c]
    if len(named) < 2:
        return False
    return not any(_is_number(c) for c in named)


#------------------------------------------------------------------
# 밀린 행 되돌리기 — 3-3 의 핵심
#=> Word 의 세로 병합 셀 때문에 값 행 앞에 **빈 칸이 하나 더** 붙어 나온다.
#   그대로 두면 머리글과 한 칸씩 어긋나 모델이 옆 칸 값을 집는다(g048).
#
#       머리글 : 구 분 | 가족부임시 | 단독부임시 | 비 고
#       원래 행:  (빈) | 대리(선임) | 25,000    | 15,000
#                      ^^^^^^^^^^^^ 이름표가 "가족부임시" 자리에 와 있다
#       고친 행: 대리(선임) | 25,000 | 15,000 | (빈)
#
#    1) 앞이 비어 있고 그 다음 칸이 **이름표(숫자가 아님)** 면 한 칸 왼쪽으로 민다.
#       다음 칸이 숫자면 "이름표가 위 행에서 이어지는 정상적인 빈 칸"이므로 두지 않는다.
#           | 임원 | 1 | 2 |
#           |      | 3 | 4 |   ← 이건 3 이 그대로 둘째 열 값이다. 밀면 안 된다.
#    2) 칸 수가 머리글보다 적으면 뒤를 빈 칸으로 채운다("| 임원 | 50,000 | 40,000 |").
#
# -in: cells  = 값 행의 칸 목록
# -in: header = 머리글 칸 목록
#
# -out: cells = 머리글과 길이가 맞춰진 칸 목록
# -out: error = 칸 수가 머리글보다 많으면 None (해석을 포기하고 원문을 둔다)
#------------------------------------------------------------------
def _align(cells, header):
    cells = list(cells)

    # 뒤쪽 빈 칸은 자리만 차지하므로 먼저 턴다 — 길이 비교를 방해한다.
    while cells and not cells[-1]:
        cells.pop()
    if not cells:
        return None

    # (1) 병합 셀 때문에 오른쪽으로 밀린 행을 되돌린다.
    #     머리글 첫 칸이 이름표 열일 때만 — 첫 열이 숫자 열인 표에는 해당이 없다.
    if (len(cells) > 1 and not cells[0] and header and not _is_number(header[0])
            and not _is_number(cells[1])):
        cells.pop(0)

    if len(cells) > len(header):
        return None

    # (2) 뒤쪽 열(보통 "비 고")이 비어 생략된 행을 채운다.
    cells += [""] * (len(header) - len(cells))
    return cells


#------------------------------------------------------------------
# 값 행 하나를 평문으로
#=> "| 대리(선임) | 25,000 | 15,000 |" 를
#   "구 분: 대리(선임) / 가족부임시: 25,000 / 단독부임시: 15,000" 으로 바꾼다.
#   이렇게 하면 행 하나만 봐도 어느 값이 무엇인지 알 수 있어, 모델이 머리글을
#   세로로 세어 맞출 필요가 없어진다.
#
#    1) 빈 값은 통째로 뺀다(있으나 마나 한 "비 고: " 를 만들지 않는다)
#    2) 머리글이 비어 있는 열은 값만 쓴다
#
# -in: cells  = 머리글과 길이가 맞춰진 칸 목록
# -in: header = 머리글 칸 목록
#
# -out: text = 평문 한 줄 (쓸 값이 없으면 "")
# -out: error = 없음
#------------------------------------------------------------------
def _render(cells, header):
    parts = []
    for name, value in zip(header, cells):
        if not value:
            continue
        parts.append("{}: {}".format(name, value) if name else value)
    return " / ".join(parts)


#------------------------------------------------------------------
# 표가 든 본문을 행 단위 평문으로 (계획서 3-3 — 표 열 혼동 대책)
#=> 모델이 표에서 옆 칸 값을 집는 실패(g048: 대리 15,000 을 25,000 으로)를 없앤다.
#   원인은 두 가지가 겹친 것이다.
#     ① Word 병합 셀 탓에 값 행이 한 칸씩 밀려 머리글과 어긋난다
#     ② 밀리지 않았더라도 모델이 열 위치를 세어 맞춰야 한다
#   ①은 _align 이, ②는 _render 가 없앤다.
#
#    1) 표 줄이 이어지는 구간(run)을 찾는다
#    2) 구간의 첫 머리글 줄을 이름표로 삼는다
#    3) 값 행마다 밀림을 되돌리고 "머리글: 값" 으로 편다
#    4) 모든 행을 폈으면 머리글 줄은 뺀다(행마다 이름이 들어가 중복이다)
#
#   ⚠️ 안전장치 — 하나라도 어긋나면 그 구간은 **원문 그대로** 둔다.
#      · 머리글을 못 찾은 구간
#      · 40자를 넘는 칸이 있는 행(표 뒤 설명 문단이 셀에 딸려 온 경우)
#      · 칸 수가 머리글보다 많은 행
#      마지막으로 글자 수가 줄지 않았는지 확인한다(extract.normalize_tables 와 같은 규칙).
#
# -in: text = 청크 본문(표가 없으면 그대로 돌려준다)
#
# -out: text = 표 구간이 평문으로 바뀐 본문
# -out: error = 없음 (해석할 수 없으면 원문을 그대로 낸다)
#------------------------------------------------------------------
def flatten_tables(text):
    if not text or "|" not in text:
        return text

    lines = text.split("\n")
    out, i, changed = [], 0, False

    while i < len(lines):
        if not _TABLE_LINE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue

        # 표 줄이 이어지는 구간을 통째로 잡는다 — 머리글은 구간의 첫 줄에 있다.
        j = i
        while j < len(lines) and _TABLE_LINE.match(lines[j]):
            j += 1
        run = lines[i:j]
        flat = _flatten_run(run)
        if flat is None:
            out.extend(run)
        else:
            out.extend(flat)
            changed = True
        i = j

    if not changed:
        return text

    result = "\n".join(out)
    # 무손실 확인 — 펴는 작업은 이름표를 덧붙일 뿐 값을 지우지 않는다.
    if _content(result) < _content(text):
        return text
    return result


#------------------------------------------------------------------
# 표 구간 하나를 펴기
#=> flatten_tables 가 잡아 준 "표 줄만 모인 구간"을 평문 줄 목록으로 바꾼다.
#
# -in: run = 표 줄 목록(머리글 줄 포함)
#
# -out: lines = 평문 줄 목록
# -out: error = 펼 수 없으면 None (호출한 쪽이 원문을 그대로 쓴다)
#------------------------------------------------------------------
def _flatten_run(run):
    rows = [_cells(ln) for ln in run]

    # 머리글이 없으면 이름을 붙일 수 없다 — 손대지 않는다.
    head_at = next((k for k, r in enumerate(rows) if _is_header(r)), None)
    if head_at is None:
        return None
    header = rows[head_at]
    if len(header) > _MAX_COLS:
        return None

    out, all_flat = [], True
    for k, cells in enumerate(rows):
        if k == head_at:
            out.append(("H", run[k]))
            continue
        # 본문 문단이 셀에 딸려 온 행은 표로 해석하지 않는다.
        if any(len(c) > _MAX_CELL_CHARS for c in cells):
            out.append(("R", run[k]))
            all_flat = False
            continue
        aligned = _align(cells, header)
        if aligned is None:
            out.append(("R", run[k]))
            all_flat = False
            continue
        line = _render(aligned, header)
        if not line:
            out.append(("R", run[k]))
            all_flat = False
            continue
        out.append(("F", line))

    if not any(tag == "F" for tag, _ in out):
        return None

    # 전부 폈으면 머리글은 행마다 들어갔으니 뺀다. 하나라도 원문이 남았으면
    # 그 행을 읽으려면 머리글이 필요하므로 남긴다.
    return [t for tag, t in out if not (all_flat and tag == "H")]


#------------------------------------------------------------------
# 내용 글자 수 (공백·칸 구분자 제외)
#=> 무손실 확인용. 표를 펴면 칸 구분자 "|" 가 사라지고 공백 배치도 달라지므로
#   둘 다 빼고 센다. 이걸 빼먹으면 "| A | B |" → "A: B" 처럼 제대로 편 결과가
#   글자 수 감소로 보여 무손실 검사가 헛돈다.
#
# -in: s = 문자열
#
# -out: int = 공백과 "|" 를 제외한 글자 수
# -out: error = 없음
#------------------------------------------------------------------
def _content(s):
    return len(re.sub(r"[\s|]+", "", s))
