#------------------------------------------------------------------
# 아이콘 만들기 (설계서 §12 — 원본 res\MDriveSearchBox.ico 자리)
#=> 트레이와 exe 에 쓸 RAGSearchBox.ico 를 그린다. 그림 파일을 따로 관리하지 않고
#   코드로 만들어 두면 색·크기를 바꿀 때 다시 그리기만 하면 된다.
#
#   작은 크기가 중요하다. 트레이 아이콘은 16px 로 보이는데, 큰 그림을 줄이면
#   돋보기 안의 물음표가 뭉개진다. 그래서 크기대로 따로 그린다 —
#   16·20·24 는 물음표 없이 돋보기만, 32 이상은 물음표까지 그린다.
#
#   사용: .venv\Scripts\python.exe RAGSearchBox\make_icon.py
#------------------------------------------------------------------

import io
import os
import struct

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "RAGSearchBox.ico")

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
BG = (31, 111, 235, 255)          # 파랑 — 탐색기 계열 색과 구분되게
FG = (255, 255, 255, 255)
SUPER = 8                          # 이만큼 크게 그린 뒤 줄여서 계단을 없앤다


#------------------------------------------------------------------
# 한 크기짜리 아이콘 한 장 그리기
#=> 둥근 사각형 바탕 + 돋보기(테두리 원 + 손잡이). 크면 물음표도 넣는다.
#    1) size*SUPER 로 크게 그린다(선이 매끄러워진다)
#    2) LANCZOS 로 줄인다
#
# -in: size = 만들 가로·세로 픽셀
#
# -out: PIL 이미지(RGBA)
# -out: error = 없음
#------------------------------------------------------------------
def draw(size):
    s = size * SUPER
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 바탕 — 모서리를 둥글게
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BG)

    # 돋보기 렌즈(테두리 원)
    pad = s * 0.20
    ring = [pad, pad, s * 0.74, s * 0.74]
    width = max(1, int(s * 0.075))
    d.ellipse(ring, outline=FG, width=width)

    # 손잡이 — 렌즈 오른쪽 아래에서 바깥으로
    d.line([s * 0.66, s * 0.66, s * 0.86, s * 0.86], fill=FG,
           width=width, joint="curve")

    # 작은 아이콘은 여기까지. 물음표를 넣으면 16px 에서 뭉개진다.
    if size >= 32:
        _question(d, ring, s)

    return img.resize((size, size), Image.LANCZOS)


#------------------------------------------------------------------
# 렌즈 안에 물음표 그리기
#=> 글꼴에 기대지 않고 선으로 직접 그린다. PC 마다 글꼴이 달라도 같게 나오게 하려는 것이다.
#    1) 위쪽 갈고리는 원호 두 개로
#    2) 아래 점은 원으로
#
# -in: d    = ImageDraw
# -in: ring = 렌즈 사각형 [x0,y0,x1,y1]
# -in: s    = 그리는 판 크기
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _question(d, ring, s):
    cx = (ring[0] + ring[2]) / 2
    cy = (ring[1] + ring[3]) / 2
    r = (ring[2] - ring[0]) / 2
    w = max(1, int(s * 0.055))

    # 갈고리 — 정원(正圓) 위에 그려야 물음표처럼 보인다. 찌그러진 타원에 그리면 느낌표가 된다.
    # PIL 각도는 3시가 0도이고 시계 방향이다. 180(9시) → 270(12시) → 0(3시) → 45 로 돈다.
    rh = r * 0.36
    hy = cy - r * 0.30
    d.arc([cx - rh, hy - rh, cx + rh, hy + rh], start=180, end=45, fill=FG, width=w)

    # 갈고리 끝에서 가운데 아래로 내려오는 획
    import math
    ex = cx + rh * math.cos(math.radians(45))
    ey = hy + rh * math.sin(math.radians(45))
    d.line([ex, ey, cx + w * 0.2, cy + r * 0.28], fill=FG, width=w, joint="curve")

    # 아래 점 — 획과 사이를 띄워야 물음표로 읽힌다
    dot = r * 0.11
    d.ellipse([cx + w * 0.2 - dot, cy + r * 0.56 - dot,
               cx + w * 0.2 + dot, cy + r * 0.56 + dot], fill=FG)


#------------------------------------------------------------------
# .ico 파일 쓰기
#=> 크기마다 다른 그림을 넣어야 해서 PIL 의 save(sizes=...) 를 쓰지 않고 직접 묶는다.
#   (그 방식은 한 장을 줄여 여러 크기를 만든다.)
#   각 장은 PNG 로 넣는다 — Windows Vista 이후가 읽는 방식이다.
#
# -in: path   = 저장 경로
# -in: images = PIL 이미지 목록(작은 것부터)
#
# -out: 만든 파일 크기(바이트)
# -out: error = 없음 (쓰기 실패는 예외 전파)
#------------------------------------------------------------------
def write_ico(path, images):
    blobs = []
    for im in images:
        b = io.BytesIO()
        im.save(b, format="PNG")
        blobs.append(b.getvalue())

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for im, blob in zip(images, blobs):
        w = 0 if im.width >= 256 else im.width       # 256 은 0 으로 적는 규칙
        h = 0 if im.height >= 256 else im.height
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)

    with open(path, "wb") as f:
        f.write(header + entries + b"".join(blobs))
    return os.path.getsize(path)


#------------------------------------------------------------------
# 실행
#
# -in: 없음
#
# -out: 0
# -out: error = 없음
#------------------------------------------------------------------
def main():
    images = [draw(n) for n in SIZES]
    size = write_ico(OUT, images)
    print("만들었습니다: {} ({:,} 바이트, {} 크기)".format(OUT, size, len(SIZES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
