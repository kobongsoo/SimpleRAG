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


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    for alias, repo, filename in TARGETS:
        try:
            fetch(repo, filename)
        except Exception as e:
            print(f"[fail] {alias}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
    print("\n완료. 이제 bench_llm.py 를 실행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
