#------------------------------------------------------------------
# 따라다니는 오른쪽 패널 (설계서 §17)
#=> 답변 창이 탐색기 파일 목록을 덮는 것이 거슬려서, 미리보기 창처럼 오른쪽에 붙여 둔다.
#   탐색기 안으로 진짜 도킹하는 길은 없다(§17 표) — 그래서 "별도 창을 붙여 두고
#   탐색기를 따라다니게" 한다.
#
#   위치 규칙
#    1) 탐색기 창 오른쪽 바깥에 자리가 있으면 거기 — 아무것도 가리지 않는다
#    2) 없으면 창 안쪽 오른쪽 가장자리 — 목록 일부를 덮지만 화면 밖으로 나가진 않는다
#    3) 어느 쪽이든 그 모니터의 작업 영역 안으로 당긴다(작업 표시줄을 침범하지 않게)
#
#   높이는 탐색기 창 높이에 맞춘다. 떠다니는 창에 쓰던 MaxHeight 는 여기서 쓰지 않는다 —
#   덜 찬 패널은 도킹처럼 보이지 않고 고장난 것처럼 보이기 때문이다(설계서 §17 에 적어 둠).
#
#   이 파일의 계산 부분은 창이 없어도 시험할 수 있게 순수 함수로 두었다.
#------------------------------------------------------------------

import log as rsb_log

GAP = 6            # 탐색기 창과 패널 사이 틈
EDGE = 4           # 창 안쪽에 붙일 때 가장자리에서 띄우는 정도
MIN_HEIGHT = 160   # 이보다 낮으면 보여 줄 것이 없다


#------------------------------------------------------------------
# 패널 자리 계산 (핵심, 순수 함수)
#=> 탐색기 창 사각형과 모니터 작업 영역을 받아 패널이 놓일 자리를 정한다.
#
# -in: win  = 탐색기 창 (left, top, right, bottom)
# -in: work = 그 창이 있는 모니터의 작업 영역 (left, top, right, bottom)
# -in: width = 패널 너비
#
# -out: (x, y, w, h) — 패널 위치와 크기
# -out: error = 없음 (창이 이상해도 작업 영역 안의 값을 돌려준다)
#------------------------------------------------------------------
def panel_rect(win, work, width):
    wl, wt, wr, wb = win
    kl, kt, kr, kb = work

    # 높이: 탐색기 창 높이에 맞추되 화면(작업 영역) 밖으로는 나가지 않는다
    top = max(wt, kt)
    bottom = min(wb, kb)
    h = max(MIN_HEIGHT, bottom - top)
    if top + h > kb:
        top = max(kt, kb - h)

    w = max(200, int(width))

    # ① 창 오른쪽 바깥에 자리가 있으면 거기 — 목록을 가리지 않는다
    outside = wr + GAP
    if outside + w <= kr:
        return int(outside), int(top), int(w), int(h)

    # ② 없으면 창 안쪽 오른쪽 가장자리
    inside = wr - w - EDGE
    x = max(kl, min(inside, kr - w))
    return int(x), int(top), int(w), int(h)


#------------------------------------------------------------------
# 창이 있는 모니터의 작업 영역 얻기
#=> 다중 모니터에서 엉뚱한 화면으로 튀지 않게, 그 창이 놓인 모니터를 기준으로 삼는다.
#   작업 표시줄을 뺀 영역(work area)을 쓴다.
#
# -in: hwnd = 기준 창
#
# -out: (left, top, right, bottom). 못 구하면 주 화면 크기
# -out: error = 없음
#------------------------------------------------------------------
def work_area(hwnd):
    try:
        import win32api
        import win32con
        mon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        return tuple(win32api.GetMonitorInfo(mon)["Work"])
    except Exception:
        try:
            import win32api
            return (0, 0, win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))
        except Exception:
            return (0, 0, 1920, 1080)


#------------------------------------------------------------------
# 대상 창의 상태 보기
#=> 패널을 어떻게 할지 정하는 데 필요한 것만 한 번에 읽는다.
#
# -in: hwnd = 대상 탐색기 창
#
# -out: (살아있나, 최소화됐나, 창 사각형 또는 None)
# -out: error = 없음 (읽기 실패는 (False, False, None))
#------------------------------------------------------------------
def target_state(hwnd):
    try:
        import win32gui
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False, False, None
        if win32gui.IsIconic(hwnd):
            return True, True, None
        if not win32gui.IsWindowVisible(hwnd):
            return True, True, None
        return True, False, tuple(win32gui.GetWindowRect(hwnd))
    except Exception:
        return False, False, None


#------------------------------------------------------------------
# 패널을 그 자리로 옮기기 (z순서까지)
#=> 대상 창 바로 위 에 끼워 넣는다. "위" 라는 것이 중요하다 —
#   대상 창 아래(SetWindowPos 의 두 번째 인자로 대상 창을 주면 그렇게 된다)에 넣으면
#   창 안쪽에 붙였을 때 탐색기에 가려 아예 안 보인다.
#   대상 바로 위에 두면, 다른 프로그램이 앞으로 나올 때는 그 프로그램이 패널보다
#   위로 가므로 "다른 프로그램 위에 혼자 떠 있는" 일도 없다(설계서 §17).
#
#   ⚠️ 활성화하지 않는다(SWP_NOACTIVATE) — 포커스를 뺏지 않는다는 원칙은 여기서도 같다.
#
# -in: panel_hwnd  = 패널 창
# -in: target_hwnd = 붙어 다닐 탐색기 창
# -in: rect        = (x, y, w, h)
#
# -out: True = 옮겼다
# -out: error = 없음 (실패는 False)
#------------------------------------------------------------------
def place_above_target(panel_hwnd, target_hwnd, rect):
    try:
        import win32con
        import win32gui
        x, y, w, h = rect
        flags = win32con.SWP_NOACTIVATE

        # 대상 창 바로 위에 있는 창을 찾아 그 뒤에 넣으면, 결과적으로 대상 바로 위가 된다
        try:
            above = win32gui.GetWindow(target_hwnd, win32con.GW_HWNDPREV)
        except Exception:
            above = 0

        if above == panel_hwnd:
            # 이미 대상 바로 위에 있다 — z순서는 건드리지 않는다(깜빡임 방지)
            insert_after = 0
            flags |= win32con.SWP_NOZORDER
        else:
            insert_after = above if above else win32con.HWND_TOP

        win32gui.SetWindowPos(panel_hwnd, insert_after, x, y, w, h, flags)
        return True
    except Exception:
        rsb_log.get("dock").exception("패널을 옮기지 못했다")
        return False


#------------------------------------------------------------------
# 패널이 차지할 오른쪽 띠 (§18)
#=> 새 패널은 탐색기와 겹치지 않는다. 화면 오른쪽 끝에 세로 띠를 잡고,
#   탐색기는 그 왼쪽 영역으로 물러나게 한다.
#
# -in: work  = 모니터 작업 영역 (left, top, right, bottom)
# -in: width = 패널 너비
#
# -out: (x, y, w, h)
# -out: error = 없음
#------------------------------------------------------------------
def right_strip(work, width):
    kl, kt, kr, kb = work
    w = max(200, min(int(width), (kr - kl) // 2))      # 화면 절반은 넘지 않게
    return int(kr - w), int(kt), int(w), int(kb - kt)


#------------------------------------------------------------------
# 탐색기가 쓸 왼쪽 영역
#=> 오른쪽 띠를 뺀 나머지. 여기에 탐색기 창을 맞춰 넣는다.
#
# -in: work  = 작업 영역
# -in: width = 패널 너비
#
# -out: (left, top, right, bottom)
# -out: error = 없음
#------------------------------------------------------------------
def left_region(work, width):
    kl, kt, kr, kb = work
    x, y, w, h = right_strip(work, width)
    return int(kl), int(kt), int(x - GAP), int(kb)


#------------------------------------------------------------------
# 창이 왼쪽 영역 안에 있는가
#=> 이미 안쪽이면 건드리지 않는다 — 사용자가 놓아둔 자리를 함부로 바꾸지 않기 위해서다.
#
# -in: rect   = 창 사각형
# -in: region = 왼쪽 영역
#
# -out: bool
# -out: error = 없음
#------------------------------------------------------------------
def fits_in(rect, region):
    l, t, r, b = rect
    rl, rt, rr, rb = region
    return l >= rl - 2 and t >= rt - 2 and r <= rr + 2 and b <= rb + 2


#------------------------------------------------------------------
# 지금 창 상태 기억해 두기 (되돌리려고)
#=> 최대화 여부와 사각형을 함께 담는다.
#
# -in: hwnd = 대상 창
#
# -out: {"maximized": bool, "rect": (l,t,r,b)} 또는 None
# -out: error = 없음
#------------------------------------------------------------------
def snapshot(hwnd):
    try:
        import win32con
        import win32gui
        place = win32gui.GetWindowPlacement(hwnd)
        return {"maximized": place[1] == win32con.SW_SHOWMAXIMIZED,
                "rect": tuple(win32gui.GetWindowRect(hwnd))}
    except Exception:
        return None


#------------------------------------------------------------------
# 탐색기를 왼쪽으로 물려 자리 만들기 (§18)
#=> 최대화되어 있으면 최대화를 풀고 왼쪽 영역에 맞춘다.
#   최대화가 아니면 폭만큼만 밀거나 줄인다 — 필요 이상으로 창을 흔들지 않는다.
#
# -in: hwnd   = 탐색기 창
# -in: region = 왼쪽 영역 (left, top, right, bottom)
#
# -out: True = 창을 옮겼다(되돌릴 거리가 생겼다)
# -out: error = 없음 (실패는 False)
#------------------------------------------------------------------
def make_room(hwnd, region):
    try:
        import win32con
        import win32gui

        rl, rt, rr, rb = region
        place = win32gui.GetWindowPlacement(hwnd)
        was_max = place[1] == win32con.SW_SHOWMAXIMIZED
        if was_max:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if was_max:
            # 최대화였다면 왼쪽 영역을 꽉 채운다 — 쓰던 넓이를 최대한 지켜 준다
            nl, nt, nw, nh = rl, rt, rr - rl, rb - rt
        else:
            nw = min(r - l, rr - rl)
            nh = min(b - t, rb - rt)
            nl = min(max(l, rl), rr - nw)      # 오른쪽 띠를 넘지 않게 왼쪽으로 민다
            nt = min(max(t, rt), rb - nh)
            if (nl, nt, nw, nh) == (l, t, r - l, b - t):
                return False                   # 이미 자리 안에 있다
        win32gui.MoveWindow(hwnd, int(nl), int(nt), int(nw), int(nh), True)
        return True
    except Exception:
        rsb_log.get("dock").exception("탐색기 자리를 만들지 못했다")
        return False


#------------------------------------------------------------------
# 옮겨 둔 창을 원래대로
#=> 사용자가 그 뒤에 창을 직접 옮겼으면 되돌리지 않는다 — 사용자의 손을 이긴다는 인상을 주지 않게.
#
# -in: hwnd  = 대상 창
# -in: saved = snapshot() 결과
# -in: ours  = 우리가 옮겨 놓은 사각형(그대로면 되돌린다)
#
# -out: True = 되돌렸다
# -out: error = 없음
#------------------------------------------------------------------
def restore(hwnd, saved, ours=None):
    if not saved:
        return False
    try:
        import win32con
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return False
        now = tuple(win32gui.GetWindowRect(hwnd))
        if ours and any(abs(a - b) > 4 for a, b in zip(now, ours)):
            return False                       # 사용자가 그 뒤에 옮겼다 — 그대로 둔다
        if saved["maximized"]:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        else:
            l, t, r, b = saved["rect"]
            win32gui.MoveWindow(hwnd, int(l), int(t), int(r - l), int(b - t), True)
        return True
    except Exception:
        return False
