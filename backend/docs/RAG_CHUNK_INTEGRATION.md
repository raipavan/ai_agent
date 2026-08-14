# RAG Chunk Integration

The knowledge base moved from "one giant string appended to the system prompt" to a
chunked, keyword-retrieved store. No embeddings, no new dependencies — SQLite FTS5
(`backend/rag.py`, its own `data/rag.db`; intentionally separate from the Postgres
migration).

## How chunks are built

`rag._chunk_text(text, chunk_chars=900, overlap_chars=180)`:

1. Split the text on markdown heading lines (lines starting with `#`). Each heading
   stays attached to its following content = one section.
2. Sections <= 900 chars become a single chunk verbatim.
3. Sections > 900 chars are split into 900-char chunks with 180-char overlap; every
   sub-chunk is prefixed with the section's heading line(s) so retrieved fragments
   keep their heading context.

The RAG body lives verbatim in `prompts/rag/*.md` (split by its `# N.` headings,
`00_header.md` .. `10_welcome_coupon.md`). `prompts/priya.py` concatenates those files
(sorted, UTF-8) at import to rebuild `_MARUTI_RAG`, so
`get_role_rag_source_text()` / the `role_state.rag` DB column stay fully compatible.

## Indexing (role = `source` column)

- `rag.build_role_index(role, text)` — clears that role's chunks, re-indexes.
- `rag.query_role(role, query, top_k, max_chars)` — retrieval restricted to one role.
- `rag.index_role_rag(role, text)` / `rag.rag_context_for_role(role, query)` /
  `rag.get_rag_store()` — module-level conveniences; config is imported lazily.

## Where the index is refreshed

1. **Startup** — `core/role_sandbox.sync_role_sandbox_on_startup()` calls
   `index_role_rag(role, rag_out)` right after `save_role_state()`.
2. **Console KB save** — `POST /api/tuning` (`api/routes/console_api.py`) after
   `set_role_rag_source_text()`.
3. **Document upload/append** — `POST /api/tuning/upload-doc` after appending text.
4. **5-min hot reload** — worker `_scheduler_loop` block 2c refreshes the chunk
   index for `maruti`, `sales_1`, `sales_2` from `get_state(role)["rag"]`.

All four call sites are wrapped in try/except so indexing can never crash startup,
the API, or the scheduler.

## Call-time integration (services/ package — NOT in this checkout)

`services/vobiz_bridge` builds the system instruction by appending the full
`state['rag']` text. Replace that append with:

```python
from core.state import rag_context_for_role

ctx = rag_context_for_role(role, <recent user transcript or topic keywords>)
if ctx:
    system_prompt += "\n\n" + ctx
```

`rag_context_for_role` returns `format_references(...)` of the top-k chunks
(`settings.rag_top_k`, `settings.rag_max_context_chars`), or `""` when
`settings.rag_enabled` is false / nothing matches. The `services/` package is absent
from this checkout, so that wiring is documented here, not edited.

## Notes

- `prompts/RAG_SECTIONS.md` remains the data-collection sheet; it is not imported.
- Chunk size / top-k are tunable via `RAG_CHUNK_CHARS`-style call args and the
  existing `RAG_TOP_K` / `RAG_MAX_CONTEXT_CHARS` env settings in `config.py`.
