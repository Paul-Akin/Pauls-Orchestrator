# ADR 0005: Memory store and hybrid retrieval

* Status: accepted
* Date: 2026-11-23
* Implements: ADR 0002 (memory ownership)

## Context

Stage 2 gives the hub a memory: notes that are searched and injected into prompts
before each model call. Requirements: you own the data, you can edit it by hand,
it works offline, it runs on a Raspberry Pi 5 later, and private notes never
reach a cloud model.

## Decisions

### 1. Markdown files are the source of truth (`~/hub-memory`)

* One `.md` file per note, optional YAML front matter (`title`, `sensitivity`,
  `tags`, `created`, `source`). Readable and editable in any editor
  (Obsidian, VS Code), diffable, and trivially backed up or synced.
* **Id = path** relative to the memory dir without `.md`
  (`personal/2026-11-23-bolt-preload`). Hand-written files need no id field.
* Lives **outside the repo** (default `~/hub-memory`) so memory can never be
  committed by accident.
* New notes are written atomically (temp file + rename) into a folder named for
  their sensitivity.

### 2. Sensitivity fails closed

Effective sensitivity is the **most restrictive** of the front-matter value and
the top-level folder (`private/`, `personal/`, `public/`). A note with neither,
an unknown value, or unreadable front matter is **PRIVATE**, so a forgotten or
broken tag can only ever restrict who sees a note.

### 3. The index is derived: SQLite + FTS5 + sqlite-vec

`data/memory_index.sqlite3` holds notes, chunks, an FTS5 table (Porter stemming,
BM25 ranking) and a `vec0` table (cosine distance) with `sensitivity` as a
metadata column. One file, no server, wheels for Windows, Linux x86_64, Linux
aarch64 and macOS. Delete it and `hub memory reindex` rebuilds it.

* Incremental scan: unchanged (mtime, size) skips; touched-but-identical
  (sha256) skips; changed notes are re-chunked and re-embedded.
* Chunks follow markdown headings and are packed up to `chunk_max_chars`; each
  keeps its heading path so a hit has context.
* A change of embedding model or dimension rebuilds the vector table; a change
  of index schema rebuilds everything.
* **Sensitivity is filtered inside SQL** for both searches, so rows above the
  caller's cap are never loaded into Python.

### 4. Embeddings are local only

Embedding a note sends its full text to the model, so the embedder must pass the
same locality check as the private route: startup refuses a non-local Ollama
endpoint or a `*-cloud` model, and the embedder re-checks before every request.
Default model `nomic-embed-text` (pull it with `ollama pull nomic-embed-text`).
The dimension is read from the first response, not configured.

If Ollama is down, search degrades to **keyword-only** and unembedded chunks
are backfilled on the next reindex. Memory never fails a chat.

### 5. Hybrid ranking: Reciprocal Rank Fusion + recency

Reciprocal Rank Fusion (Cormack, Clarke and Büttcher, SIGIR 2009) combines the
BM25 and cosine rankings without normalizing their incomparable scores:

$$\text{RRF}(d)=\sum_{r\in\{\text{BM25},\cos\}}\frac{1}{k+\text{rank}_r(d)},\quad k=60$$

$$s(d)=\text{RRF}(d)\cdot\left[\beta+(1-\beta)\,2^{-\Delta t/T_{1/2}}\right]$$

* k = 60 is from the paper. β = 0.7 and T½ = 180 days are engineering
  judgment (old engineering notes must stay findable). All are configurable.
* Vector hits with cosine < τ (default 0.35) are dropped **before** fusion, so the
  nearest neighbour isn't injected when nothing is actually relevant.
* Injection is greedy by s(d) within `inject_max` chunks and `token_budget`
  approximate tokens (chars / 4).

### 6. Injection is conservative and visible

* The `MemoryContextBuilder` searches with `max_sensitivity_for(route ceiling)`:
  PRIVATE only on `local_only` routes. It uses the **route's** ceiling, not the
  answering model's, so a cloud-capable route never gets PRIVATE notes even if
  its local fallback answers. The cap is enforced in SQL, re-checked by the
  provider, and re-checked by the builder.
* Notes go in one system message: a preamble labelling them as reference data,
  then `<memory><note id=... title=... section=... created=...>` blocks, with
  any closing tags inside notes neutralized.
* Every response lists `memory_used` (id, title, section, sensitivity, score);
  the UI shows them as chips. `use_memory: false` disables lookup per request.

## Consequences and limits

* **Prompt injection is reduced, not eliminated.** A note containing
  instructions is labelled as data, but a model may still follow it. Only you
  write notes, which bounds the risk; stage 4 inputs (screen capture, phone
  events) must not be auto-written into memory without review.
* Keyword search uses an English stopword list and Porter stemming.
* The token budget uses a chars/4 approximation; actual usage varies by tokenizer.
* Automatic extraction of memories from chats is deliberately not implemented;
  all writes are explicit (`hub remember`, the UI, `POST /memory`, or editing files).
