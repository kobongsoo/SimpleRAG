#------------------------------------------------------------------
# GGUF 후보 모델 메타데이터 조회 (다운로드 전 확인용)
#=> HF 저장소의 파일 목록/크기만 읽어 Q4_K_M 후보를 찾아 출력한다.
#   가중치는 내려받지 않는다(메타데이터 API만 호출).
#------------------------------------------------------------------
import sys
from huggingface_hub import HfApi

CANDIDATES = [
    ("Qwen/Qwen3-1.7B-GGUF",     "Qwen3-1.7B"),
    ("Qwen/Qwen3-0.6B-GGUF",     "Qwen3-0.6B"),
    ("unsloth/Qwen3-1.7B-GGUF",  "Qwen3-1.7B (unsloth)"),
    ("unsloth/Qwen3-0.6B-GGUF",  "Qwen3-0.6B (unsloth)"),
]

api = HfApi()

for repo, label in CANDIDATES:
    print(f"\n=== {label}  [{repo}] ===")
    try:
        info = api.repo_info(repo, files_metadata=True)
    except Exception as e:
        print(f"  조회 실패: {type(e).__name__}: {e}")
        continue

    hits = []
    for f in info.siblings or []:
        name = f.rfilename
        if not name.lower().endswith(".gguf"):
            continue
        # 속도 벤치 대상: Q4_K_M 우선, 비교용으로 Q8_0/Q5_K_M 도 표시
        if any(q in name.upper() for q in ("Q4_K_M", "Q5_K_M", "Q8_0")):
            size = f.size or 0
            hits.append((name, size))

    if not hits:
        print("  Q4_K_M 계열 파일 없음")
    for name, size in sorted(hits):
        print(f"  {name:<45} {size/1024/1024:8.1f} MB")
