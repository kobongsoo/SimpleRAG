#------------------------------------------------------------------
# 메타데이터 규칙 사전 (메타데이터 검색 설계서 §7-1 · §10-2 · §10-3 · §10-4 · §12-2)
#=> 정규화에 쓰는 낱말 목록을 한곳에 모은다. 설치처마다 다른 값(회사 메일 · 그룹웨어 도메인,
#   스캐너 모델 등)은 데이터 폴더의 meta_rules.yaml 로 덧붙일 수 있다(코드를 고치지 않고).
#   yaml 의 같은 키는 기본 목록에 더해진다(덮어쓰지 않는다).
#------------------------------------------------------------------

import os

# ── 사람 (§7-1) ──────────────────────────────────────────
# 대소문자 무시 완전 일치면 사람이 아니다(PC · 계정 기본값 · 회사 · OS 이름)
JUNK_PEOPLE = [
    "user", "pc", "home", "admin", "administrator", "owner", "windows xp", "windows user",
    "windows 사용자", "microsoft 계정", "microsoft corporation", "microsoft office user",
    "ibm_user", "winxp", "사용자", "사용자1", "user1", "guest", "test", "unknown",
    "microsoft", "hp", "dell", "samsung", "lg", "ibm", "lenovo", "mac", "pptxgenjs",
    "your user name", "osd user", "내부pc", "python-docx", "python-pptx", "openpyxl", "reportlab",
]
# 정규식(대소문자 무시)으로 걸리면 사람이 아니다 — 회사 · 기관 · 장비 · 프로그램 이름
JUNK_PEOPLE_PATTERNS = [
    r"\b(inc|corp|corporation|co|ltd|llc|gmbh|company|technologies|technology|foundation|institute|"
    r"university|computer|computers|customer|systems?|software|solutions?|group|team|tft|lab|labs)\b\.?",
    r"(^|[\s\-_])pc\d*$", r"^pc\d+$", r"\buser\d*$", r"^(주|\(주\)|㈜)", r"(주식회사|재단|협회|연구원|대학교)$",
    r"^(asus|acer|samsung|lg|hp|dell|lenovo)$", r"^win(7|8|10|11|xp)$", r"(desktop|laptop|notebook)$",
    r"\bnetwork\b",
]
# 한글 낱말이 이것으로 끝나면 소속(부서)이다 — "이경준 매니저 정보화전략팀" 에서 떼어 낸다
ORG_SUFFIX = ["팀", "부", "실", "그룹", "본부", "센터", "랩스", "사업부", "연구소"]
# 이름 앞뒤에 붙는 직함(떼어 낸다)
TITLES = [
    "대표이사", "대표", "사장", "부사장", "전무", "상무", "이사", "본부장", "실장", "부장", "차장",
    "과장", "대리", "주임", "사원", "팀장", "파트장", "책임", "선임", "수석", "연구원", "매니저",
    "프로", "님",
]

# ── 원본 프로그램 (§10-2) — (값, Creator · Producer · Application 에 들어 있는 말) 앞에서부터 ──
ORIGIN_APPS = [
    ("scanner", ["apeosport", "sindoh", "samsung-scx", "samsung scx", "sl-c", "canon ir", "ricoh",
                 "konica", "docucentre", "xerox", "scanner", "scansnap"]),
    ("hwp", ["hancom pdf", "hwp", "한글", "hancom"]),
    ("word", ["microsoft word", "word"]),
    ("excel", ["microsoft excel", "excel"]),
    ("ppt", ["microsoft powerpoint", "powerpoint"]),
    ("print", ["print to pdf", "pscript", "distiller", "ghostscript", "pdf printer", "pdfcreator",
               "cutepdf", "bullzip"]),
    ("web", ["chrome", "skia", "mozilla", "edge", "wkhtmltopdf"]),
]

# ── 받은 경로 (§10-3) — 도메인 끝부분 일치 ──
RECV_DOMAINS = {
    "mail": ["ezwebmail.bizmeka.com", "outlook.office.com", "outlook.live.com",
             "mail-attachment.googleusercontent.com", "mail.naver.com", "mail.daum.net"],
    "drive": ["drive.usercontent.google.com", "drive.google.com", "zdl-storage.worksmobile.com",
              "onedrive.live.com", "sharepoint.com", "dropbox.com", "dropboxusercontent.com"],
    "intranet": ["mpower.mocomsys.com"],
    "ai": ["claude.ai", "chatgpt.com", "chat.openai.com", "gemini.google.com"],
}
# 이 접두로 시작하는 호스트도 사내로 본다(그룹웨어)
INTRANET_PREFIX = ["gw.", "groupware.", "intranet."]

# ── 문서 유형 · 양식 (§10-4) ──
DOC_TYPES = [
    "절차서", "지침", "규정", "매뉴얼", "메뉴얼", "제안서", "계약서", "보고서", "회의록", "확인서",
    "계획서", "정의서", "명세서", "설계서", "견적서", "요청서", "결과서", "기안서", "품의서",
    "가이드", "교육자료", "사용자설명서", "점검표",
]
FORM_WORDS = ["양식", "서식", "신청서", "의뢰서", "일지", "별지", "템플릿", "template"]
FORM_FOLDER_WORDS = ["템플릿", "양식", "서식"]
FORM_SHEETS = ["표지", "작성방법", "작성 방법", "입력"]
TEMPLATE_EXTS = ["dotx", "dotm", "dot", "xltx", "xltm", "xlt", "potx", "potm", "pot", "hwt"]

# 이 이름(대소문자 무시, 확장자 무시)의 템플릿은 "양식 정보 없음"으로 본다
PLAIN_TEMPLATES = ["normal", "normal.dot", "normal.dotm", "normal.dotx", "blank", "빈 프레젠테이션",
                   "office theme", "office 테마"]

# ── 제목 쓰레기 (§9-1) — 정규식 ──
TITLE_JUNK = [
    r"^슬라이드\s*\d+$", r"^slide\s*\d+$", r"^powerpoint\s*프레젠테이션$", r"^powerpoint presentation$",
    r"^pptxgenjs presentation$", r"^제목\s*없음$", r"^untitled$", r"^문서\s*\d*$", r"^document\s*\d*$",
    r"^\d+$", r"\.(dvi|doc|docx|hwp|hwpx|xls|xlsx|ppt|pptx|pdf|txt|tex)$", r"^presentation\d*$",
]


#------------------------------------------------------------------
# 규칙 읽기 — 기본값 + 데이터 폴더 meta_rules.yaml
#=> yaml 이 있으면 같은 이름의 목록에 덧붙인다(dict 는 키별로 목록을 덧붙임).
#    1) 기본값을 복사한다
#    2) <root>/meta_rules.yaml 을 읽어 덧붙인다(없거나 깨졌으면 기본값만)
#
# -in: root = 데이터 폴더(None 이면 기본값만)
#
# -out: rules = {이름: 목록 또는 dict}
# -out: error = 없음 (yaml 이 깨졌으면 무시하고 기본값)
#------------------------------------------------------------------
def load_rules(root=None):
    rules = {
        "junk_people": list(JUNK_PEOPLE), "junk_people_patterns": list(JUNK_PEOPLE_PATTERNS),
        "org_suffix": list(ORG_SUFFIX), "titles": list(TITLES), "origin_apps": [list(x) for x in ORIGIN_APPS],
        "recv_domains": {k: list(v) for k, v in RECV_DOMAINS.items()}, "intranet_prefix": list(INTRANET_PREFIX),
        "doc_types": list(DOC_TYPES), "form_words": list(FORM_WORDS), "form_folder_words": list(FORM_FOLDER_WORDS),
        "form_sheets": list(FORM_SHEETS), "template_exts": list(TEMPLATE_EXTS),
        "plain_templates": list(PLAIN_TEMPLATES), "title_junk": list(TITLE_JUNK),
    }
    path = os.path.join(root, "meta_rules.yaml") if root else None
    if not path or not os.path.isfile(path):
        return rules
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            extra = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 — 사용자 파일이 깨져도 기본 규칙으로 돈다
        return rules
    for k, v in extra.items():
        if k not in rules:
            continue
        # 목록은 덧붙이고, dict(받은 경로)는 키별 목록에 덧붙인다
        if isinstance(rules[k], dict) and isinstance(v, dict):
            for kk, vv in v.items():
                rules[k].setdefault(kk, []).extend(vv or [])
        elif isinstance(rules[k], list) and isinstance(v, list):
            rules[k].extend(v)
    return rules
