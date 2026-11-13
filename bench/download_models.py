#------------------------------------------------------------------
# 벤치 대상 GGUF 모델 내려받기
#=> HF 에서 Q4_K_M 파일만 models/ 로 받는다. 이미 있으면 건너뛴다.
#   공식 Qwen/*-GGUF 는 Q8_0 만 올려두어, Q4_K_M 은 unsloth 배포본을 쓴다.
#
#   사용:  python bench/download_models.py
#------------------------------------------------------------------
import os
import sys
from huggingface_hub import hf_hub_download

_HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(_HERE, "..", "models")

# (별칭, HF repo, 파일명)
TARGETS = [
    ("qwen3-0.6b-q4", "unsloth/Qwen3-0.6B-GGUF", "Qwen3-0.6B-Q4_K_M.gguf"),
    ("qwen3-1.7b-q4", "unsloth/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q4_K_M.gguf"),
]


#------------------------------------------------------------------
# 모델 1개 확보
#=> models/<파일명> 이 이미 있으면 그 경로를 그대로 돌려준다(재다운로드 방지).
#
# -in: repo, filename = HF 저장소/파일명
#
# -out: path = 로컬 파일 경로
#------------------------------------------------------------------
def fetch(repo, filename):
    dest = os.path.abspath(os.path.join(MODELS_DIR, filename))
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        print(f"[skip] 이미 있음: {dest} ({os.path.getsize(dest)/1024/1024:.1f} MB)")
        return dest

    print(f"[get ] {repo}/{filename} → {MODELS_DIR}")
    src = hf_hub_download(repo_id=repo, filename=filename,
                          local_dir=os.path.abspath(MODELS_DIR))
    print(f"[ok  ] {src} ({os.path.getsize(src)/1024/1024:.1f} MB)")
    return src


#------------------------------------------------------------------
# OCR 모델 확보 (OCR 설계서 §3-3 · §10)
#=> 스캔 문서·그림 파일 OCR 에 쓰는 두 ONNX 모델과 글자 사전을 models/ocr/ 에 둔다.
#    1) 검출 PaddlePaddle/PP-OCRv6_tiny_det_onnx → PP-OCRv6_tiny_det.onnx
#    2) 인식 PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx → korean_PP-OCRv5_mobile_rec.onnx
#    3) 인식 모델 inference.yml 의 character_dict 를 한 줄에 한 글자로 → korean_PP-OCRv5_mobile_rec.dict.txt
#   이미 있는 파일은 건너뛴다. (v6 인식 모델은 한글이 0자라 쓰지 않는다 — 2026-11-13 실측)
#
# -in: 없음
#
# -out: ocr_dir = models/ocr 경로
# -out: error = 내려받기 실패·yml 읽기 실패 시 예외 전파
#------------------------------------------------------------------
def fetch_ocr():
    import shutil
    import yaml
    ocr_dir = os.path.abspath(os.path.join(MODELS_DIR, "ocr"))
    os.makedirs(ocr_dir, exist_ok=True)
    # (HF 저장소, 받을 파일, 둘 이름)
    want = [
        ("PaddlePaddle/PP-OCRv6_tiny_det_onnx", "inference.onnx", "PP-OCRv6_tiny_det.onnx"),
        ("PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx", "inference.onnx", "korean_PP-OCRv5_mobile_rec.onnx"),
    ]
    for repo, fn, name in want:
        dest = os.path.join(ocr_dir, name)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            print(f"[skip] 이미 있음: {dest}")
            continue
        print(f"[get ] {repo}/{fn}")
        src = hf_hub_download(repo_id=repo, filename=fn)
        # 캐시 파일을 이름만 바꿔 복사한다(두 저장소 파일 이름이 모두 inference.onnx 라 겹치지 않게)
        shutil.copy2(src, dest)
        print(f"[ok  ] {dest} ({os.path.getsize(dest)/1024/1024:.1f} MB)")
    dict_path = os.path.join(ocr_dir, "korean_PP-OCRv5_mobile_rec.dict.txt")
    if not os.path.isfile(dict_path):
        yml = hf_hub_download(repo_id="PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx", filename="inference.yml")
        chars = yaml.safe_load(open(yml, encoding="utf-8"))["PostProcess"]["character_dict"]
        # 한 줄에 한 글자 — 앞 'blank' · 끝 띄어쓰기는 엔진이 붙인다
        with open(dict_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(chars) + "\n")
        print(f"[ok  ] {dict_path} ({len(chars)}자)")
    return ocr_dir


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    for alias, repo, filename in TARGETS:
        try:
            fetch(repo, filename)
        except Exception as e:
            print(f"[fail] {alias}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    try:
        fetch_ocr()
    except Exception as e:
        print(f"[fail] ocr: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("\n완료. 이제 bench_llm.py 를 실행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
