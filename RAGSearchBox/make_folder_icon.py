#------------------------------------------------------------------
# 인덱싱 폴더 · 드라이브 아이콘 만들기 (폴더 아이콘 · 탐색기 모드 설계서 §6)
#=> 탐색기에서 "여기는 MoAI 가 읽은 곳"임을 알리는 아이콘 두 벌을 그린다.
#     moai-folder.ico — Windows 11 노란 폴더 + 오른쪽 아래 빨간 M 배지
#     moai-drive.ico  — 회색 드라이브 + 같은 배지
#   폴더 모양을 지켜야 폴더로 읽히므로 배지만 덧붙인다.
#   작은 크기(16 · 20 · 24)는 배지를 크게 키운 별도 그림이다 — 큰 그림을 줄이면 M 이 뭉개진다.
#   그림 파일을 따로 관리하지 않고 코드로 그려 두면 색 · 크기를 바꿀 때 다시 돌리기만 하면 된다.
#
#   사용: .venv\Scripts\python.exe RAGSearchBox\make_folder_icon.py
#   결과: RAGSearchBox\ 와 rust\ragsearchbox\ 에 두 파일(Rust 판은 exe 안에 넣는다)
#------------------------------------------------------------------

import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_icon import write_ico  # noqa: E402  (같은 폴더의 .ico 쓰기를 그대로 쓴다)

OUT_DIRS = [HERE, os.path.join(os.path.dirname(HERE), "rust", "ragsearchbox")]

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SUPER = 8                              # 이만큼 크게 그린 뒤 줄여서 계단을 없앤다

BACK = (227, 162, 49, 255)             # 폴더 뒤판(진한 노랑)
FRONT = (255, 201, 63, 255)            # 폴더 앞판
LIGHT = (255, 218, 117, 255)           # 앞판 윗선(빛 받는 쪽)
BRAND = (229, 22, 63, 255)             # MoAI-S 빨강 #e5163f
WHITE = (255, 255, 255, 255)
DRIVE = (185, 192, 202, 255)           # 드라이브 몸통
DRIVE_TOP = (213, 218, 225, 255)       # 드라이브 윗면
DRIVE_SLOT = (141, 150, 163, 255)      # 드라이브 홈
LED = (63, 191, 111, 255)              # 드라이브 불빛


#------------------------------------------------------------------
# 좌표 비율 → 그리는 판 좌표
#=> 그림은 64칸(작은 그림은 16칸) 격자로 설계했다. 그 칸 좌표를 실제 판 크기로 늘린다.
#
# -in: box  = [x0, y0, x1, y1] (격자 칸 단위)
# -in: s    = 그리는 판 크기(픽셀)
# -in: grid = 격자 칸 수(64 또는 16)
#
# -out: [x0, y0, x1, y1] (픽셀)
# -out: error = 없음
#------------------------------------------------------------------
def _sc(box, s, grid):
    return [v * s / grid for v in box]


#------------------------------------------------------------------
# M 배지 그리기
#=> 흰 테두리 → 빨간 둥근 사각 → 흰 M 획. 흰 테두리가 있어야 노란 폴더 위에서 배지가 떠 보인다.
#
# -in: d     = ImageDraw
# -in: s     = 판 크기
# -in: grid  = 격자 칸 수
# -in: outer = 흰 테두리 사각 [x0,y0,x1,y1] (격자)
# -in: inner = 빨간 사각 [x0,y0,x1,y1] (격자)
# -in: m     = M 획 점 목록 [(x,y), …] (격자)
# -in: w     = M 획 굵기(격자)
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _badge(d, s, grid, outer, inner, m, w):
    k = s / grid
    d.rounded_rectangle(_sc(outer, s, grid), radius=(outer[2] - outer[0]) * 0.21 * k, fill=WHITE)
    d.rounded_rectangle(_sc(inner, s, grid), radius=(inner[2] - inner[0]) * 0.19 * k, fill=BRAND)
    pts = [(x * k, y * k) for x, y in m]
    d.line(pts, fill=WHITE, width=max(1, int(w * k)), joint="curve")
    # 획 끝을 둥글게 — PIL 선은 끝이 각지므로 점을 찍어 메운다
    r = w * k / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=WHITE)


#------------------------------------------------------------------
# 폴더 한 장 그리기
#=> 32px 이상은 64칸 설계(배지가 폴더의 오른쪽 아래 절반), 그보다 작으면 16칸 설계(배지를 더 크게).
#    1) 뒤판(탭 있는 모양) → 앞판 → 앞판 윗선
#    2) M 배지
#    3) LANCZOS 로 줄인다
#
# -in: size = 만들 가로 · 세로 픽셀
#
# -out: PIL 이미지(RGBA)
# -out: error = 없음
#------------------------------------------------------------------
def draw_folder(size):
    s = size * SUPER
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if size >= 32:
        g = 64
        k = s / g
        # 뒤판 — 왼쪽 위에 탭이 솟은 모양(탭과 몸통 두 사각을 겹친다)
        d.rounded_rectangle(_sc([4, 11, 28, 24], s, g), radius=4 * k, fill=BACK)
        d.rounded_rectangle(_sc([4, 17, 60, 54], s, g), radius=4 * k, fill=BACK)
        d.polygon([(22 * k, 11 * k), (27 * k, 11 * k), (32 * k, 17 * k), (22 * k, 17 * k)], fill=BACK)
        d.rounded_rectangle(_sc([4, 22, 60, 54], s, g), radius=4 * k, fill=FRONT)
        d.rounded_rectangle(_sc([4, 22, 60, 25], s, g), radius=1.5 * k, fill=LIGHT)
        _badge(d, s, g, [33, 29, 62, 58], [35.5, 31.5, 59.5, 55.5],
               [(41, 50.5), (41, 37), (47.5, 44.5), (54, 37), (54, 50.5)], 3.4)
    else:
        g = 16
        k = s / g
        d.rounded_rectangle(_sc([1, 2.5, 7.5, 6], s, g), radius=1 * k, fill=BACK)
        d.rounded_rectangle(_sc([1, 4, 15, 14], s, g), radius=1 * k, fill=BACK)
        d.rounded_rectangle(_sc([1, 5.5, 15, 14], s, g), radius=1 * k, fill=FRONT)
        _badge(d, s, g, [6.5, 6, 16, 15.5], [7.3, 6.8, 15.3, 14.8],
               [(9, 13.2), (9, 8.6), (11.3, 11.2), (13.6, 8.6), (13.6, 13.2)], 1.25)
    return img.resize((size, size), Image.LANCZOS)


#------------------------------------------------------------------
# 드라이브 한 장 그리기
#=> 회색 판(윗면 밝게) · 홈 · 초록 불빛 + 같은 M 배지. 작은 크기는 배지를 키운다.
#
# -in: size = 만들 가로 · 세로 픽셀
#
# -out: PIL 이미지(RGBA)
# -out: error = 없음
#------------------------------------------------------------------
def draw_drive(size):
    s = size * SUPER
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if size >= 32:
        g = 64
        k = s / g
        d.rounded_rectangle(_sc([5, 20, 59, 48], s, g), radius=5 * k, fill=DRIVE)
        d.rounded_rectangle(_sc([5, 20, 59, 33], s, g), radius=5 * k, fill=DRIVE_TOP)
        d.rounded_rectangle(_sc([11, 38, 33, 41.5], s, g), radius=1.75 * k, fill=DRIVE_SLOT)
        d.ellipse(_sc([45.6, 37.1, 50.4, 41.9], s, g), fill=LED)
        _badge(d, s, g, [33, 29, 62, 58], [35.5, 31.5, 59.5, 55.5],
               [(41, 50.5), (41, 37), (47.5, 44.5), (54, 37), (54, 50.5)], 3.4)
    else:
        g = 16
        k = s / g
        d.rounded_rectangle(_sc([0.5, 4, 15.5, 11.5], s, g), radius=1.5 * k, fill=DRIVE)
        d.rounded_rectangle(_sc([0.5, 4, 15.5, 7.5], s, g), radius=1.5 * k, fill=DRIVE_TOP)
        d.rounded_rectangle(_sc([2, 9, 6, 10], s, g), radius=0.5 * k, fill=DRIVE_SLOT)
        _badge(d, s, g, [6.5, 6, 16, 15.5], [7.3, 6.8, 15.3, 14.8],
               [(9, 13.2), (9, 8.6), (11.3, 11.2), (13.6, 8.6), (13.6, 13.2)], 1.25)
    return img.resize((size, size), Image.LANCZOS)


#------------------------------------------------------------------
# 실행 — 두 아이콘을 두 폴더에 쓴다
#
# -in: 없음
#
# -out: 0
# -out: error = 없음 (쓰기 실패는 예외 전파)
#------------------------------------------------------------------
def main():
    for name, fn in (("moai-folder.ico", draw_folder), ("moai-drive.ico", draw_drive)):
        images = [fn(n) for n in SIZES]
        for out_dir in OUT_DIRS:
            path = os.path.join(out_dir, name)
            n = write_ico(path, images)
            print("만들었습니다: {} ({:,} 바이트, {} 크기)".format(path, n, len(SIZES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
