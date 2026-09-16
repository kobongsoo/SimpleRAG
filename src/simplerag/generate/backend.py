#------------------------------------------------------------------
# 생성 백엔드 선택 — CPU / iGPU(Vulkan) (REPORT §31, §32)
#=> 같은 llama-cpp-python 0.3.35 인데 네이티브 DLL 을 두 벌 가진다.
#     CPU 판    : llama_cpp 패키지 안의 llama_cpp/lib (기본)
#     Vulkan 판 : <ROOT>/runtime/llama_vulkan (동봉)
#   llama_cpp 는 import 하는 순간 DLL 을 읽고, 한 번 읽은 DLL 은 같은
#   프로세스에서 바꿀 수 없다. 그래서 **llama_cpp 를 import 하기 전에** 어느
#   쪽을 쓸지 정하고 환경변수 LLAMA_CPP_LIB_PATH 로 알려 준다.
#
#   왜 자식 프로세스로 재는가
#    1) Vulkan 판은 vulkan-1.dll 을 직접 링크한다(PE import 표로 확인). 런타임이
#       없거나 드라이버가 깨진 PC 에서 본 프로세스가 적재하다 죽으면 앱이 통째로
#       죽는다. 자식이 대신 죽으면 CPU 로 가면 된다.
#    2) 두 판의 속도를 비교하려면 둘 다 올려야 하는데 한 프로세스엔 하나뿐이다.
#
#   왜 Vulkan 판을 CPU 용으로 겸하지 않는가
#    - n_gpu_layers=0 이어도 Vulkan 판은 큰 행렬곱을 iGPU 로 보낸다(실측 1041토큰
#      2.87초 vs CPU 판 4.0~4.2초). 'CPU 모드'가 사실은 CPU 가 아니다.
#    - vulkan-1.dll 이 없는 PC 에서는 아예 적재가 안 된다.
#
#   왜 '빠른 쪽'을 재서 고르는가
#    - iGPU 는 PC 마다 편차가 크다. 이 노트북(Iris Xe)은 prefill 3.1배지만
#      구형 UHD 는 CPU 보다 느릴 수 있다(§31.2).
#    - iGPU 는 답변 문장이 CPU 와 달라지고 정답률이 조금 내려가는 신호가
#      있었다(−3/−4문항, 비유의, §31.12). 그래서 GPU_MIN_SPEEDUP 배 이상
#      **확실히 빠를 때만** iGPU 를 쓴다.
#
#   결과는 BACKEND_CACHE_PATH 에 저장해 다음 실행부터는 재지 않는다.
#   PC·DLL·모델이 바뀌면 키가 달라져 다시 잰다. 강제로 다시 재려면
#   `simplerag backend --reprobe`.
#------------------------------------------------------------------

import json
import os
import platform
import subprocess
import sys
import threading
import time

from .. import config

# Vulkan 판에 필요한 DLL. mtmd(멀티모달)는 쓰지 않아 동봉하지 않는다.
VULKAN_DLLS = ("ggml-base.dll", "ggml-cpu.dll", "ggml-vulkan.dll", "ggml.dll", "llama.dll")

_lock = threading.Lock()
_selected = None        # 프로세스당 한 번만 정한다(DLL 은 바꿀 수 없으므로)


#------------------------------------------------------------------
# 화면 표시용 이름
#
# -in: backend = "cpu" | "vulkan" | None
#
# -out: 사람이 읽을 이름
# -out: error = 없음
#------------------------------------------------------------------
def label(backend):
    return "iGPU(Vulkan)" if backend == "vulkan" else "CPU"


#------------------------------------------------------------------
# 리랭킹 사용 여부
#=> config.RERANK 가 1/0 이면 그대로 따르고, auto 면 iGPU 일 때만 켠다.
#   CPU 에서 리랭킹을 켜면 TTFT 3초 달성률이 74% → 53% 로 무너졌고,
#   iGPU 에서는 98~99% 를 지켰다(REPORT §30.2, §31.11).
#
# -in: backend = 선택된 생성 백엔드 "cpu" | "vulkan"
#
# -out: True = 리랭킹 켬
# -out: error = 없음
#------------------------------------------------------------------
def rerank_enabled(backend):
    mode = config.RERANK
    if mode in ("1", "on", "true", "yes"):
        return True
    if mode in ("0", "off", "false", "no"):
        return False
    return backend == "vulkan"


#------------------------------------------------------------------
# 설치된 llama-cpp-python 버전 (import 없이)
#=> llama_cpp 를 import 하면 그 순간 DLL 이 정해지므로 메타데이터로만 읽는다.
#
# -in: 없음
#
# -out: "0.3.35" 같은 문자열, 알 수 없으면 None (exe 에 메타데이터가 없을 때)
# -out: error = 없음
#------------------------------------------------------------------
def _installed_version():
    try:
        from importlib.metadata import version
        return version("llama_cpp_python")
    except Exception:
        return None


#------------------------------------------------------------------
# Vulkan 판 DLL 의 버전 표식 읽기
#
# -in: lib_dir = Vulkan 런타임 폴더
#
# -out: VERSION.txt 내용(예: "0.3.35"), 없으면 None
# -out: error = 없음
#------------------------------------------------------------------
def _read_version(lib_dir):
    try:
        with open(os.path.join(lib_dir, "VERSION.txt"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


#------------------------------------------------------------------
# Vulkan 판을 쓸 수 있는 상태인가 (측정 전 사전 점검)
#=> 자식 프로세스를 띄우기 전에 파일만으로 걸러낼 수 있는 경우를 먼저 거른다.
#    1) 런타임 폴더와 DLL 5개가 있는가
#    2) DLL 버전이 파이썬 바인딩과 같은가 — 다르면 구조체 배치가 달라
#       죽거나 조용히 오작동한다
#    3) 시스템에 vulkan-1.dll 이 있는가 — 없으면 적재 자체가 실패한다
#
# -in: lib_dir = Vulkan 런타임 폴더(None 이면 설정값)
#
# -out: (ok, why) = 쓸 수 있으면 (True, ""), 아니면 (False, 이유)
# -out: error = 없음
#------------------------------------------------------------------
def vulkan_available(lib_dir=None):
    lib_dir = lib_dir or config.VULKAN_LIB_DIR
    if not os.path.isdir(lib_dir):
        return False, "Vulkan 런타임 폴더 없음: {}".format(lib_dir)

    missing = [f for f in VULKAN_DLLS if not os.path.isfile(os.path.join(lib_dir, f))]
    if missing:
        return False, "Vulkan DLL 누락: {}".format(", ".join(missing))

    want, have = _read_version(lib_dir), _installed_version()
    if want and have and want != have:
        return False, "Vulkan DLL 버전({}) ≠ llama-cpp-python({})".format(want, have)

    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    if not os.path.isfile(os.path.join(sysroot, "System32", "vulkan-1.dll")):
        return False, "Vulkan 런타임(vulkan-1.dll) 없음 — 그래픽 드라이버가 없거나 오래됨"
    return True, ""


#------------------------------------------------------------------
# 측정 결과로 백엔드 고르기 (순수 함수 — 테스트 대상)
#=> iGPU 가 CPU 보다 min_speedup 배 이상 빠를 때만 iGPU.
#   iGPU 측정이 실패했으면 CPU, CPU 측정만 실패했으면 iGPU.
#
# -in: cpu         = CPU 측정 결과 {ok, prefill_tps, error}
# -in: gpu         = iGPU 측정 결과 {ok, prefill_tps, error}
# -in: min_speedup = 필요 배수(None 이면 config.GPU_MIN_SPEEDUP)
#
# -out: (backend, reason) = ("cpu"|"vulkan", 사람이 읽을 이유)
# -out: error = 없음
#------------------------------------------------------------------
def decide(cpu, gpu, min_speedup=None):
    min_speedup = config.GPU_MIN_SPEEDUP if min_speedup is None else min_speedup
    if not gpu or not gpu.get("ok"):
        return "cpu", "iGPU 사용 불가 — {}".format((gpu or {}).get("error", "측정 안 됨"))
    if not cpu or not cpu.get("ok"):
        return "vulkan", "CPU 측정 실패 — iGPU 사용 ({})".format((cpu or {}).get("error", ""))

    ratio = gpu["prefill_tps"] / max(cpu["prefill_tps"], 1e-6)
    if ratio >= min_speedup:
        return "vulkan", "iGPU prefill {:.0f} t/s = CPU {:.0f} t/s 의 {:.1f}배".format(
            gpu["prefill_tps"], cpu["prefill_tps"], ratio)
    return "cpu", "iGPU 가 충분히 빠르지 않음 ({:.0f} vs {:.0f} t/s, {:.1f}배 < {:.1f}배)".format(
        gpu["prefill_tps"], cpu["prefill_tps"], ratio, min_speedup)


#------------------------------------------------------------------
# 저장된 측정이 이 환경에 맞는지 가르는 키
#=> PC 이름·모델·바인딩 버전·Vulkan DLL(크기+수정시각)·판정 기준·스레드 수.
#   하나라도 바뀌면 옛 측정을 쓰지 않고 다시 잰다.
#   그래픽 드라이버 갱신은 키에 없다(조회에 WMI 가 필요해 느리다) —
#   그때는 `backend --reprobe`.
#
# -in: model_alias = 생성 모델 별칭(None 이면 기본)
#
# -out: dict
# -out: error = 없음
#------------------------------------------------------------------
def cache_key(model_alias=None):
    alias = model_alias or config.DEFAULT_GEN_MODEL
    try:
        st = os.stat(os.path.join(config.VULKAN_LIB_DIR, "ggml-vulkan.dll"))
        dll = "{}:{}".format(st.st_size, int(st.st_mtime))
    except OSError:
        dll = None
    return {"host": platform.node(),
            "model": config.GEN_MODELS.get(alias, alias),
            "llama_cpp_python": _installed_version(),
            "vulkan_dll": dll,
            "min_speedup": config.GPU_MIN_SPEEDUP,
            "threads": config.GEN_THREADS}


#------------------------------------------------------------------
# 저장된 측정 보기 (측정하지 않음)
#=> 생성 모델을 안 쓰는 명령(search 등)이 리랭킹 여부만 알고 싶을 때 쓴다.
#
# -in: model_alias = 생성 모델 별칭
#
# -out: 저장 내용 dict, 없거나 키가 안 맞으면 None
# -out: error = 없음 (파일 손상도 None)
#------------------------------------------------------------------
def peek(model_alias=None):
    try:
        with open(config.BACKEND_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if data.get("key") == cache_key(model_alias) else None


#------------------------------------------------------------------
# 측정 결과 저장
#=> 임시 파일에 쓰고 바꿔치기한다 — 쓰다 죽어도 반쪽 파일이 남지 않게.
#
# -in: data = 저장할 dict
#
# -out: 없음
# -out: error = 없음 (저장 실패는 무시 — 다음 실행 때 다시 잴 뿐이다)
#------------------------------------------------------------------
def _save(data):
    tmp = config.BACKEND_CACHE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, config.BACKEND_CACHE_PATH)
    except OSError:
        pass


#------------------------------------------------------------------
# 측정용 자식 프로세스 명령
#=> exe 면 자기 자신을, 소스 실행이면 python cli.py 를 숨은 명령
#   `_probe-backend` 로 띄운다.
#
# -in: kind       = "cpu" | "vulkan"
# -in: model_path = GGUF 경로
#
# -out: 명령 인자 리스트
# -out: error = 없음
#------------------------------------------------------------------
def _probe_cmd(kind, model_path):
    args = ["_probe-backend", kind, model_path, config.VULKAN_LIB_DIR]
    if config.FROZEN:
        return [sys.executable] + args
    cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli.py")
    return [sys.executable, cli] + args


#------------------------------------------------------------------
# 한 백엔드 측정 (자식 프로세스 실행)
#=> 자식의 표준출력 마지막 JSON 줄을 결과로 읽는다. 자식이 죽거나 시간을
#   넘기면 실패 결과를 만든다 — 본 프로세스는 절대 같이 죽지 않는다.
#
# -in: kind       = "cpu" | "vulkan"
# -in: model_path = GGUF 경로
# -in: timeout    = 초(None 이면 설정값)
#
# -out: {ok, prefill_tps, prefill_s, prompt_tokens, load_s, warm_s, wall_s} 또는
#       {ok: False, error}
# -out: error = 없음
#------------------------------------------------------------------
def run_probe(kind, model_path, timeout=None):
    timeout = timeout or config.BACKEND_PROBE_TIMEOUT
    t0 = time.perf_counter()
    try:
        r = subprocess.run(_probe_cmd(kind, model_path), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "시간 초과({}초)".format(timeout)}
    except OSError as e:
        return {"ok": False, "error": "측정 프로세스 실행 실패: {}".format(e)}

    out = None
    for line in reversed((r.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                out = json.loads(line)
                break
            except ValueError:
                continue
    if out is None:
        tail = [x for x in (r.stderr or "").strip().splitlines() if x.strip()][-1:]
        return {"ok": False, "error": "측정 프로세스 비정상 종료(exit={}) {}".format(
            r.returncode, tail[0][:160] if tail else "")}
    out["wall_s"] = round(time.perf_counter() - t0, 1)
    return out


#------------------------------------------------------------------
# 백엔드 선택 (핵심) — llama_cpp import 전에 한 번 부른다
#=> 프로세스당 한 번만 정하고 기억한다. 정한 결과를 환경변수로 반영한다.
#
# -in: model_alias = 생성 모델 별칭(None 이면 기본)
# -in: reprobe     = True 면 저장된 측정을 무시하고 다시 잰다
# -in: on_log      = 진행 안내를 받을 함수(첫 측정은 20~40초라 알려야 한다)
#
# -out: info = {backend, source, reason, cpu?, vulkan?}
#        source = cache(저장된 측정) | probe(방금 측정) | config(환경변수) |
#                 check(사전 점검 탈락) | loaded(이미 적재됨)
# -out: error = 없음 — 어떤 실패든 CPU 로 폴백하고 이유를 담는다
#------------------------------------------------------------------
def select(model_alias=None, reprobe=False, on_log=None):
    global _selected
    with _lock:
        if _selected is not None and not reprobe:
            return _selected
        info = _select(model_alias, reprobe, on_log or (lambda m: None))
        _apply(info)
        _selected = info
        return info


#------------------------------------------------------------------
# 백엔드 선택 본체
#    1) 이미 llama_cpp 가 올라와 있으면 그 DLL 을 따른다(바꿀 수 없다)
#    2) 환경변수 cpu 면 끝
#    3) 사전 점검(파일·버전·vulkan-1.dll) 탈락이면 CPU
#    4) 저장된 측정이 맞으면 그대로
#    5) 아니면 자식 프로세스로 재고 decide() → 저장
#
# -in: model_alias, reprobe, log = select() 와 같음
#
# -out: info dict
# -out: error = 없음
#------------------------------------------------------------------
def _select(model_alias, reprobe, log):
    if "llama_cpp" in sys.modules:
        import llama_cpp
        gpu = bool(llama_cpp.llama_supports_gpu_offload())
        return {"backend": "vulkan" if gpu else "cpu", "source": "loaded",
                "reason": "llama_cpp 가 이미 적재됨 — 적재된 DLL 을 그대로 쓴다"}

    mode = config.GEN_BACKEND
    if mode == "cpu":
        return {"backend": "cpu", "source": "config", "reason": "SIMPLERAG_GEN_BACKEND=cpu"}
    if mode not in ("auto", "vulkan"):
        log("[backend] 알 수 없는 SIMPLERAG_GEN_BACKEND={} — auto 로 처리".format(mode))
        mode = "auto"

    ok, why = vulkan_available()
    if not ok:
        return {"backend": "cpu", "source": "check", "reason": why}

    cached = None if reprobe else peek(model_alias)
    if cached:
        if mode == "auto":
            return {"backend": cached["backend"], "source": "cache",
                    "reason": cached.get("reason", ""),
                    "cpu": cached.get("cpu"), "vulkan": cached.get("vulkan")}
        if (cached.get("vulkan") or {}).get("ok"):
            return {"backend": "vulkan", "source": "cache",
                    "reason": "SIMPLERAG_GEN_BACKEND=vulkan", "vulkan": cached.get("vulkan")}

    model_path = config.gen_model_path(model_alias or config.DEFAULT_GEN_MODEL)
    if not os.path.isfile(model_path):
        return {"backend": "cpu", "source": "check", "reason": "생성 모델 없음 — 측정 생략"}

    log("[backend] 이 PC 의 CPU/iGPU 생성 속도를 잽니다 (처음 한 번, 약 15~40초)")
    gpu = run_probe("vulkan", model_path)

    if mode == "vulkan":
        if gpu.get("ok"):
            return {"backend": "vulkan", "source": "probe",
                    "reason": "SIMPLERAG_GEN_BACKEND=vulkan", "vulkan": gpu}
        return {"backend": "cpu", "source": "probe",
                "reason": "iGPU 지정됐지만 사용 불가 — {}".format(gpu.get("error", "")),
                "vulkan": gpu}

    cpu = run_probe("cpu", model_path)
    backend, reason = decide(cpu, gpu)
    _save({"key": cache_key(model_alias), "backend": backend, "reason": reason,
           "cpu": cpu, "vulkan": gpu, "probed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    log("[backend] {} 선택 — {}".format(label(backend), reason))
    return {"backend": backend, "source": "probe", "reason": reason, "cpu": cpu, "vulkan": gpu}


#------------------------------------------------------------------
# 선택 결과를 환경변수로 반영
#=> iGPU 면 LLAMA_CPP_LIB_PATH 를 Vulkan 폴더로, CPU 면 그 변수가 우리 Vulkan
#   폴더를 가리킬 때만 지운다(사용자가 다른 목적으로 지정한 값은 건드리지 않는다).
#
# -in: info = select 결과
#
# -out: 없음
# -out: error = 없음
#------------------------------------------------------------------
def _apply(info):
    if info.get("source") == "loaded":
        return
    if info["backend"] == "vulkan":
        os.environ["LLAMA_CPP_LIB_PATH"] = config.VULKAN_LIB_DIR
        return
    cur = os.environ.get("LLAMA_CPP_LIB_PATH")
    if cur and os.path.normcase(os.path.abspath(cur)) == os.path.normcase(
            os.path.abspath(config.VULKAN_LIB_DIR)):
        os.environ.pop("LLAMA_CPP_LIB_PATH", None)


#------------------------------------------------------------------
# 자식 프로세스 본체 — `_probe-backend <kind> <model> <lib_dir>`
#=> 한 백엔드로 모델을 올리고 운영과 같은 모양의 프롬프트로 prefill 속도를 잰다.
#    1) DLL 지정(Vulkan 이면 환경변수, CPU 면 제거) 후 llama_cpp import
#    2) Vulkan 이면 버전 일치 + GPU 오프로드 지원 확인(엉뚱한 DLL 방지)
#    3) 운영과 같은 인자로 적재 — n_gpu_layers / flash_attn 은 백엔드별 설정값
#    4) 1회 버림(셰이더 컴파일 흡수) + 2회 측정, 매번 reset 해 KV 재사용 없이
#    5) 결과 JSON 한 줄을 표준출력으로
#
# -in: argv = [kind, model_path, lib_dir]
#
# -out: 종료코드 0(성공) / 3(실패) — 결과는 표준출력 JSON
# -out: error = 없음 (예외는 JSON 의 error 로 담는다. DLL 적재 중 네이티브 크래시는
#               프로세스가 죽고, 부모가 비정상 종료로 처리한다)
#------------------------------------------------------------------
def probe_main(argv):
    kind, model_path, lib_dir = (list(argv) + [None, None, None])[:3]
    out = {"kind": kind, "ok": False}
    try:
        vk = kind == "vulkan"
        if vk:
            os.environ["LLAMA_CPP_LIB_PATH"] = lib_dir
        else:
            os.environ.pop("LLAMA_CPP_LIB_PATH", None)

        import llama_cpp
        from .prompts import SYSTEM_PROMPT, build_prompt, build_user_message

        out["version"] = getattr(llama_cpp, "__version__", None)
        gpu = bool(llama_cpp.llama_supports_gpu_offload())
        if vk:
            want = _read_version(lib_dir)
            if want and out["version"] and want != out["version"]:
                raise RuntimeError("DLL 버전 {} ≠ 바인딩 {}".format(want, out["version"]))
            if not gpu:
                raise RuntimeError("GPU 오프로드를 지원하지 않는 DLL 이 적재됨")

        t0 = time.perf_counter()
        llm = llama_cpp.Llama(
            model_path=model_path, n_ctx=config.GEN_N_CTX,
            n_threads=config.GEN_THREADS, n_threads_batch=config.GEN_THREADS,
            n_batch=config.GEN_N_BATCH,
            flash_attn=config.GEN_FLASH_ATTN_GPU if vk else config.GEN_FLASH_ATTN,
            n_gpu_layers=config.GEN_GPU_LAYERS if vk else 0,
            verbose=False)
        out["load_s"] = round(time.perf_counter() - t0, 2)

        # 운영 프롬프트와 같은 모양(시스템 지시문 + 근거 3건), 비슷한 길이로 잰다
        body = ("출장여비 규정에 따라 국내출장 숙박비 상한액은 서울특별시 70,000원, "
                "광역시 60,000원, 그 밖의 지역 50,000원으로 하고 일비는 1일 20,000원을 지급한다. ")

        # 측정 회차마다 질문 번호만 바꾼 프롬프트 — 같은 길이, 다른 내용
        def prompt(i):
            return build_prompt(SYSTEM_PROMPT, build_user_message(
                "측정 질문 {}번 — 서울특별시 숙박비 상한액은 얼마인가요?".format(i), [body * 2] * 3))

        n_tok = len(llm.tokenize(prompt(0).encode("utf-8"), special=True))
        times = []
        for i in range(3):
            llm.reset()       # 앞 측정의 KV 를 재사용하지 않게 — 매번 전체 prefill
            t = time.perf_counter()
            llm.create_completion(prompt(i), max_tokens=1, temperature=0.0)
            times.append(time.perf_counter() - t)

        sec = sum(times[1:]) / len(times[1:])   # 첫 회는 셰이더 컴파일 포함일 수 있어 뺀다
        out.update({"ok": True, "gpu_offload": gpu, "prompt_tokens": n_tok,
                    "warm_s": round(times[0], 2), "prefill_s": round(sec, 3),
                    "prefill_tps": round(n_tok / sec, 1)})
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, str(e)[:200])

    sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0 if out["ok"] else 3
