#------------------------------------------------------------------
# 실측 환경(테스트 PC 사양) 수집
#=> 벤치 결과는 하드웨어를 같이 적어두지 않으면 재현도, 해석도 불가능하다.
#   CPU/메모리/디스크/OS 와 함께 llama.cpp 가 실제로 감지한 SIMD 명령어셋까지
#   기록한다. AVX2/AVX512 유무는 CPU 추론 속도를 좌우하는 핵심 변수다.
#
#   메모리 대역폭도 함께 적는다 — decode 속도는 연산이 아니라
#   '가중치를 메모리에서 읽어오는 속도'에 묶여 있기 때문이다.
#
#   사용:  python bench/sysinfo.py
#------------------------------------------------------------------

import json
import os
import platform
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.abspath(os.path.join(_HERE, "..", "results"))


#------------------------------------------------------------------
# PowerShell 한 줄 실행 후 표준출력 반환
#=> WMI/CIM 조회는 파이썬 표준 라이브러리로 안 되므로 PowerShell 을 빌려 쓴다.
#
# -in: script = 실행할 PowerShell 구문
#
# -out: text = 표준출력(실패 시 빈 문자열)
#------------------------------------------------------------------
def ps(script):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout.strip()
    except Exception:
        return ""


#------------------------------------------------------------------
# CPU 정보 수집
#=> 이름/물리코어/논리코어/최대클럭. Intel 하이브리드(P/E) 는 WMI 로 P·E 구분이
#   안 나오므로, 논리-물리 차이로 P코어 수를 역산해 추정한다.
#     P코어는 하이퍼스레딩(2스레드), E코어는 1스레드 이므로
#     P = (논리 - 물리),  E = 물리 - P
#
# -out: dict = cpu 정보
#------------------------------------------------------------------
def cpu_info():
    raw = ps("$c=Get-CimInstance Win32_Processor|Select-Object -First 1;"
             "'{0}|{1}|{2}|{3}' -f $c.Name.Trim(),$c.NumberOfCores,"
             "$c.NumberOfLogicalProcessors,$c.MaxClockSpeed")
    name, cores, logical, mhz = (raw.split("|") + ["", "", "", ""])[:4]
    cores = int(cores) if cores.isdigit() else 0
    logical = int(logical) if logical.isdigit() else 0

    p_cores = logical - cores if logical > cores else 0   # HT 있는 코어 수 = P코어
    e_cores = cores - p_cores if p_cores else 0

    return {
        "name": name,
        "physical_cores": cores,
        "logical_cores": logical,
        "p_cores_est": p_cores,
        "e_cores_est": e_cores,
        "max_clock_mhz": int(mhz) if mhz.isdigit() else None,
        "arch": platform.machine(),
    }


# SMBIOSMemoryType 코드 -> 사람이 읽는 이름 (필요한 것만).
_MEM_TYPE = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4",
             34: "DDR5", 35: "LPDDR5", 30: "LPDDR3", 31: "LPDDR4"}


#------------------------------------------------------------------
# 메모리 정보 수집 (핵심 — decode 속도의 상한을 결정)
#=> LLM decode 는 매 토큰마다 가중치 전체를 메모리에서 읽으므로, 속도 상한이
#   '메모리 대역폭 / 모델 크기' 로 정해진다. 따라서 대역폭을 정확히 잡아야 한다.
#
#   주의: LPDDR5 는 온패키지 다이가 각각 별도 모듈로 보고된다(여기선 16bit x 8개).
#   이를 '채널 8개'로 세면 대역폭을 4배 부풀리게 되므로, 반드시 DataWidth 를
#   모두 더해 '총 버스 폭' 을 구한 뒤 계산한다.
#     대역폭(GB/s) = 전송률(MT/s) x 총버스폭(bit) / 8 / 1000
#
# -out: dict = 메모리 정보 + 대역폭 추정치
#------------------------------------------------------------------
def mem_info():
    total = ps("[math]::Round((Get-CimInstance Win32_ComputerSystem)"
               ".TotalPhysicalMemory/1GB,1)")
    raw = ps("Get-CimInstance Win32_PhysicalMemory|ForEach-Object{"
             "'{0}|{1}|{2}|{3}' -f [math]::Round($_.Capacity/1GB,0),"
             "$_.ConfiguredClockSpeed,$_.DataWidth,$_.SMBIOSMemoryType}")

    mods, total_bits, mts, mtype = [], 0, 0, None
    for line in raw.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        gb, speed, width, tcode = parts
        total_bits += int(width) if width.isdigit() else 0
        mts = max(mts, int(speed) if speed.isdigit() else 0)
        if tcode.isdigit():
            mtype = _MEM_TYPE.get(int(tcode), "type" + tcode)
        mods.append("{}GB@{}MT/s({}bit)".format(gb, speed, width))

    bw = round(mts * total_bits / 8 / 1000, 1) if (mts and total_bits) else None

    return {
        "total_gb": float(total) if total else None,
        "type": mtype,
        "transfer_mts": mts or None,
        "bus_width_bits": total_bits or None,
        "module_count": len(mods),
        "modules": mods,
        "est_bandwidth_gbs": bw,
    }


#------------------------------------------------------------------
# 저장장치 정보 수집
#=> 모델 로딩(mmap) 속도에 영향. SSD/NVMe 여부만 확인하면 충분하다.
#------------------------------------------------------------------
def disk_info():
    raw = ps("Get-PhysicalDisk|ForEach-Object{'{0}|{1}|{2}GB' -f "
             "$_.FriendlyName,$_.MediaType,[math]::Round($_.Size/1GB,0)}")
    return [d for d in raw.splitlines() if d.strip()]


#------------------------------------------------------------------
# llama.cpp 가 감지한 SIMD 명령어셋 (핵심)
#=> llama_print_system_info() 가 AVX2/AVX512/FMA/F16C 지원 여부를 뱉는다.
#   같은 코어 수라도 AVX512 유무로 prefill 속도가 크게 갈리므로 반드시 기록한다.
#   (Alder Lake/Raptor Lake 소비자용 CPU 는 AVX512 가 비활성화되어 있다)
#
# -out: dict = backend 문자열과 파싱된 플래그
#------------------------------------------------------------------
def llama_simd():
    try:
        import ctypes
        import llama_cpp

        fn = llama_cpp.llama_print_system_info
        fn.restype = ctypes.c_char_p
        raw = fn()
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception as e:
        return {"error": "{}: {}".format(type(e).__name__, e)}

    flags = {}
    for token in text.split("|"):
        token = token.strip()
        if "=" in token:
            k, v = token.split("=", 1)
            flags[k.strip()] = v.strip()

    enabled = sorted(k for k, v in flags.items() if v == "1")
    return {"raw": text.strip(), "enabled": enabled}


#------------------------------------------------------------------
# 사양 전체 수집 + 사람이 읽는 표로 출력
#------------------------------------------------------------------
def collect():
    try:
        import llama_cpp
        llama_ver = llama_cpp.__version__
    except Exception:
        llama_ver = "미설치"

    return {
        "os": "{} {} (build {})".format(platform.system(), platform.release(),
                                        platform.version()),
        "python": sys.version.split()[0],
        "llama_cpp_python": llama_ver,
        "cpu": cpu_info(),
        "memory": mem_info(),
        "disks": disk_info(),
        "gpu": ps("(Get-CimInstance Win32_VideoController|"
                  "Select-Object -ExpandProperty Name) -join ', '"),
        "simd": llama_simd(),
    }


def render(info):
    c, m = info["cpu"], info["memory"]
    lines = []
    add = lines.append

    add("=" * 66)
    add(" 실측 환경 (테스트 PC 사양)")
    add("=" * 66)
    add("  OS            : {}".format(info["os"]))
    add("  CPU           : {}".format(c["name"]))
    add("  코어          : {}C / {}T  (P-core {} + E-core {} 추정)".format(
        c["physical_cores"], c["logical_cores"], c["p_cores_est"], c["e_cores_est"]))
    add("  기본 클럭     : {} MHz  (WMI 보고값 — 터보 최대치는 별도)".format(
        c["max_clock_mhz"]))
    add("  RAM           : {} GB  {} {}MT/s, {}bit 버스 ({}개 모듈)".format(
        m["total_gb"], m["type"], m["transfer_mts"],
        m["bus_width_bits"], m["module_count"]))
    add("  메모리 대역폭 : 약 {} GB/s (이론치)".format(m["est_bandwidth_gbs"]))
    add("  GPU           : {}".format(info["gpu"] or "없음"))
    for d in info["disks"]:
        add("  디스크        : {}".format(d))
    add("-" * 66)
    # decode 속도 상한 — 매 토큰마다 가중치 전체를 읽으므로 대역폭/모델크기 가 천장이다.
    if m["est_bandwidth_gbs"]:
        add("  [참고] 메모리 대역폭이 정하는 decode 속도 이론상한")
        for label, gb in (("Qwen3-0.6B Q4_K_M", 0.37), ("Qwen3-1.7B Q4_K_M", 1.03)):
            add("         {:<20} {:>6.0f} tok/s (실측은 보통 이 값의 40~60%)".format(
                label, m["est_bandwidth_gbs"] / gb))
        add("-" * 66)
    add("  Python        : {}".format(info["python"]))
    add("  llama-cpp-py  : {}".format(info["llama_cpp_python"]))
    simd = info["simd"]
    if "enabled" in simd:
        add("  SIMD 감지     : {}".format(", ".join(simd["enabled"]) or "없음"))
    else:
        add("  SIMD 감지     : {}".format(simd.get("error")))
    add("=" * 66)
    return "\n".join(lines)


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    info = collect()
    print(render(info))

    out = os.path.join(RESULTS_DIR, "sysinfo.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print("\n[sysinfo] 저장: " + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
