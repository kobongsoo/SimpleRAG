//------------------------------------------------------------------
// 이 PC 사양 읽기 — 설정 > 모델의 "받기 전 추천"에 쓴다 (모델 받기 설계서 §4-1)
//=> 후보 모델을 받지 않고 판단하려고 Windows 기본 기능만 쓴다(1초 안에 끝난다).
//    · 메모리: GlobalMemoryStatusEx — iGPU 는 메모리를 나눠 쓰므로 전체 메모리가 곧 그래픽 메모리 한도
//    · 그래픽: DXGI 장치 목록 — 이름 · 전용 VRAM (소프트웨어 장치는 뺀다)
//    · CPU: 레지스트리 ProcessorNameString · 논리 스레드 수
//    · 디스크 여유: GetDiskFreeSpaceExW
//    · 이 PC 의 실제 처리 속도: 워커가 처음 실행 때 내장 1.7B 로 잰 gen_backend.json
//------------------------------------------------------------------

use serde_json::{json, Value};
use windows::core::HSTRING;

// 그래픽 장치 하나
#[derive(Debug, Clone)]
pub struct Gpu {
    pub name: String,
    pub vram_mb: u64,
}

// 사양 묶음
#[derive(Debug, Clone, Default)]
pub struct Hw {
    pub ram_total_mb: u64,
    pub ram_free_mb: u64,
    pub gpus: Vec<Gpu>,
    pub cpu: String,
    pub threads: usize,
    // 설치 폴더 드라이브 이름(예: "C:") · 여유
    pub drive: String,
    pub disk_free_mb: u64,
    // gen_backend.json 에서 — 고른 백엔드(vulkan | cpu) · 그 백엔드의 내장 1.7B prefill 속도(토큰/초)
    pub backend: String,
    pub prefill_tps: f64,
    // 이 PC 에서 잰 다른 모델들 — (GGUF 파일, 고른 백엔드, prefill 토큰/초). 1.7B 값이 없을 때 환산에 쓴다
    pub probes: Vec<(String, String, f64)>,
}

// 외장 GPU 로 칠 전용 VRAM 하한 — 인텔·AMD 내장 그래픽은 128~512MB 로 보고한다
const DGPU_MIN_MB: u64 = 2048;

//------------------------------------------------------------------
// 사양 읽기 — 못 읽은 칸은 0 · 빈 글로 남긴다(예외 없음)
//   install_dir = 모델이 들어갈 폴더(디스크 여유를 잴 드라이브) · backend_json = gen_backend.json 경로
//------------------------------------------------------------------
pub fn read(install_dir: &str, backend_json: Option<&str>) -> Hw {
    let mut hw = Hw::default();
    let (t, f) = memory_mb();
    hw.ram_total_mb = t;
    hw.ram_free_mb = f;
    hw.gpus = gpus();
    hw.cpu = cpu_name();
    hw.threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1);
    hw.drive = install_dir.chars().take(2).collect();
    hw.disk_free_mb = disk_free_mb(install_dir);
    if let Some(p) = backend_json {
        hw.probes = probes(p);
        if let Some((_, b, tps)) = hw.probes.iter().find(|x| x.0 == crate::models::BASE_FILE) {
            hw.backend = b.clone();
            hw.prefill_tps = *tps;
        } else if let Some((_, b, _)) = hw.probes.first() {
            // 1.7B 를 재지 않았어도 iGPU 가속을 쓰는지는 다른 모델 측정으로 안다
            hw.backend = b.clone();
        }
    }
    hw
}

//------------------------------------------------------------------
// gen_backend.json 의 이 PC 측정 모두 — 모델 파일마다 가장 최근 것 하나. 못 읽으면 빈 목록
//------------------------------------------------------------------
pub fn probes(path: &str) -> Vec<(String, String, f64)> {
    let Ok(raw) = std::fs::read_to_string(path) else { return vec![] };
    let Ok(v) = serde_json::from_str::<Value>(&raw) else { return vec![] };
    let mut files: Vec<String> = v["entries"].as_array().map(|a| a.iter().filter_map(|e| e["key"]["model"].as_str().map(String::from)).collect())
        .unwrap_or_default();
    files.sort();
    files.dedup();
    files.into_iter().filter_map(|f| {
        let (b, tps) = probe_speed(path, &f);
        (tps > 0.0).then_some((f, b, tps))
    }).collect()
}

impl Hw {
    // 외장 GPU 가 있나 (전용 VRAM 2GB 이상)
    pub fn dgpu_mb(&self) -> u64 {
        self.gpus.iter().map(|g| g.vram_mb).filter(|m| *m >= DGPU_MIN_MB).max().unwrap_or(0)
    }

    //------------------------------------------------------------------
    // 설정 창에 보일 값 — 사람이 읽는 글로
    //------------------------------------------------------------------
    pub fn to_json(&self) -> Value {
        let gpu = match self.gpus.first() {
            None => "없음".to_string(),
            Some(g) => {
                let accel = match self.backend.as_str() {
                    "vulkan" => " · 가속 씀(Vulkan)",
                    "cpu" => " · 가속 안 씀(CPU 가 더 빠름)",
                    _ => "",
                };
                let kind = if g.vram_mb >= DGPU_MIN_MB { format!("외장 {}GB", gb1(g.vram_mb)) } else { "내장".into() };
                format!("{} ({}{})", g.name, kind, accel)
            }
        };
        json!({
            "ram": format!("{}GB (지금 여유 {}GB)", gb0(self.ram_total_mb), gb1(self.ram_free_mb)),
            "ram_total_mb": self.ram_total_mb,
            "gpu": gpu,
            "cpu": if self.cpu.is_empty() { format!("{}스레드", self.threads) } else { format!("{} · {}스레드", self.cpu, self.threads) },
            "disk": format!("{} {}GB", self.drive, gb0(self.disk_free_mb)),
            "disk_free_mb": self.disk_free_mb,
        })
    }
}

// MB → GB 글(반올림 정수 · 소수 한 자리)
fn gb0(mb: u64) -> String {
    format!("{}", (mb as f64 / 1024.0).round() as u64)
}
fn gb1(mb: u64) -> String {
    format!("{:.1}", mb as f64 / 1024.0)
}

//------------------------------------------------------------------
// 전체 · 여유 메모리(MB) — 못 읽으면 (0, 0)
//------------------------------------------------------------------
fn memory_mb() -> (u64, u64) {
    use windows::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
    let mut m = MEMORYSTATUSEX { dwLength: std::mem::size_of::<MEMORYSTATUSEX>() as u32, ..Default::default() };
    if unsafe { GlobalMemoryStatusEx(&mut m) }.is_ok() {
        (m.ullTotalPhys / 1_048_576, m.ullAvailPhys / 1_048_576)
    } else {
        (0, 0)
    }
}

//------------------------------------------------------------------
// 그래픽 장치 목록(DXGI) — 소프트웨어 장치(Microsoft Basic Render)는 뺀다. 전용 VRAM 큰 순
//------------------------------------------------------------------
fn gpus() -> Vec<Gpu> {
    use windows::Win32::Graphics::Dxgi::{CreateDXGIFactory1, IDXGIFactory1, DXGI_ADAPTER_FLAG_SOFTWARE};
    let mut out = Vec::new();
    let Ok(f) = (unsafe { CreateDXGIFactory1::<IDXGIFactory1>() }) else { return out };
    for i in 0..8u32 {
        let Ok(a) = (unsafe { f.EnumAdapters1(i) }) else { break };
        let Ok(d) = (unsafe { a.GetDesc1() }) else { continue };
        if d.Flags & (DXGI_ADAPTER_FLAG_SOFTWARE.0 as u32) != 0 {
            continue;
        }
        let n = d.Description.iter().position(|c| *c == 0).unwrap_or(d.Description.len());
        let name = String::from_utf16_lossy(&d.Description[..n]).trim().to_string();
        // 같은 장치가 두 번 나오는 PC 가 있다(하이브리드 그래픽)
        if out.iter().any(|g: &Gpu| g.name == name) {
            continue;
        }
        out.push(Gpu { name, vram_mb: d.DedicatedVideoMemory as u64 / 1_048_576 });
    }
    out.sort_by(|a, b| b.vram_mb.cmp(&a.vram_mb));
    out
}

//------------------------------------------------------------------
// CPU 이름 — 레지스트리(HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0). 못 읽으면 빈 글
//------------------------------------------------------------------
fn cpu_name() -> String {
    use windows::Win32::System::Registry::{RegGetValueW, HKEY_LOCAL_MACHINE, RRF_RT_REG_SZ};
    let mut buf = [0u16; 256];
    let mut size = (buf.len() * 2) as u32;
    let r = unsafe {
        RegGetValueW(HKEY_LOCAL_MACHINE, &HSTRING::from(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"),
                     &HSTRING::from("ProcessorNameString"), RRF_RT_REG_SZ, None, Some(buf.as_mut_ptr() as *mut _), Some(&mut size))
    };
    if r.is_err() {
        return String::new();
    }
    let n = buf.iter().position(|c| *c == 0).unwrap_or(buf.len());
    // "13th Gen Intel(R) Core(TM) i5-1340P" → (R)(TM) 떼고 공백 하나로
    String::from_utf16_lossy(&buf[..n]).replace("(R)", "").replace("(TM)", "").split_whitespace().collect::<Vec<_>>().join(" ")
}

//------------------------------------------------------------------
// 그 경로가 있는 드라이브의 여유 공간(MB) — 못 읽으면 0
//------------------------------------------------------------------
pub fn disk_free_mb(path: &str) -> u64 {
    use windows::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
    // 아직 없는 폴더면 있는 위쪽 폴더로
    let mut p = std::path::PathBuf::from(path);
    while !p.exists() {
        if !p.pop() {
            return 0;
        }
    }
    let mut free = 0u64;
    let ok = unsafe { GetDiskFreeSpaceExW(&HSTRING::from(p.as_os_str()), Some(&mut free), None, None) };
    if ok.is_ok() { free / 1_048_576 } else { 0 }
}

//------------------------------------------------------------------
// gen_backend.json 에서 이 PC · 그 모델 파일로 잰 (고른 백엔드, 그 백엔드 prefill 토큰/초)
//=> 워커(simplerag generate::backend)가 처음 올릴 때 CPU · iGPU 를 재서 남긴다. 호스트 이름이 같은 항목만 쓴다.
//   없으면 ("", 0.0) — 그때는 사양 등급으로 어림한다
//------------------------------------------------------------------
pub fn probe_speed(path: &str, model_file: &str) -> (String, f64) {
    let Ok(raw) = std::fs::read_to_string(path) else { return (String::new(), 0.0) };
    let Ok(v) = serde_json::from_str::<Value>(&raw) else { return (String::new(), 0.0) };
    let host = std::env::var("COMPUTERNAME").unwrap_or_default();
    let entries = v["entries"].as_array().cloned().unwrap_or_default();
    // 같은 모델 항목이 여럿이면(라이브러리 판이 바뀌는 등) 가장 최근 것
    let mut best: Option<(&str, &Value)> = None;
    for e in &entries {
        if e["key"]["model"].as_str() != Some(model_file) {
            continue;
        }
        if !host.is_empty() && e["key"]["host"].as_str().map(|h| !h.eq_ignore_ascii_case(&host)).unwrap_or(false) {
            continue;
        }
        let at = e["probed_at"].as_str().unwrap_or("");
        if best.map(|(b, _)| at >= b).unwrap_or(true) {
            best = Some((at, e));
        }
    }
    let Some((_, e)) = best else { return (String::new(), 0.0) };
    let backend = e["backend"].as_str().unwrap_or("").to_string();
    let tps = e[backend.as_str()]["prefill_tps"].as_f64().unwrap_or(0.0);
    (backend, tps)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn probe_picks_this_host_and_model() {
        let p = std::env::temp_dir().join(format!("rsb_gen_backend_{}.json", std::process::id()));
        let host = std::env::var("COMPUTERNAME").unwrap_or_default();
        let j = json!({"version": 2, "entries": [
            {"key": {"host": "OTHER-PC", "model": "Qwen3-1.7B-Q4_K_M.gguf"}, "backend": "cpu", "cpu": {"prefill_tps": 10.0}, "probed_at": "2026-11-01 00:00:00"},
            {"key": {"host": host, "model": "Qwen3-1.7B-Q4_K_M.gguf"}, "backend": "vulkan", "vulkan": {"prefill_tps": 259.7}, "cpu": {"prefill_tps": 80.3}, "probed_at": "2026-10-22 14:33:56"},
            {"key": {"host": host, "model": "Qwen3-4B-Q4_K_M.gguf"}, "backend": "vulkan", "vulkan": {"prefill_tps": 99.0}, "probed_at": "2026-10-28 00:00:00"}
        ]});
        std::fs::write(&p, j.to_string()).unwrap();
        let (b, tps) = probe_speed(p.to_str().unwrap(), "Qwen3-1.7B-Q4_K_M.gguf");
        assert_eq!(b, "vulkan");
        assert!((tps - 259.7).abs() < 1e-6);
        assert_eq!(probe_speed(p.to_str().unwrap(), "None.gguf").1, 0.0);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn reads_this_pc() {
        let hw = read("C:\\", None);
        assert!(hw.ram_total_mb > 1000);
        assert!(hw.threads >= 1);
        assert!(hw.disk_free_mb > 0);
    }
}
