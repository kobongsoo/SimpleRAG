//------------------------------------------------------------------
// e5 토크나이저 감싸기
//=> 청킹·BM25·토큰 길이 계산이 모두 같은 e5 tokenizer.json 을 쓴다(HF tokenizers — Python 판과
//   같은 구현이라 같은 토큰이 나온다). 길이 계산은 자주 반복되므로 작은 캐시를 둔다(_tlen).
//------------------------------------------------------------------

use std::collections::HashMap;
use std::sync::Mutex;

use tokenizers::Tokenizer;

//------------------------------------------------------------------
// 토크나이저 + 길이 캐시
// -필드: inner = HF 토크나이저(잘라내기 끔)
// -필드: cache = 글자 → 토큰 수
//------------------------------------------------------------------
pub struct Tok {
    pub inner: Tokenizer,
    cache: Mutex<HashMap<String, usize>>,
}

impl Tok {
    //------------------------------------------------------------------
    // tokenizer.json 읽기 — 잘라내기·채우기는 끈다(Python no_truncation)
    //------------------------------------------------------------------
    pub fn from_file(path: &str) -> Result<Tok, String> {
        let mut t = Tokenizer::from_file(path).map_err(|e| format!("tokenizer 로딩 실패: {}", e))?;
        let _ = t.with_truncation(None);
        t.with_padding(None);
        Ok(Tok { inner: t, cache: Mutex::new(HashMap::new()) })
    }

    // 토큰 id (특수 토큰 없이)
    pub fn ids(&self, text: &str) -> Vec<u32> {
        self.inner.encode(text, false).map(|e| e.get_ids().to_vec()).unwrap_or_default()
    }

    // 토큰 문자열 (BM25 용, 특수 토큰 없이)
    pub fn tokens(&self, text: &str) -> Vec<String> {
        self.inner.encode(text, false).map(|e| e.get_tokens().to_vec()).unwrap_or_default()
    }

    // id → 글자 (특수 토큰은 건너뜀 — Python tok.decode 기본값)
    pub fn decode(&self, ids: &[u32]) -> String {
        self.inner.decode(ids, true).unwrap_or_default()
    }

    //------------------------------------------------------------------
    // 토큰 수 (_tlen) — 캐시가 2만 개를 넘으면 비운다
    //------------------------------------------------------------------
    pub fn tlen(&self, s: &str) -> usize {
        if let Some(n) = self.cache.lock().unwrap().get(s) {
            return *n;
        }
        let n = self.ids(s).len();
        let mut c = self.cache.lock().unwrap();
        if c.len() > 20000 {
            c.clear();
        }
        c.insert(s.to_string(), n);
        n
    }
}
