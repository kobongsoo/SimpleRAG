# ------------------------------------------------------------------
# 배포용 라이선스 문서 만들기
# => 배포본에 넣는 라이선스 txt 3개를 지금 쓰는 구성으로 새로 만든다.
#    1) LICENSES_MODELS.txt      = 임베딩 · 리랭커 · 생성(sLLM) 모델
#    2) LICENSES_THIRD_PARTY.txt = 문서 파서 · 런타임(DLL) · Rust 라이브러리
#    3) LICENSES_PYTHON.txt      = Python 판 라이브러리 — Rust 판에는 없어 dist\simplerag 에만 만든다
#    모델 표는 사람이 확인한 값이라 아래 MODELS 에 적어 두고, 라이브러리는 cargo metadata · .venv 에서 읽는다.
#    모델 · 런타임 원문은 저장소 licenses\ 에 보관한다(새 모델을 넣으면 MODELS 와 원문을 함께 고친다).
#
# 사용: .venv\Scripts\python.exe rust\gen_licenses.py [출력 폴더, 기본 dist-rust]
# ------------------------------------------------------------------
import hashlib
import importlib.metadata as metadata
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIC = ROOT / "licenses"
VENV_SITE = ROOT / ".venv" / "Lib" / "site-packages"
LINE = "=" * 78
THIN = "-" * 78

# 모델 표 — 2026-11-02 Hugging Face 모델 카드 · LICENSE 로 확인한 값
MODELS = [
    {"path": "e5-small-ko", "role": "임베딩(문서 · 질의 벡터)", "name": "dragonkue/multilingual-e5-small-ko (ONNX int8 변환)",
     "dev": "dragonkue", "license": "Apache-2.0", "url": "https://huggingface.co/dragonkue/multilingual-e5-small-ko",
     "base": "intfloat/multilingual-e5-small (MIT)", "text": "Qwen3-Apache-2.0.txt", "strip_copyright": True},
    {"path": "bge-reranker-base-int8", "role": "리랭커(검색 결과 재정렬)", "name": "BAAI/bge-reranker-base (ONNX int8 변환)",
     "dev": "Beijing Academy of Artificial Intelligence (BAAI)", "license": "MIT", "url": "https://huggingface.co/BAAI/bge-reranker-base",
     "text": "FlagEmbedding-MIT.txt"},
    {"path": "Qwen3-0.6B-Q4_K_M.gguf", "role": "생성(sLLM)", "name": "Qwen/Qwen3-0.6B (unsloth GGUF Q4_K_M)",
     "dev": "Alibaba Cloud (Qwen)", "license": "Apache-2.0", "url": "https://huggingface.co/unsloth/Qwen3-0.6B-GGUF", "text": "Qwen3-Apache-2.0.txt"},
    {"path": "Qwen3-1.7B-Q4_K_M.gguf", "role": "생성(sLLM) — 기본", "name": "Qwen/Qwen3-1.7B (unsloth GGUF Q4_K_M)",
     "dev": "Alibaba Cloud (Qwen)", "license": "Apache-2.0", "url": "https://huggingface.co/unsloth/Qwen3-1.7B-GGUF", "text": "Qwen3-Apache-2.0.txt"},
    {"path": "Qwen3-4B-Q4_K_M.gguf", "role": "생성(sLLM) — 정밀 모드(별도 설치)", "name": "Qwen/Qwen3-4B (unsloth GGUF Q4_K_M)",
     "dev": "Alibaba Cloud (Qwen)", "license": "Apache-2.0", "url": "https://huggingface.co/unsloth/Qwen3-4B-GGUF", "text": "Qwen3-Apache-2.0.txt"},
    {"path": "Qwen3.5-2B-Q4_K_M.gguf", "role": "생성(sLLM)", "name": "Qwen/Qwen3.5-2B (unsloth GGUF Q4_K_M)",
     "dev": "Alibaba Cloud (Qwen)", "license": "Apache-2.0", "url": "https://huggingface.co/unsloth/Qwen3.5-2B-GGUF", "text": "Qwen3.5-Apache-2.0.txt"},
    {"path": "Qwen3.5-4B-Q4_K_M.gguf", "role": "생성(sLLM)", "name": "Qwen/Qwen3.5-4B (unsloth GGUF Q4_K_M)",
     "dev": "Alibaba Cloud (Qwen)", "license": "Apache-2.0", "url": "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF", "text": "Qwen3.5-Apache-2.0.txt"},
    {"path": "Midm-2.0-Mini-Instruct-Q4_K_M.gguf", "role": "생성(sLLM) — 한국어", "name": "K-intelligence/Midm-2.0-Mini-Instruct (GGUF Q4_K_M 변환)",
     "dev": "KT Corporation", "license": "MIT", "url": "https://huggingface.co/K-intelligence/Midm-2.0-Mini-Instruct", "text": "Midm-2.0-MIT.txt"},
    {"path": "kanana-1.5-2.1b-instruct-Q4_K_M.gguf", "role": "생성(sLLM) — 측정용", "name": "kakaocorp/kanana-1.5-2.1b-instruct-2505 (GGUF Q4_K_M 변환)",
     "dev": "Kakao Corp.", "license": "Apache-2.0", "url": "https://huggingface.co/kakaocorp/kanana-1.5-2.1b-instruct-2505", "text": "Kanana-1.5-Apache-2.0.txt"},
    {"path": "EXAONE-4.0-1.2B-Q4_K_M.gguf", "role": "생성(sLLM) — 측정용", "name": "LGAI-EXAONE/EXAONE-4.0-1.2B-GGUF (Q4_K_M)",
     "dev": "LG Management Development Institute Co., Ltd.", "license": "EXAONE AI Model License 1.2 - NC (비상업)",
     "url": "https://huggingface.co/LGAI-EXAONE/EXAONE-4.0-1.2B-GGUF", "text": "EXAONE-AI-Model-License-1.2-NC.txt", "noncommercial": True},
]

# 배포하는 생성(sLLM) 모델 — 문서에는 이 둘만 싣는다. 나머지는 측정용이라 dist-rust 에 남아 있으면 경고한다
SHIP_SLLM = {"Qwen3-1.7B-Q4_K_M.gguf", "Qwen3-4B-Q4_K_M.gguf"}

# 파일 형식별 문서 파서 표 — 코드(rust/simplerag/src/extract · legacy)에서 확인한 구성
PARSERS = [
    ("PDF", "PDFium(pdfium.dll)", "Apache-2.0 / BSD-3-Clause + 구성 요소 라이선스"),
    ("DOCX · PPTX · HWPX", "zip · quick-xml · flate2 (Rust)", "MIT (flate2 는 MIT OR Apache-2.0)"),
    ("XLSX · XLS", "calamine (Rust)", "MIT"),
    ("HWP · DOC · PPT (OLE)", "cfb (Rust) + 자체 구현", "MIT"),
    ("문자 인코딩 판별", "encoding_rs (Rust)", "(Apache-2.0 OR MIT) AND BSD-3-Clause"),
]

# requirements.txt 와 배포본(dist\simplerag\_internal)에서 확인한 Python 패키지 시작점
PY_ROOTS = ["llama-cpp-python", "onnxruntime", "tokenizers", "numpy", "qdrant-client", "olefile", "pypdfium2", "xlrd", "PyYAML",
            "fastembed", "pywin32", "h2", "hpack", "hyperframe", "setuptools"]

MIT_TEMPLATE = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


#------------------------------------------------------------------
# 글 파일 읽기
#=> 인코딩이 제각각인 라이선스 파일을 깨지지 않게 읽고 줄바꿈을 맞춘다.
#
# -in: path = 읽을 파일
#
# -out: text = 파일 내용(줄바꿈 \n)
# -out: error = 파일이 없으면 OSError 전파
#------------------------------------------------------------------
def read_text(path):
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 오래된 라이선스 파일은 latin-1 이 많다 — 글자가 틀려도 멈추지 않게 한다
        text = raw.decode("latin-1")
    return text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


#------------------------------------------------------------------
# 큰 제목 만들기
#=> 긴 문서에서 찾기 쉽도록 굵은 줄로 감싼 제목을 만든다.
#
# -in: title = 제목 글
#
# -out: text = 제목 세 줄
# -out: error = 없음
#------------------------------------------------------------------
def heading(title):
    return f"\n{LINE}\n{title}\n{LINE}\n"


#------------------------------------------------------------------
# 파일로 저장
#=> 메모장에서도 한글이 깨지지 않게 BOM 을 붙이고 Windows 줄바꿈으로 쓴다.
#
# -in: path = 저장할 파일
# -in: text = 내용
#
# -out: 없음
# -out: error = 쓰기 실패 시 OSError 전파
#------------------------------------------------------------------
def write_doc(path, text):
    Path(path).write_text(text, encoding="utf-8-sig", newline="\r\n")
    print(f"만듦: {path} ({Path(path).stat().st_size:,} 바이트)")


#------------------------------------------------------------------
# 같은 글 묶기
#=> 같은 MIT 원문을 수백 번 넣지 않도록 내용이 같은 라이선스를 하나로 합치고 쓰는 곳을 함께 적는다.
#    1) 공백을 지운 글의 해시를 열쇠로 삼는다
#    2) 쓰는 패키지가 많은 원문부터 내보낸다
#
# -in: pairs = (쓰는 곳 이름, 라이선스 글) 목록
#
# -out: text = 묶은 원문 전체
# -out: error = 없음
#------------------------------------------------------------------
def group_texts(pairs):
    groups = {}
    for owner, text in pairs:
        key = hashlib.sha1(re.sub(r"\s+", "", text).encode("utf-8")).hexdigest()
        groups.setdefault(key, {"text": text, "owners": []})["owners"].append(owner)
    out = []
    for g in sorted(groups.values(), key=lambda v: (-len(v["owners"]), v["owners"][0])):
        owners = sorted(set(g["owners"]))
        out.append(f"\n{THIN}\n적용: {', '.join(owners)}\n{THIN}\n{g['text']}")
    return "".join(out)


#------------------------------------------------------------------
# 모델 라이선스 문서 쓰기
#=> 모델 폴더에 실제로 있는 모델을 표로 적고, 각 모델의 라이선스 원문을 붙인다.
#    1) 폴더에 있는 모델만 "포함" 으로 적는다
#    2) MODELS 에 없는 모델 파일이 있으면 경고를 낸다(확인 안 된 모델을 배포하지 않게)
#    3) 배포 대상이 아닌 sLLM 은 문서에서 빼고, 폴더에 남아 있으면 경고한다
#    4) 비상업 모델이 있으면 문서 맨 앞에 이용 제한을 적는다
#
# -in: dist = 배포 폴더
#
# -out: warnings = 빌드 때 알릴 경고 목록
# -out: error = 원문 파일이 없으면 OSError 전파
#------------------------------------------------------------------
def build_models(dist):
    models_dir = dist / "models"
    warnings = []
    present = []
    for m in MODELS:
        if not (models_dir / m["path"]).exists():
            continue
        # gguf 는 SHIP_SLLM 만 배포 대상이다 — 임베딩 · 리랭커는 폴더라 여기 걸리지 않는다
        if m["path"].endswith(".gguf") and m["path"] not in SHIP_SLLM:
            warnings.append(f"배포 대상이 아닌 모델이 배포본에 있음: {m['path']} ({m['license']}) — 배포 전에 빼세요")
            continue
        present.append(m)
    for name in sorted(SHIP_SLLM):
        if not (models_dir / name).exists():
            warnings.append(f"배포 대상 모델이 없음: {name}")
    known = {m["path"] for m in MODELS}
    if models_dir.is_dir():
        for p in models_dir.iterdir():
            if (p.suffix == ".gguf" or p.is_dir()) and p.name not in known:
                warnings.append(f"라이선스를 확인하지 않은 모델: {p.name} — gen_licenses.py 의 MODELS 에 넣으세요")
    doc = ["MoAI-S / SimpleRAG 모델 라이선스 안내\n", "이 배포본에 들어 있는 AI 모델과 그 라이선스입니다. 각 모델의 권리는 해당 개발사에 있습니다.\n"]
    noncom = [m for m in present if m.get("noncommercial")]
    if noncom:
        doc.append(heading("이용 제한 — 비상업 전용 모델"))
        for m in noncom:
            doc.append(f"{m['path']}\n  {m['license']}\n"
                       "  연구 · 교육 목적으로만 사용할 수 있고, 상업적 이용(수익을 내는 제품 · 서비스 포함)과\n"
                       "  그 출력물의 상업적 이용은 별도 계약 없이는 금지됩니다. 전문은 아래 원문을 따릅니다.\n")
            warnings.append(f"비상업 모델이 배포본에 있음: {m['path']} — 상업 배포 전에 빼거나 개발사와 계약하세요")
    doc.append(heading("모델 목록"))
    for m in present:
        lines = [m["path"], f"  용도       : {m['role']}", f"  모델       : {m['name']}", f"  개발       : {m['dev']}",
                 f"  라이선스   : {m['license']}", f"  출처       : {m['url']}"]
        if m.get("base"):
            lines.append(f"  기반 모델  : {m['base']}")
        doc.append("\n".join(lines) + "\n\n")
    doc.append("참고: ONNX int8 · GGUF Q4_K_M 파일은 원본 모델을 변환(양자화)한 것입니다. 가중치 형식만 바꿨고 재학습하지 않았습니다.\n")
    doc.append(heading("라이선스 원문"))
    pairs = []
    for m in present:
        text = read_text(LIC / "models" / m["text"])
        # Apache 원문의 Copyright 줄은 Alibaba 표기라 다른 개발사 모델에는 틀린 정보가 되므로 뺀다
        if m.get("strip_copyright"):
            text = re.sub(r"^\s*Copyright \d{4} Alibaba Cloud\s*$", f"   Copyright {m['dev']}", text, flags=re.M)
        pairs.append((m["path"], text))
    if any(m["path"] == "e5-small-ko" for m in present):
        pairs.append(("e5-small-ko 의 기반 모델 intfloat/multilingual-e5-small", f"MIT License\n\nCopyright (c) intfloat\n\n{MIT_TEMPLATE}"))
    doc.append(group_texts(pairs))
    write_doc(dist / "LICENSES_MODELS.txt", "".join(doc))
    return warnings


#------------------------------------------------------------------
# 빌드에 들어가는 Rust 라이브러리 목록
#=> cargo metadata 로 Windows 빌드의 실제 의존성만 고른다.
#    1) 작업 공간 패키지에서 출발해 일반 의존성만 따라간다(개발 · 빌드 전용 의존성은 배포물에 안 들어간다)
#    2) 우리 패키지(simplerag · ragsearchbox)는 뺀다
#
# -in: 없음
#
# -out: crates = cargo metadata 의 패키지 목록(이름 · 버전 순)
# -out: error = cargo 실패 시 CalledProcessError 전파
#------------------------------------------------------------------
def rust_crates():
    out = subprocess.run(["cargo", "metadata", "--format-version", "1", "--locked", "--filter-platform", "x86_64-pc-windows-msvc"],
                         cwd=ROOT / "rust", capture_output=True, check=True).stdout
    meta = json.loads(out.decode("utf-8"))
    pkgs = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    workspace = set(meta["workspace_members"])
    seen, stack = set(), list(workspace)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for dep in nodes[cur]["deps"]:
            # dep_kinds 의 kind 가 None 이면 일반 의존성이다
            if any(k["kind"] is None for k in dep["dep_kinds"]):
                stack.append(dep["pkg"])
    return sorted((pkgs[i] for i in seen - workspace), key=lambda p: (p["name"], p["version"]))


#------------------------------------------------------------------
# 패키지 안의 라이선스 파일 찾기
#=> 이름이 LICENSE · COPYING · NOTICE 같은 파일을 모은다.
#    C 소스를 품은 -sys 패키지는 그 안쪽 폴더의 라이선스(예: oniguruma/COPYING)도 찾는다.
#
# -in: folder = 패키지 폴더
# -in: deep   = True 면 하위 폴더까지 찾는다
#
# -out: files = 찾은 파일 경로 목록
# -out: error = 없음 (없으면 빈 목록)
#------------------------------------------------------------------
def license_files(folder, deep=False):
    pattern = re.compile(r"^(licen[cs]e|copying|copyright|notice|unlicense)([-._].*)?$", re.I)
    walk = folder.rglob("*") if deep else folder.iterdir()
    return sorted(p for p in walk if p.is_file() and pattern.match(p.name) and p.suffix.lower() not in (".rs", ".toml", ".py", ".json"))


#------------------------------------------------------------------
# 문서 파서 · 런타임 · Rust 라이브러리 문서 쓰기
#=> 배포 폴더의 exe · DLL 에 실제로 들어간 구성 요소의 고지문을 한 파일로 만든다.
#    1) 형식별 문서 파서 표
#    2) DLL 런타임 — ONNX Runtime · PDFium · llama.cpp 의 고지문 전문
#    3) Rust 라이브러리 표와 라이선스 원문(같은 글은 묶는다)
#
# -in: dist = 배포 폴더
#
# -out: 없음
# -out: error = .venv · cargo 가 없으면 예외 전파
#------------------------------------------------------------------
def build_third_party(dist):
    crates = rust_crates()
    doc = ["MoAI-S / SimpleRAG 제3자 소프트웨어 라이선스 안내\n", "이 배포본의 exe · DLL 에 들어 있는 문서 파서, 런타임, Rust 라이브러리의 라이선스입니다.\n"]
    doc.append(heading("1. 문서 파서"))
    for fmt, lib, lic in PARSERS:
        doc.append(f"{fmt:<26} {lib}\n{'':<26} 라이선스: {lic}\n")
    doc.append("\n사이냅 문서 필터(snf_exe.exe)는 상용 제품으로 이 배포본에 들어 있지 않습니다.\n"
               "exe 옆 synap 폴더에 따로 설치한 경우에만 쓰이며, 그때는 사이냅소프트의 사용권을 따릅니다.\n")
    doc.append(heading("2. 런타임"))
    doc.append("onnxruntime.dll · onnxruntime_providers_shared.dll\n  ONNX Runtime 1.29.0 — MIT, Microsoft Corporation\n  https://github.com/microsoft/onnxruntime\n\n"
               "pdfium.dll\n  PDFium(pdfium-binaries 빌드) — Apache-2.0 / BSD-3-Clause + 구성 요소 라이선스\n  https://pdfium.googlesource.com/pdfium/\n\n"
               "runtime\\llama_cpu · runtime\\llama_vulkan\n  llama.cpp(llama-cpp-python 0.3.35 휠) — MIT\n  https://github.com/ggml-org/llama.cpp\n\n"
               "vcruntime140 · vcruntime140_1 · msvcp140 · msvcp140_1 · vcomp140.dll 은 Microsoft Visual C++ 재배포 패키지 파일로, Microsoft 재배포 조항을 따릅니다.\n"
               "Microsoft Edge WebView2 Runtime · Vulkan 드라이버는 이 배포본에 넣지 않고 PC 에 설치된 것을 씁니다.\n"
               "webview2-com-sys 는 Microsoft WebView2 SDK 의 로더를 포함하며 Microsoft 의 SDK 사용권을 따릅니다.\n"
               "  https://www.nuget.org/packages/Microsoft.Web.WebView2/\n")
    pairs = [("ONNX Runtime", read_text(VENV_SITE / "onnxruntime" / "LICENSE"))]
    for p in sorted((LIC / "runtime").glob("*.txt")):
        pairs.append((p.stem.replace("-LICENSE", " ").replace("-", " ").strip(), read_text(p)))
    pdf_dir = next(VENV_SITE.glob("pypdfium2-*.dist-info")) / "licenses"
    for p in sorted((pdf_dir / "data" / "windows_x64" / "BUILD_LICENSES").iterdir()):
        pairs.append((f"PDFium 구성 요소 {p.stem}", read_text(p)))
    for p in sorted((pdf_dir / "LICENSES").glob("*.txt")):
        if p.stem in ("Apache-2.0", "BSD-3-Clause"):
            pairs.append(("PDFium 바인딩 · 빌드", read_text(p)))
    doc.append(group_texts(pairs))
    doc.append(heading("2-1. ONNX Runtime 제3자 고지문(ThirdPartyNotices.txt 전문)"))
    doc.append(read_text(VENV_SITE / "onnxruntime" / "ThirdPartyNotices.txt"))

    doc.append(heading(f"3. Rust 라이브러리 ({len(crates)}개)"))
    doc.append("simplerag-rs.exe · MoAISearch.exe 에 정적으로 링크됩니다. 여러 라이선스 중 고를 수 있는 경우(OR) 표기대로 고를 수 있습니다.\n\n")
    pairs, missing = [], []
    for c in crates:
        repo = c.get("repository") or ""
        doc.append(f"{c['name']} {c['version']}  [{c.get('license') or c.get('license_file') or '표기 없음'}]  {repo}\n")
        owner = f"{c['name']} {c['version']}"
        folder = Path(c["manifest_path"]).parent
        files = license_files(folder, deep=c["name"].endswith(("-sys", "_sys")))
        for f in files:
            pairs.append((owner, read_text(f)))
        if not files:
            missing.append(c)
    doc.append(heading("3-1. Rust 라이브러리 라이선스 원문"))
    doc.append(group_texts(pairs))
    if missing:
        doc.append(heading("3-2. 패키지에 라이선스 파일이 없는 라이브러리"))
        doc.append("아래는 패키지에 원문 파일이 없어 Cargo.toml 표기를 따릅니다. 저작자는 패키지 표기입니다.\n\n")
        for c in missing:
            doc.append(f"{c['name']} {c['version']}  [{c.get('license')}]  저작자: {', '.join(c.get('authors') or ['표기 없음'])}\n")
        doc.append(f"\n{THIN}\nMIT 표준 조항\n{THIN}\n{MIT_TEMPLATE}")
        doc.append(f"\n{THIN}\nApache-2.0 표준 조항\n{THIN}\n{read_text(LIC / 'models' / 'Kanana-1.5-Apache-2.0.txt')}")
    write_doc(dist / "LICENSES_THIRD_PARTY.txt", "".join(doc))


#------------------------------------------------------------------
# Python 패키지 라이선스 표기 읽기
#=> 패키지 정보에서 사람이 읽을 라이선스 이름을 고른다(표기 방식이 패키지마다 다르다).
#
# -in: dist = importlib.metadata 패키지 정보
#
# -out: name = 라이선스 이름(없으면 "표기 없음")
# -out: error = 없음
#------------------------------------------------------------------
def py_license(dist):
    m = dist.metadata
    name = m.get("License-Expression") or (m.get("License") or "").split("\n")[0].strip()
    if name and len(name) < 70:
        return name
    labels = [c.split("::")[-1].strip() for c in (m.get_all("Classifier") or []) if c.startswith("License")]
    return ", ".join(labels) or "표기 없음"


#------------------------------------------------------------------
# Python 판 라이선스 문서 쓰기
#=> requirements 에서 출발해 의존성을 따라가며 패키지 표와 원문을 만든다.
#    1) .venv 의 패키지 정보에서 실제 의존성(선택 기능 제외)을 모은다
#    2) 패키지 안의 라이선스 파일을 묶어 붙인다
#    3) Python 본체 · PyInstaller · 런타임(ONNX Runtime · PDFium · llama.cpp) 고지문을 덧붙인다
#
# -in: dist = Python 판 배포 폴더
#
# -out: 없음
# -out: error = .venv 가 없으면 StopIteration · OSError 전파
#------------------------------------------------------------------
def build_python(dist):
    norm = lambda s: re.sub(r"[-_.]+", "-", s).lower()
    installed = {norm(d.metadata["Name"]): d for d in metadata.distributions(path=[str(VENV_SITE)])}
    seen, stack = {}, [norm(r) for r in PY_ROOTS]
    while stack:
        cur = stack.pop()
        if cur in seen or cur not in installed:
            continue
        seen[cur] = installed[cur]
        for req in seen[cur].requires or []:
            # extra 조건이 붙은 의존성은 선택 기능이라 배포본에 없다
            if "extra ==" not in req:
                stack.append(norm(re.match(r"[A-Za-z0-9_.-]+", req)[0]))
    doc = ["SimpleRAG Python 판 라이브러리 라이선스 안내\n",
           "Python 판 배포본에 들어 있는 패키지와 런타임의 라이선스입니다. AI 모델의 라이선스는 이 문서에 없습니다.\n"]
    doc.append(heading("문서 파서(Python 판)"))
    doc.append("PDF                pypdfium2(PDFium)\nHWP · DOC · PPT    olefile + CSOClassify 추출 모듈(자체 구현)\nXLS                xlrd\n")
    doc.append(heading(f"패키지 목록 ({len(seen)}개)"))
    pairs = []
    pattern = re.compile(r"(^|/)(licen[cs]e|licen[cs]es/|copying|notice|authors?)", re.I)
    for name in sorted(seen):
        d = seen[name]
        doc.append(f"{d.metadata['Name']} {d.version}  [{py_license(d)}]\n")
        for f in d.files or []:
            path = str(f).replace("\\", "/")
            # 바이너리 · 문서 HTML 은 고지문이 아니다
            if pattern.search(path) and Path(path).suffix.lower() in ("", ".txt", ".md", ".rst", ".apache", ".bsd", ".ijg") and "/data/windows" not in path and "/data/" not in path:
                target = Path(d.locate_file(f))
                if target.is_file():
                    pairs.append((f"{d.metadata['Name']} {d.version}", read_text(target)))
    doc.append(heading("파일 단위 공개 의무가 있는 패키지(MPL-2.0)"))
    doc.append("certifi · tqdm 은 MPL-2.0 을 포함합니다. 수정하지 않고 배포하며, 소스는 각 패키지의 공식 저장소에서 받을 수 있습니다.\n")
    doc.append(heading("Python 본체 · 패키징 도구"))
    doc.append("Python 3.12 — Python Software Foundation License\nPyInstaller — GPL-2.0-or-later + 부트로더 예외(만든 프로그램의 배포 조건을 제한하지 않음)\n"
               "Visual C++ 런타임 · Universal CRT DLL — Microsoft 재배포 조항을 따름\n")
    py_home = Path(sys.base_prefix)
    if (py_home / "LICENSE.txt").is_file():
        pairs.append(("Python 3.12 본체", read_text(py_home / "LICENSE.txt")))
    for p in VENV_SITE.glob("pyinstaller-*.dist-info/licenses/*"):
        pairs.append(("PyInstaller", read_text(p)))
    # Rust 판 문서를 함께 배포하지 않으므로 런타임 구성 요소의 고지문도 여기에 싣는다
    pdf_dir = next(VENV_SITE.glob("pypdfium2-*.dist-info")) / "licenses"
    for p in sorted((pdf_dir / "data" / "windows_x64" / "BUILD_LICENSES").iterdir()):
        pairs.append((f"PDFium 구성 요소 {p.stem}", read_text(p)))
    for p in sorted((LIC / "runtime").glob("*.txt")):
        pairs.append((p.stem.replace("-LICENSE", " ").replace("-", " ").strip(), read_text(p)))
    doc.append(heading("라이선스 원문"))
    doc.append(group_texts(pairs))
    doc.append(heading("ONNX Runtime 제3자 고지문(ThirdPartyNotices.txt 전문)"))
    doc.append(read_text(VENV_SITE / "onnxruntime" / "ThirdPartyNotices.txt"))
    write_doc(dist / "LICENSES_PYTHON.txt", "".join(doc))


#------------------------------------------------------------------
# 진입점
#=> 세 문서를 만들고 빌드 때 알아야 할 경고를 보인다.
#
# -in: 없음 (명령줄 첫 인자 = 출력 폴더, 기본 dist-rust)
#
# -out: 없음
# -out: error = 하위 함수의 예외 전파
#------------------------------------------------------------------
def main():
    # 콘솔이 cp949 여도 경고 글자 때문에 멈추지 않게 한다
    sys.stdout.reconfigure(errors="replace")
    dist = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist-rust"
    warnings = build_models(dist)
    build_third_party(dist)
    # Python 패키지는 Rust 판에 없다 — 그 패키지가 실제로 들어 있는 Python 판 배포본에만 둔다
    py_dist = ROOT / "dist" / "simplerag"
    if py_dist.is_dir():
        build_python(py_dist)
    for w in warnings:
        print(f"⚠️ {w}")


if __name__ == "__main__":
    main()
