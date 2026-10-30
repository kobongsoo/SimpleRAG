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

    //------------------------------------------------------------------
    // 청크 하나의 단어별 빈도 (BM25 용)
    //=> BM25 는 "단어가 몇 번 나왔나" 만 쓴다. 단어 번호를 첫 등장 순서로 매기므로 순서도 지킨다.
    //    1) 토큰을 차례로 보며 처음 나온 단어는 끝에 붙이고, 이미 있으면 빈도만 올린다
    //
    // -in: text = 청크 본문
    //
    // -out: [(단어, 빈도)] — 첫 등장 순서
    // -out: error = 없음 (토큰화 실패는 빈 목록)
    //------------------------------------------------------------------
    pub fn term_freqs(&self, text: &str) -> Vec<(String, u32)> {
        let mut out: Vec<(String, u32)> = vec![];
        let mut pos: HashMap<String, usize> = HashMap::new();
        for t in self.tokens(text) {
            match pos.get(&t) {
                Some(&i) => out[i].1 += 1,
                None => {
                    pos.insert(t.clone(), out.len());
                    out.push((t, 1));
                }
            }
        }
        out
    }

    //------------------------------------------------------------------
    // 여러 청크의 단어별 빈도 — 스레드 여러 개로 나눠서 (BM25 단계 A)
    //=> BM25 구축 시간의 약 60% 가 토큰화다. 청크를 스레드 수만큼 잘라 동시에 토큰화하고,
    //   결과는 입력 순서대로 이어 붙인다 — 그래서 한 줄씩 할 때와 결과가 똑같다.
    //
    // -in: texts   = 청크 본문 목록
    // -in: threads = 스레드 수(1 이면 한 줄씩)
    //
    // -out: 청크마다 [(단어, 빈도)] — texts 순서
    // -out: error = 없음
    //------------------------------------------------------------------
    pub fn term_freqs_par(&self, texts: &[&str], threads: usize) -> Vec<Vec<(String, u32)>> {
        let threads = threads.max(1);
        if threads == 1 || texts.len() < 64 {
            return texts.iter().map(|t| self.term_freqs(t)).collect();
        }
        let per = texts.len().div_ceil(threads);
        std::thread::scope(|s| {
            let hs: Vec<_> = texts.chunks(per).map(|part| s.spawn(move || part.iter().map(|t| self.term_freqs(t)).collect::<Vec<_>>())).collect();
            // 나눈 차례대로 모아 입력 순서를 지킨다
            hs.into_iter().flat_map(|h| h.join().expect("토큰화 스레드 실패")).collect()
        })
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
