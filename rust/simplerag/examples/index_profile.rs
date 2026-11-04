//------------------------------------------------------------------
// 인덱싱 단계별 시간 측정 (진단용 예제)
//=> sync_doc 과 같은 함수로 문서마다 ①파싱 ②청킹 ③임베딩 ④메타추출 ⑤DB 적재를
//   따로 재서 "1쪽당 몇 ms" 로 보여 준다. 실제 인덱스를 건드리지 않도록
//   SIMPLERAG_HOME 을 임시 폴더로 두고 돌린다.
//    1) 폴더의 문서를 모두 모은다
//    2) 문서마다 다섯 단계를 차례로 재고 쪽 수와 청크 수를 적는다
//    3) 합계 · 1쪽당 · 1청크당 시간을 표로 찍고, 문서별 줄은 JSONL 로 남긴다
//
// 실행: cargo run --release --example index_profile -- <문서 폴더> [결과.jsonl]
//------------------------------------------------------------------

use std::time::Instant;

use serde_json::{json, Map};
use simplerag::{chunk, docmodel, embed::Embedder, indexer, meta, store::VectorStore};

fn ms(t: Instant) -> f64 {
    t.elapsed().as_secs_f64() * 1000.0
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let dir = args.get(1).expect("문서 폴더를 주세요");
    let out = args.get(2).cloned();

    let (files, _) = indexer::list_files(dir, true).expect("폴더 읽기 실패");
    let emb = Embedder::new();
    let t = Instant::now();
    emb.ensure_loaded().expect("임베더 적재 실패");
    eprintln!("임베더 적재 {:.0} ms", ms(t));
    let tok = emb.tokenizer().unwrap();

    let home = std::env::var("SIMPLERAG_HOME").expect("SIMPLERAG_HOME 을 임시 폴더로 지정하세요");
    let store = VectorStore::new(Some(format!("{}\\rag_index", home)));
    store.ensure_loaded().expect("저장소 열기 실패");

    // 합계: 파싱 · 청킹 · 임베딩 · 메타 · DB
    let mut tot = [0f64; 5];
    let (mut pages, mut chunks_n, mut toks_n) = (0i64, 0usize, 0usize);
    let mut lines = vec![];
    let mut next_id = 0i64;
    for (i, path) in files.iter().enumerate() {
        let name = std::path::Path::new(path).file_name().unwrap().to_string_lossy().to_string();
        let title = std::path::Path::new(&name).file_stem().unwrap().to_string_lossy().to_string();
        // ① 파싱
        let t = Instant::now();
        let mut doc = match docmodel::extract_document(path, Some(&title)) {
            Ok(d) => d,
            Err(e) => {
                eprintln!("  [실패] {} — {}", name, e);
                continue;
            }
        };
        let t_parse = ms(t);
        // ② 청킹
        let t = Instant::now();
        let metas = chunk::split_blocks(&mut doc, &tok, None);
        let t_chunk = ms(t);
        let texts: Vec<String> = metas.iter().map(|m| m.text.clone()).collect();
        let ntok: usize = texts.iter().map(|c| tok.inner.encode(format!("passage: {}", c), true).map(|e| e.get_ids().len()).unwrap_or(0)).sum();
        // ③ 임베딩
        let t = Instant::now();
        let vecs = emb.embed_passages(&texts).expect("임베딩 실패");
        let t_embed = ms(t);
        // ④ 메타데이터 추출(meta.sqlite)
        let t = Instant::now();
        let chash = indexer::content_hash(path).unwrap_or_default();
        meta::hook::synced(&simplerag::pyutil::abspath(path), Some(&chash));
        let t_meta = ms(t);
        // ⑤ DB 적재(벡터 + payload 넣기, 옛 청크 지우기)
        let t = Instant::now();
        let ids: Vec<i64> = (next_id..next_id + texts.len() as i64).collect();
        next_id += texts.len() as i64;
        let payloads: Vec<Map<String, serde_json::Value>> = metas.iter().enumerate().map(|(k, m)| {
            let mut p = Map::new();
            p.insert("text".into(), json!(m.text));
            p.insert("doc_path".into(), json!(path));
            p.insert("doc_name".into(), json!(name));
            p.insert("chunk_idx".into(), json!(k));
            p
        }).collect();
        store.upsert(&ids, &vecs, &payloads).expect("upsert 실패");
        store.delete_doc_except(path, &ids).expect("delete 실패");
        let t_db = ms(t);

        let pg = doc.pages.unwrap_or(1).max(1);
        let row = [t_parse, t_chunk, t_embed, t_meta, t_db];
        for k in 0..5 {
            tot[k] += row[k];
        }
        pages += pg;
        chunks_n += texts.len();
        toks_n += ntok;
        eprintln!("[{}/{}] {} — {}쪽 {}청크 | 파싱 {:.0} 청킹 {:.0} 임베딩 {:.0} 메타 {:.0} DB {:.0} ms",
            i + 1, files.len(), name, pg, texts.len(), t_parse, t_chunk, t_embed, t_meta, t_db);
        lines.push(json!({"name": name, "format": doc.format, "pages": pg, "chunks": texts.len(), "tokens": ntok,
            "parse_ms": t_parse, "chunk_ms": t_chunk, "embed_ms": t_embed, "meta_ms": t_meta, "db_ms": t_db}).to_string());
    }

    let all: f64 = tot.iter().sum();
    println!("\n문서 {}건 · {}쪽 · 청크 {} · 토큰 {} · 합계 {:.1} 초", lines.len(), pages, chunks_n, toks_n, all / 1000.0);
    println!("{:<8} {:>10} {:>10} {:>10} {:>7}", "단계", "합계(초)", "쪽당(ms)", "청크당(ms)", "비중");
    for (k, nm) in ["파싱", "청킹", "임베딩", "메타", "DB"].iter().enumerate() {
        println!("{:<8} {:>10.2} {:>10.1} {:>10.2} {:>6.1}%", nm, tot[k] / 1000.0, tot[k] / pages as f64,
            tot[k] / chunks_n.max(1) as f64, tot[k] / all * 100.0);
    }
    if let Some(o) = out {
        std::fs::write(&o, lines.join("\n") + "\n").expect("결과 저장 실패");
    }
}
