//------------------------------------------------------------------
// 작은 pickle 해석기 (Python 판 인덱스 가져오기용)
//=> Qdrant local 은 점마다 PointStruct 를 pickle(프로토콜 4)로 sqlite 에 넣는다. 그 바이트를
//   읽어 id·vector·payload 를 꺼낼 만큼만 pickle 가상 기계를 흉내 낸다.
//   클래스 객체(STACK_GLOBAL + NEWOBJ + BUILD)는 BUILD 로 받은 상태 사전으로 대신한다.
//------------------------------------------------------------------

use serde_json::{json, Map, Value};

// 스택 위의 값
#[derive(Debug, Clone)]
enum P {
    V(Value),
    Mark,
    Global,
    Obj(Box<Value>),
}

fn to_value(p: P) -> Value {
    match p {
        P::V(v) => v,
        P::Obj(v) => *v,
        P::Mark | P::Global => Value::Null,
    }
}

// 사전 열쇠로 쓸 글자
fn key_of(p: &P) -> String {
    match p {
        P::V(Value::String(s)) => s.clone(),
        P::V(v) => v.to_string(),
        _ => String::new(),
    }
}

//------------------------------------------------------------------
// pickle 바이트 → JSON 값
//
// -in: b = pickle 바이트
//
// -out: Ok(값) — 객체는 BUILD 상태(예: {"__dict__": {...}, ...})
// -out: error = 모르는 연산자·잘린 데이터면 Err
//------------------------------------------------------------------
pub fn loads(b: &[u8]) -> Result<Value, String> {
    let mut st: Vec<P> = vec![];
    let mut memo: Vec<P> = vec![];
    let mut i = 0usize;
    let rd = |i: &mut usize, n: usize| -> Result<&[u8], String> {
        let s = b.get(*i..*i + n).ok_or("pickle 잘림")?;
        *i += n;
        Ok(s)
    };
    // MARK 까지 꺼내기
    fn pop_mark(st: &mut Vec<P>) -> Vec<P> {
        let pos = st.iter().rposition(|p| matches!(p, P::Mark)).unwrap_or(0);
        let items = st.split_off(pos + 1);
        st.pop();
        items
    }
    loop {
        let op = *b.get(i).ok_or("pickle 끝 없음")?;
        i += 1;
        match op {
            0x80 => { rd(&mut i, 1)?; }                            // PROTO
            0x95 => { rd(&mut i, 8)?; }                            // FRAME
            b'.' => break,                                          // STOP
            b'(' => st.push(P::Mark),                               // MARK
            b'N' => st.push(P::V(Value::Null)),
            0x88 => st.push(P::V(json!(true))),
            0x89 => st.push(P::V(json!(false))),
            b'J' => { let s = rd(&mut i, 4)?; st.push(P::V(json!(i32::from_le_bytes(s.try_into().unwrap())))); }
            b'K' => { let s = rd(&mut i, 1)?; st.push(P::V(json!(s[0]))); }
            b'M' => { let s = rd(&mut i, 2)?; st.push(P::V(json!(u16::from_le_bytes(s.try_into().unwrap())))); }
            0x8a => {                                                // LONG1
                let n = rd(&mut i, 1)?[0] as usize;
                let s = rd(&mut i, n)?;
                let mut v: i64 = 0;
                for (k, byte) in s.iter().enumerate().take(8) {
                    v |= (*byte as i64) << (8 * k);
                }
                if n > 0 && n < 8 && s[n - 1] & 0x80 != 0 {
                    v -= 1i64 << (8 * n);
                }
                st.push(P::V(json!(v)));
            }
            b'G' => { let s = rd(&mut i, 8)?; st.push(P::V(json!(f64::from_be_bytes(s.try_into().unwrap())))); }
            0x8c => { let n = rd(&mut i, 1)?[0] as usize; let s = rd(&mut i, n)?; st.push(P::V(json!(String::from_utf8_lossy(s)))); }
            b'X' => { let n = u32::from_le_bytes(rd(&mut i, 4)?.try_into().unwrap()) as usize; let s = rd(&mut i, n)?; st.push(P::V(json!(String::from_utf8_lossy(s)))); }
            0x8d => { let n = u64::from_le_bytes(rd(&mut i, 8)?.try_into().unwrap()) as usize; let s = rd(&mut i, n)?; st.push(P::V(json!(String::from_utf8_lossy(s)))); }
            b'C' => { let n = rd(&mut i, 1)?[0] as usize; rd(&mut i, n)?; st.push(P::V(Value::Null)); } // SHORT_BINBYTES
            b'B' => { let n = u32::from_le_bytes(rd(&mut i, 4)?.try_into().unwrap()) as usize; rd(&mut i, n)?; st.push(P::V(Value::Null)); }
            0x94 => { if let Some(t) = st.last() { memo.push(t.clone()); } }                          // MEMOIZE
            b'q' => { let k = rd(&mut i, 1)?[0] as usize; if let Some(t) = st.last() { if memo.len() <= k { memo.resize(k + 1, P::V(Value::Null)); } memo[k] = t.clone(); } }
            b'r' => { let k = u32::from_le_bytes(rd(&mut i, 4)?.try_into().unwrap()) as usize; if let Some(t) = st.last() { if memo.len() <= k { memo.resize(k + 1, P::V(Value::Null)); } memo[k] = t.clone(); } }
            b'h' => { let k = rd(&mut i, 1)?[0] as usize; st.push(memo.get(k).cloned().ok_or("memo 없음")?); }
            b'j' => { let k = u32::from_le_bytes(rd(&mut i, 4)?.try_into().unwrap()) as usize; st.push(memo.get(k).cloned().ok_or("memo 없음")?); }
            b')' => st.push(P::V(json!([]))),                       // EMPTY_TUPLE
            b']' => st.push(P::V(json!([]))),                       // EMPTY_LIST
            b'}' => st.push(P::V(json!({}))),                       // EMPTY_DICT
            0x8f => st.push(P::V(json!([]))),                       // EMPTY_SET
            0x85 | 0x86 | 0x87 => {                                  // TUPLE1/2/3
                let n = (op - 0x84) as usize;
                let items = st.split_off(st.len().saturating_sub(n));
                st.push(P::V(Value::Array(items.into_iter().map(to_value).collect())));
            }
            b't' => { let items = pop_mark(&mut st); st.push(P::V(Value::Array(items.into_iter().map(to_value).collect()))); }
            b'l' => { let items = pop_mark(&mut st); st.push(P::V(Value::Array(items.into_iter().map(to_value).collect()))); }
            b'e' | 0x90 => {                                         // APPENDS / ADDITEMS
                let items = pop_mark(&mut st);
                if let Some(P::V(Value::Array(a))) = st.last_mut() {
                    a.extend(items.into_iter().map(to_value));
                }
            }
            b'a' => {                                                // APPEND
                let x = st.pop().map(to_value).unwrap_or(Value::Null);
                if let Some(P::V(Value::Array(a))) = st.last_mut() {
                    a.push(x);
                }
            }
            b'u' => {                                                // SETITEMS
                let items = pop_mark(&mut st);
                let mut it = items.into_iter();
                let mut pairs = vec![];
                while let (Some(k), Some(v)) = (it.next(), it.next()) {
                    pairs.push((key_of(&k), to_value(v)));
                }
                if let Some(P::V(Value::Object(m))) = st.last_mut() {
                    for (k, v) in pairs {
                        m.insert(k, v);
                    }
                }
            }
            b's' => {                                                // SETITEM
                let v = st.pop().map(to_value).unwrap_or(Value::Null);
                let k = st.pop().map(|p| key_of(&p)).unwrap_or_default();
                if let Some(P::V(Value::Object(m))) = st.last_mut() {
                    m.insert(k, v);
                }
            }
            0x93 => { st.pop(); st.pop(); st.push(P::Global); }     // STACK_GLOBAL
            b'c' => {                                                // GLOBAL (두 줄)
                for _ in 0..2 {
                    while *b.get(i).ok_or("pickle 잘림")? != b'\n' {
                        i += 1;
                    }
                    i += 1;
                }
                st.push(P::Global);
            }
            0x81 => { st.pop(); st.pop(); st.push(P::Obj(Box::new(json!({})))); } // NEWOBJ
            b'R' => { st.pop(); st.pop(); st.push(P::Obj(Box::new(json!({})))); } // REDUCE
            b'b' => {                                                // BUILD
                let state = st.pop().map(to_value).unwrap_or(Value::Null);
                if let Some(top) = st.last_mut() {
                    *top = P::Obj(Box::new(state));
                }
            }
            0x92 => { st.pop(); st.pop(); st.pop(); st.push(P::Obj(Box::new(json!({})))); } // NEWOBJ_EX
            0x91 => { let items = pop_mark(&mut st); st.push(P::V(Value::Array(items.into_iter().map(to_value).collect()))); } // FROZENSET
            0x8e => { let n = u64::from_le_bytes(rd(&mut i, 8)?.try_into().unwrap()) as usize; rd(&mut i, n)?; st.push(P::V(Value::Null)); }
            b'0' => { st.pop(); }                                    // POP
            b'1' => { pop_mark(&mut st); }                           // POP_MARK
            b'2' => { if let Some(t) = st.last().cloned() { st.push(t); } } // DUP
            other => return Err(format!("모르는 pickle 연산자 0x{:02x}", other)),
        }
    }
    Ok(st.pop().map(to_value).unwrap_or(Value::Null))
}

//------------------------------------------------------------------
// PointStruct pickle → (id, 벡터, payload)
//------------------------------------------------------------------
pub fn point_of(b: &[u8]) -> Result<(i64, Vec<f32>, Map<String, Value>), String> {
    let v = loads(b)?;
    let d = v.get("__dict__").cloned().unwrap_or(v);
    let id = d.get("id").and_then(|x| x.as_i64()).ok_or("id 없음")?;
    let vec: Vec<f32> = d.get("vector").and_then(|x| x.as_array()).ok_or("vector 없음")?
        .iter().map(|x| x.as_f64().unwrap_or(0.0) as f32).collect();
    let payload = d.get("payload").and_then(|x| x.as_object()).cloned().unwrap_or_default();
    Ok((id, vec, payload))
}
