# RAGSearchBox

Windows 탐색기에서 **지정한 폴더를 열면 오른쪽에 대화 패널**이 떠서,
그 폴더의 문서에 대해 물어보면 SimpleRAG 가 근거를 찾아 답해 주는 상주 프로그램입니다.

```
탐색기에서 지정 폴더를 연다  →  오른쪽에 패널이 뜬다
      ┌─────────────────────────────┐
      │ 📁 D:\분류함\규정            │
      │ 근거 3건 · AI 요약           │
      │ ...                          │
      ├─────────────────────────────┤
      │ [질문을 쓰세요]      보내기  │
      └─────────────────────────────┘
```

패널은 탐색기와 **겹치지 않습니다** — 자리를 만들려고 탐색기 창을 왼쪽으로 물리고,
패널이 사라지면 원래 자리로 돌려줍니다. 범위 밖 폴더로 나가면 저절로 사라집니다.

> 예전 방식(검색창에 `?질문` + Enter)은 `[Trigger] SearchBox = 1` 로 켤 수 있습니다. 기본은 꺼짐입니다.

설계 문서: [`plan/RAGSearchBox_설계서.html`](../plan/RAGSearchBox_설계서.html)

---

## 1. 설치

### 필요한 것

* Windows 10 이상 (Windows 11 에서 실측 검증)
* 이미 빌드된 SimpleRAG 배포 폴더 (`dist\simplerag`) 와 인덱싱된 문서
* 관리자 권한은 **필요 없습니다**

### 배치

`RAGSearchBox.exe` 를 `simplerag.exe` 와 **같은 폴더**에 둡니다. 그러면 워커를 자동으로 찾습니다.

```
dist\simplerag\
├─ simplerag.exe          ← 워커(모델). --python-option u 로 빌드된 것이어야 합니다
├─ RAGSearchBox.exe       ← 이 프로그램
├─ RAGSearchBox.ini       ← 설정 (필수: [Scope] Folders)
├─ models\  qdrant_data\  runtime\  …
```

> ⚠️ 함께 쓰는 `simplerag.exe` 는 `--python-option u` 로 빌드된 것이어야 합니다.
> 그 옵션이 없으면 근거가 답변과 함께 늦게 뜹니다(설계서 D8). `build_exe.py` 는 이미 이 옵션을 씁니다.

### 설정 — 이것만 하면 됩니다

`RAGSearchBox.ini` 의 `[Scope] Folders` 에 **동작할 폴더**를 적습니다. 여러 개면 `;` 로 나눕니다.

```ini
[Scope]
Folders = D:\분류함;\\fileserver\공유\규정
```

**비워 두면 아무 데서도 동작하지 않습니다.** (모델도 올리지 않습니다.)
지정한 폴더의 하위 폴더까지 포함합니다.

> 폴더 제한은 **질문을 받을지**만 정합니다. 검색 자체는 인덱스 전체에서 하므로,
> 지정 폴더와 인덱싱한 폴더를 맞춰 두는 것이 운영 규칙입니다.

---

## 2. 사용

1. `RAGSearchBox.exe` 실행 → 트레이에 상주합니다 (창은 뜨지 않습니다)
2. 탐색기에서 **지정한 폴더(또는 그 하위)를 엽니다** → 오른쪽에 패널이 뜹니다
3. 아래 입력 칸에 질문을 쓰고 **Enter** (줄바꿈은 Shift+Enter)
4. 근거가 먼저 뜨고 그다음 답변이 흘러나옵니다. 계속 이어서 물을 수 있습니다
5. **근거 파일 보기** 를 누르면 그 탐색기 탭이 근거 파일 목록으로 바뀝니다 (**원래대로** 로 복귀)

패널을 끌어다 옮기면 제자리로 돌아옵니다(붙어 있는 창이라서입니다). 가장자리를 끌어 **너비는 바꿀 수 있고, 그 값은 설정에 저장**됩니다.

패널은 뜰 때 포커스를 뺏지 않습니다 — 탐색기에서 하던 일을 계속하다가, 물어볼 때 패널을 누르면 됩니다.
닫기(X)를 누르면 그 탐색기 창에서 범위 폴더를 보는 동안은 다시 뜨지 않습니다.
**범위 밖 폴더에 갔다 돌아오거나**, 트레이 메뉴 **창 열기** 를 고르면 다시 뜹니다.

**대화 지우기** 는 쌓인 질문·답변을 모두 비웁니다.

### 트레이 메뉴 (아이콘 오른쪽 클릭)

| 항목 | 설명 |
|---|---|
| 창 열기 | 닫은 패널을 다시 띄웁니다. 범위 폴더를 보는 탐색기 창이 있으면 그 옆에, 없으면 첫 번째 범위 폴더를 탐색기로 열어 붙입니다. 패널이 앞으로 오고 입력 칸에 커서가 놓입니다 |
| 모델 내리기 | 메모리(약 2.5GB)와 **인덱스 잠금**을 돌려줍니다. `simplerag index` 를 돌리기 전에 누르세요 |
| 모델 다시 올리기 | 인덱싱이 끝난 뒤 다시 올립니다 (다음 질문 때 자동으로 올라오기도 합니다) |
| 로그인 시 자동 시작 | 켜고 끕니다 (아래 참고) |
| 로그 폴더 열기 | `%LOCALAPPDATA%\RAGSearchBox\log` |
| 종료 | 워커까지 함께 내려갑니다 |

트레이 아이콘에 마우스를 올리면 지금 상태(준비됨 / 답변 중 / 내려감)가 보입니다.

---

## 3. 로그인할 때 자동 시작

기본은 **등록하지 않습니다**. 원하면 한 번만 실행하세요.

```bat
RAGSearchBox.exe --autorun on       :: 등록
RAGSearchBox.exe --autorun off      :: 해제
RAGSearchBox.exe --autorun status   :: 확인 (종료 코드 0=등록됨, 1=안 됨)
```

* 등록 위치는 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 입니다(사용자 단위, 관리자 권한 불필요)
* 프로그램 폴더를 옮기면 다음 실행 때 경로를 알아서 고칩니다
* **프로그램을 지우기 전에 `--autorun off` 를 먼저 실행하세요** — 없는 exe 를 가리키는 등록이 남지 않게
* 창이 없는 프로그램이라 결과는 명령을 친 콘솔에 나옵니다. 콘솔 없이 실행했으면 로그에 남고, `on`/`off` 는 알림 창으로도 알려 줍니다

`StartMode = boot`(기본) 와 함께 쓰면 로그인 직후 모델을 미리 올립니다(약 10초 CPU 사용).
범위 폴더가 비어 있으면 올리지 않습니다.

---

## 4. 설정 항목

| 섹션 | 키 | 기본 | 설명 |
|---|---|---|---|
| SimpleRAG | `SimpleRagExe` | (자동) | 비우면 exe 옆 → `..\dist\simplerag` 순서로 찾습니다 |
| | `NoStream` | 0 | 1 이면 답변을 한 번에 받습니다 |
| Worker | `StartMode` | boot | `boot`=시작 때 예열 / `lazy`=첫 질문 때 |
| | `IdleUnloadMin` | 60 | 이 시간(분) 질문이 없으면 모델을 내립니다 (0=유지) |
| | `AnswerTimeoutSec` | 60 | 답변이 멈추면 워커를 다시 올립니다 |
| Monitor | `Mode` | 0 | 0=포커스 이벤트(기본) / 1=주기 조회 |
| Panel | `Enabled` | 1 | 폴더 패널을 쓸지 |
| | `ShrinkExplorer` | 1 | 자리를 만들려고 탐색기 창을 왼쪽으로 물릴지 |
| | `FolderPollMs` | 500 | 지금 보고 있는 폴더를 확인하는 주기(ms) |
| Trigger | `SearchBox` | 0 | **옛 방식**(검색창에 `?` 입력 → 답변 창). 0 이면 검색창을 찾지도 않습니다 |
| | `Prefix` / `MinChars` | ? / 2 | 옛 방식에서 질문으로 볼 접두어와 최소 길이 |
| Scope | `Folders` | (없음) | **필수.** `;` 로 나눈 폴더 목록 |
| Window | `Width` | 460 | 패널·답변 창 너비. **패널 가장자리를 끌어 바꾸면 이 값이 자동으로 갱신됩니다**(화면 절반까지) |
| | `MaxHeight` / `FontSize` | 560 / 10 | 답변 창 최대 높이(패널은 탐색기 창 높이에 맞춥니다) · 글자 크기 |
| Log | `Level` | INFO | `DIAG` 로 바꾸면 검색창 판정까지 자세히 남습니다 |

값이 범위를 벗어나면 기본값으로 되돌리고 트레이 알림과 로그로 알려 줍니다(프로그램은 계속 돕니다).

---

## 5. 잘 안 될 때

로그부터 보세요: 트레이 메뉴 → **로그 폴더 열기** (`%LOCALAPPDATA%\RAGSearchBox\log`)

| 증상 | 확인할 것 |
|---|---|
| 패널이 안 뜬다 | `[Scope] Folders` 에 지금 폴더가 들어 있는가 · 탐색기가 앞에 있는가 · 그 창에서 닫기를 누르지 않았는가(트레이 → **창 열기**) |
| 아무 반응이 없다 | `Level = DIAG` 로 바꾸고 다시 시도 — `폴더 바뀜` / `패널 표시` 가 찍히는지 봅니다 |
| "SimpleRAG 를 찾지 못했습니다" | `RAGSearchBox.exe` 가 `simplerag.exe` 옆에 있는지, 아니면 INI 의 `SimpleRagExe` 를 지정 |
| `simplerag index` 가 잠금 오류 | 트레이 → **모델 내리기** 후 인덱싱, 끝나면 **모델 다시 올리기** |
| "SimpleRAG chat/index 가 실행 중입니다" | 다른 곳에서 chat/index 를 쓰는 중입니다. 끝낸 뒤 다시 질문하세요 |
| 두 번 실행해도 하나만 뜬다 | 정상입니다. 중복 실행을 막습니다(모델이 둘이 되지 않게) |

---

## 6. 개발자용

```bat
:: 의존성 (SimpleRAG 의 .venv 를 공유합니다)
.venv\Scripts\python.exe -m pip install -r RAGSearchBox\requirements.txt

:: 소스로 실행
.venv\Scripts\python.exe RAGSearchBox\main.py

:: 단위 시험 (탐색기·모델 없이 도는 것들)
cd RAGSearchBox\tests && ..\..\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"

:: 아이콘 다시 그리기
.venv\Scripts\python.exe RAGSearchBox\make_icon.py

:: exe 빌드 (+ dist\simplerag 로 배포)
.venv\Scripts\python.exe RAGSearchBox\build_searchbox.py --deploy
```

눈으로 보는 확인 스크립트도 있습니다.

* `tests\manual_panel.py` — 폴더 패널을 가짜 워커로 띄워 봅니다
* `tests\manual_ui.py` — 옛 답변 창·트레이를 띄워 봅니다
* `tests\manual_app.py` — 감시를 빼고 app 전체 흐름을 확인합니다
* `tests\manual_errors.py` — 오류 상황을 일부러 만들어 봅니다

모든 `.py` 함수·클래스·메서드에는 `D:\Project\CLAUDE.md` 형식의 한글 주석 헤더를 답니다.
