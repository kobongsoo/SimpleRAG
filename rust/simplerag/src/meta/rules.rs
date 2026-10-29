//------------------------------------------------------------------
// 메타데이터 규칙 사전 (메타데이터 검색 설계서 §7-1 · §10-2 · §10-3 · §10-4 · §12-2, Python meta/rules.py)
//=> 정규화에 쓰는 낱말 목록을 한곳에 모은다. 설치처마다 다른 값(회사 메일 · 그룹웨어 도메인,
//   스캐너 모델 등)은 데이터 폴더의 meta_rules.yaml 로 덧붙일 수 있다(코드를 고치지 않고).
//   yaml 의 같은 키는 기본 목록에 더해진다(덮어쓰지 않는다).
//   정규식 목록은 읽을 때 한 번만 컴파일해 둔다(문서마다 다시 만들지 않게).
//------------------------------------------------------------------

use fancy_regex::Regex as FRegex;

// ── 사람 (§7-1) ──────────────────────────────────────────
// 대소문자 무시 완전 일치면 사람이 아니다(PC · 계정 기본값 · 회사 · OS 이름)
pub const JUNK_PEOPLE: &[&str] = &[
    "user", "pc", "home", "admin", "administrator", "owner", "windows xp", "windows user",
    "windows 사용자", "microsoft 계정", "microsoft corporation", "microsoft office user",
    "ibm_user", "winxp", "사용자", "사용자1", "user1", "guest", "test", "unknown",
    "microsoft", "hp", "dell", "samsung", "lg", "ibm", "lenovo", "mac", "pptxgenjs",
    "your user name", "osd user", "내부pc", "python-docx", "python-pptx", "openpyxl", "reportlab",
];
// 정규식(대소문자 무시)으로 걸리면 사람이 아니다 — 회사 · 기관 · 장비 · 프로그램 이름
pub const JUNK_PEOPLE_PATTERNS: &[&str] = &[
    r"\b(inc|corp|corporation|co|ltd|llc|gmbh|company|technologies|technology|foundation|institute|university|computer|computers|customer|systems?|software|solutions?|group|team|tft|lab|labs)\b\.?",
    r"(^|[\s\-_])pc\d*$", r"^pc\d+$", r"\buser\d*$", r"^(주|\(주\)|㈜)", r"(주식회사|재단|협회|연구원|대학교)$",
    r"^(asus|acer|samsung|lg|hp|dell|lenovo)$", r"^win(7|8|10|11|xp)$", r"(desktop|laptop|notebook)$",
    r"\bnetwork\b",
];
// 한글 낱말이 이것으로 끝나면 소속(부서)이다 — "이경준 매니저 정보화전략팀" 에서 떼어 낸다
pub const ORG_SUFFIX: &[&str] = &["팀", "부", "실", "그룹", "본부", "센터", "랩스", "사업부", "연구소"];
// 이름 앞뒤에 붙는 직함(떼어 낸다)
pub const TITLES: &[&str] = &[
    "대표이사", "대표", "사장", "부사장", "전무", "상무", "이사", "본부장", "실장", "부장", "차장",
    "과장", "대리", "주임", "사원", "팀장", "파트장", "책임", "선임", "수석", "연구원", "매니저",
    "프로", "님",
];

// ── 원본 프로그램 (§10-2) — (값, Creator · Producer · Application 에 들어 있는 말) 앞에서부터 ──
pub const ORIGIN_APPS: &[(&str, &[&str])] = &[
    ("scanner", &["apeosport", "sindoh", "samsung-scx", "samsung scx", "sl-c", "canon ir", "ricoh",
                  "konica", "docucentre", "xerox", "scanner", "scansnap"]),
    ("hwp", &["hancom pdf", "hwp", "한글", "hancom"]),
    ("word", &["microsoft word", "word"]),
    ("excel", &["microsoft excel", "excel"]),
    ("ppt", &["microsoft powerpoint", "powerpoint"]),
    ("print", &["print to pdf", "pscript", "distiller", "ghostscript", "pdf printer", "pdfcreator",
                "cutepdf", "bullzip"]),
    ("web", &["chrome", "skia", "mozilla", "edge", "wkhtmltopdf"]),
];

// ── 받은 경로 (§10-3) — 도메인 끝부분 일치 ──
pub const RECV_DOMAINS: &[(&str, &[&str])] = &[
    ("mail", &["ezwebmail.bizmeka.com", "outlook.office.com", "outlook.live.com",
               "mail-attachment.googleusercontent.com", "mail.naver.com", "mail.daum.net"]),
    ("drive", &["drive.usercontent.google.com", "drive.google.com", "zdl-storage.worksmobile.com",
                "onedrive.live.com", "sharepoint.com", "dropbox.com", "dropboxusercontent.com"]),
    ("intranet", &["mpower.mocomsys.com"]),
    ("ai", &["claude.ai", "chatgpt.com", "chat.openai.com", "gemini.google.com"]),
];
// 이 접두로 시작하는 호스트도 사내로 본다(그룹웨어)
pub const INTRANET_PREFIX: &[&str] = &["gw.", "groupware.", "intranet."];

// ── 문서 유형 · 양식 (§10-4) ──
pub const DOC_TYPES: &[&str] = &[
    "절차서", "지침", "규정", "매뉴얼", "메뉴얼", "제안서", "계약서", "보고서", "회의록", "확인서",
    "계획서", "정의서", "명세서", "설계서", "견적서", "요청서", "결과서", "기안서", "품의서",
    "가이드", "교육자료", "사용자설명서", "점검표",
];
// 유형어가 아니지만 그 유형을 가리키는 말(7단계 · 시험 ③) — (낱말, 유형)
pub const TYPE_ALIASES: &[(&str, &str)] = &[
    ("제도", "규정"), ("취업규칙", "규정"), ("정관", "규정"), ("해설", "가이드"), ("안내문", "가이드"),
    ("설치가이드", "가이드"), ("증명서", "확인서"), ("증서", "확인서"), ("주의사항", "지침"), ("관리계획", "계획서"),
];
pub const FORM_WORDS: &[&str] = &["양식", "서식", "신청서", "의뢰서", "일지", "별지", "템플릿", "template"];
pub const FORM_FOLDER_WORDS: &[&str] = &["템플릿", "양식", "서식"];
pub const FORM_SHEETS: &[&str] = &["표지", "작성방법", "작성 방법", "입력"];
pub const TEMPLATE_EXTS: &[&str] = &["dotx", "dotm", "dot", "xltx", "xltm", "xlt", "potx", "potm", "pot", "hwt"];

// 이 이름(대소문자 무시, 확장자 무시)의 템플릿은 "양식 정보 없음"으로 본다
pub const PLAIN_TEMPLATES: &[&str] = &["normal", "normal.dot", "normal.dotm", "normal.dotx", "blank", "빈 프레젠테이션",
                                       "office theme", "office 테마"];

// ── 제목 쓰레기 (§9-1) — 정규식 ──
pub const TITLE_JUNK: &[&str] = &[
    r"^슬라이드\s*\d+$", r"^slide\s*\d+$", r"^powerpoint\s*프레젠테이션$", r"^powerpoint presentation$",
    r"^pptxgenjs presentation$", r"^제목\s*없음$", r"^untitled$", r"^문서\s*\d*$", r"^document\s*\d*$",
    r"^\d+$", r"\.(dvi|doc|docx|hwp|hwpx|xls|xlsx|ppt|pptx|pdf|txt|tex)$", r"^presentation\d*$",
];

//------------------------------------------------------------------
// 규칙 묶음 (Python load_rules() 의 dict)
// -필드: junk_people · junk_people_patterns · org_suffix · titles  = 사람 정리용 목록
// -필드: origin_apps  = [(값, 단서 낱말들)] 앞에서부터 / recv_domains = [(종류, 도메인들)] 순서 유지
// -필드: doc_types · form_* · template_exts · plain_templates · title_junk = 유형 · 양식 · 제목 판정
// -필드: type_aliases = [(낱말, 유형)] — 제목에 낱말이 있으면 그 유형 +3 (7단계)
// -필드: re_*         = 위 정규식 목록을 컴파일해 둔 것(잘못된 사용자 정규식은 건너뜀)
//------------------------------------------------------------------
#[derive(Debug, Clone)]
pub struct Rules {
    pub junk_people: Vec<String>,
    pub junk_people_patterns: Vec<String>,
    pub org_suffix: Vec<String>,
    pub titles: Vec<String>,
    pub origin_apps: Vec<(String, Vec<String>)>,
    pub recv_domains: Vec<(String, Vec<String>)>,
    pub intranet_prefix: Vec<String>,
    pub doc_types: Vec<String>,
    pub form_words: Vec<String>,
    pub form_folder_words: Vec<String>,
    pub form_sheets: Vec<String>,
    pub template_exts: Vec<String>,
    pub plain_templates: Vec<String>,
    pub title_junk: Vec<String>,
    pub type_aliases: Vec<(String, String)>,
    pub(crate) re_junk_people: Vec<FRegex>,
    pub(crate) re_title_junk: Vec<FRegex>,
    // 직함 떼기 — 긴 직함부터 (뒤에 붙은 것 · 앞에 붙은 것)
    pub(crate) re_titles: Vec<(FRegex, FRegex)>,
}

// &[&str] → Vec<String>
fn v(xs: &[&str]) -> Vec<String> {
    xs.iter().map(|s| s.to_string()).collect()
}

// yaml 값 → 글자 목록(목록이 아니면 None)
fn yaml_strs(y: &serde_yaml::Value) -> Option<Vec<String>> {
    y.as_sequence().map(|s| s.iter().map(yaml_str).collect())
}

//------------------------------------------------------------------
// yaml 스칼라 → 파이썬 str() 모양 글자
//
// -in: y = yaml 값
//
// -out: String (null 은 "None", 참거짓은 "True"/"False")
// -out: error = 없음
//------------------------------------------------------------------
pub fn yaml_str(y: &serde_yaml::Value) -> String {
    match y {
        serde_yaml::Value::Null => "None".into(),
        serde_yaml::Value::Bool(b) => if *b { "True".into() } else { "False".into() },
        serde_yaml::Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                i.to_string()
            } else if let Some(u) = n.as_u64() {
                u.to_string()
            } else {
                crate::pyutil::float_repr(n.as_f64().unwrap_or(0.0))
            }
        }
        serde_yaml::Value::String(s) => s.clone(),
        other => serde_yaml::to_string(other).unwrap_or_default().trim_end().to_string(),
    }
}

impl Rules {
    //------------------------------------------------------------------
    // 기본 규칙
    //------------------------------------------------------------------
    pub fn defaults() -> Rules {
        let mut r = Rules {
            junk_people: v(JUNK_PEOPLE),
            junk_people_patterns: v(JUNK_PEOPLE_PATTERNS),
            org_suffix: v(ORG_SUFFIX),
            titles: v(TITLES),
            origin_apps: ORIGIN_APPS.iter().map(|(a, w)| (a.to_string(), v(w))).collect(),
            recv_domains: RECV_DOMAINS.iter().map(|(a, w)| (a.to_string(), v(w))).collect(),
            intranet_prefix: v(INTRANET_PREFIX),
            doc_types: v(DOC_TYPES),
            form_words: v(FORM_WORDS),
            form_folder_words: v(FORM_FOLDER_WORDS),
            form_sheets: v(FORM_SHEETS),
            template_exts: v(TEMPLATE_EXTS),
            plain_templates: v(PLAIN_TEMPLATES),
            title_junk: v(TITLE_JUNK),
            type_aliases: TYPE_ALIASES.iter().map(|(w, t)| (w.to_string(), t.to_string())).collect(),
            re_junk_people: vec![],
            re_title_junk: vec![],
            re_titles: vec![],
        };
        r.compile();
        r
    }

    //------------------------------------------------------------------
    // 정규식 목록 컴파일 — 목록을 바꾼 뒤 부른다
    //=> 사람 쓰레기 패턴은 소문자 값에 그대로(re.search), 제목 쓰레기는 대소문자 무시(re.I).
    //   직함은 Python 처럼 길이 내림차순(같은 길이는 원래 순서)으로 둔다.
    //
    // -in: 없음
    //
    // -out: 없음
    // -out: error = 없음 (잘못된 정규식은 건너뛴다)
    //------------------------------------------------------------------
    pub fn compile(&mut self) {
        self.re_junk_people = self.junk_people_patterns.iter().filter_map(|p| FRegex::new(p).ok()).collect();
        self.re_title_junk = self.title_junk.iter().filter_map(|p| FRegex::new(&format!("(?i){}", p)).ok()).collect();
        let mut titles = self.titles.clone();
        // 긴 직함부터 — "대표이사" 를 "대표" 보다 먼저 뗀다
        titles.sort_by(|a, b| b.chars().count().cmp(&a.chars().count()));
        self.re_titles = titles.iter().filter_map(|t| {
            let e = regex::escape(t);
            let tail = FRegex::new(&format!(r"(?<=[가-힣])\s*{}$", e)).ok()?;
            let head = FRegex::new(&format!(r"^{}\s+", e)).ok()?;
            Some((tail, head))
        }).collect();
    }
}

//------------------------------------------------------------------
// 규칙 읽기 — 기본값 + 데이터 폴더 meta_rules.yaml
//=> yaml 이 있으면 같은 이름의 목록에 덧붙인다(dict 는 키별로 목록을 덧붙임).
//    1) 기본값을 복사한다
//    2) <root>/meta_rules.yaml 을 읽어 덧붙인다(없거나 깨졌으면 기본값만)
//
// -in: root = 데이터 폴더("" 이면 기본값만)
//
// -out: Rules
// -out: error = 없음 (yaml 이 깨졌으면 무시하고 기본값)
//------------------------------------------------------------------
pub fn load_rules(root: &str) -> Rules {
    let mut rules = Rules::defaults();
    if root.is_empty() {
        return rules;
    }
    let path = crate::pyutil::join(root, "meta_rules.yaml");
    if !std::path::Path::new(&path).is_file() {
        return rules;
    }
    let extra: serde_yaml::Value = match std::fs::read_to_string(&path).ok().and_then(|s| serde_yaml::from_str(&s).ok()) {
        Some(y) => y,
        None => return rules,
    };
    let map = match extra.as_mapping() {
        Some(m) => m.clone(),
        None => return rules,
    };
    for (k, val) in map.iter() {
        let key = yaml_str(k);
        // 목록은 덧붙이고, dict(받은 경로)는 키별 목록에 덧붙인다
        let list: Option<&mut Vec<String>> = match key.as_str() {
            "junk_people" => Some(&mut rules.junk_people),
            "junk_people_patterns" => Some(&mut rules.junk_people_patterns),
            "org_suffix" => Some(&mut rules.org_suffix),
            "titles" => Some(&mut rules.titles),
            "intranet_prefix" => Some(&mut rules.intranet_prefix),
            "doc_types" => Some(&mut rules.doc_types),
            "form_words" => Some(&mut rules.form_words),
            "form_folder_words" => Some(&mut rules.form_folder_words),
            "form_sheets" => Some(&mut rules.form_sheets),
            "template_exts" => Some(&mut rules.template_exts),
            "plain_templates" => Some(&mut rules.plain_templates),
            "title_junk" => Some(&mut rules.title_junk),
            _ => None,
        };
        if let Some(l) = list {
            if let Some(xs) = yaml_strs(val) {
                l.extend(xs);
            }
            continue;
        }
        if key == "origin_apps" {
            // [[값, [단서…]], …] 모양만 받는다
            if let Some(seq) = val.as_sequence() {
                for item in seq {
                    if let Some(pair) = item.as_sequence() {
                        if pair.len() == 2 {
                            let words = yaml_strs(&pair[1]).unwrap_or_default();
                            rules.origin_apps.push((yaml_str(&pair[0]), words));
                        }
                    }
                }
            }
        } else if key == "type_aliases" {
            // [[낱말, 유형], …] 모양만 받는다(파이썬은 두 칸짜리로 풀어 쓴다)
            if let Some(seq) = val.as_sequence() {
                for item in seq {
                    if let Some(pair) = item.as_sequence() {
                        if pair.len() == 2 {
                            rules.type_aliases.push((yaml_str(&pair[0]), yaml_str(&pair[1])));
                        }
                    }
                }
            }
        } else if key == "recv_domains" {
            if let Some(m) = val.as_mapping() {
                for (kk, vv) in m.iter() {
                    let kk = yaml_str(kk);
                    let add = yaml_strs(vv).unwrap_or_default();
                    match rules.recv_domains.iter_mut().find(|(n, _)| *n == kk) {
                        Some((_, l)) => l.extend(add),
                        None => rules.recv_domains.push((kk, add)),
                    }
                }
            }
        }
    }
    rules.compile();
    rules
}
