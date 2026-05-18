# Xiongan Policy Pipeline

This project downloads the current Xiongan New Area policy records, converts
each record to Markdown, generates declaration guides, structures those guides
into matching rules, and matches a company profile against the rules.

Source site:

https://xaiip.org.cn/#/government/policyFile?areaRange=%E9%9B%84%E5%AE%89%E6%96%B0%E5%8C%BA

The downloader uses the same public APIs as the website:

- `GET https://api.xaiip.org.cn/policyFile/list`
- `GET https://api.xaiip.org.cn/admin/policyFile/info`
- `GET https://api.xaiip.org.cn/files/view?url=...`

## 1. Download And Convert

Run:

```bash
python3 -u xiongan_policy_pdf_to_md.py --page-size 10 --strict-count
```

Default output:

- `data/xiongan_policy_files/pdfs/` for PDF attachments
- `data/xiongan_policy_files/files/` for non-PDF attachments
- `data/xiongan_policy_files/policy_md/` for final policy-level Markdown
- `data/xiongan_policy_files/manifest.jsonl`
- `data/xiongan_policy_files/manifest.json`
- `data/xiongan_policy_files/manifest.csv`

The expected current result is 152 Markdown files in `policy_md/`, one per
policy record.

Conversion rules:

- If a detail page has正文 and attachments, the正文 and all parsed attachments are
  merged into the same policy Markdown file.
- If a detail page has no网页正文 and the policy is represented by a PDF, that PDF
  is treated as `## 正文`.
- DOCX/DOC/XLSX/TXT-like readable attachments are converted into Markdown and
  included as `## 附件`.

PDF conversion defaults to MinerU. The MinerU key is loaded in this order:

1. `--mineru-api-key`
2. `MINERU_API_KEY`
3. `--mineru-key-file`, defaulting to `/Users/turing/PycharmProjects/PythonProject/ic/minerutomarkdown.py`

The key is read at runtime and is not copied into this repository.

## 2. Generate Declaration Guides

After `policy_md/` exists, generate one declaration guide per policy:

```bash
python3 -u summarize_declaration_guides.py --force --timeout 600 --max-input-chars 60000 --max-output-tokens 4096 --retries 2
```

Default LLM settings in code:

- API base: `http://192.168.211.108:8000/v1`
- API key: `EMPTY`
- model: `/data3/yangsien/models/Qwen3.6-27B`
- thinking mode disabled by default through:
  - `enable_thinking: false`
  - `chat_template_kwargs.enable_thinking: false`

Output:

- `data/xiongan_policy_files/declaration_guides/*_申报指南.md`
- `data/xiongan_policy_files/declaration_guides/guide_manifest.jsonl`
- `data/xiongan_policy_files/declaration_guides/guide_manifest.csv`

If a guide input exceeds `--max-input-chars`, the script skips model generation
for that document, still writes a guide file with the failure reason, records
the status in `guide_manifest`, and continues.

## 3. Build Policy Rules

Structure declaration guides into policy matching rules:

```bash
python3 -u build_policy_rules.py --rules-file data/xiongan_policy_files/policy_rules.jsonl --force --timeout 600 --max-input-chars 60000 --max-output-tokens 4096 --retries 2
```

Output:

- `data/xiongan_policy_files/policy_rules.jsonl`

Each JSONL row is one policy rule with fields such as:

- `policy_id`
- `title`
- `policy_summary`
- `benefit`
- `applicant_objects`
- `hard_conditions`
- `soft_conditions`
- `exclusion_conditions`
- `required_materials`
- `process`
- `deadline`
- `contacts`
- `missing_info_needed`
- `industry_keywords`
- `region_requirements`
- `company_stage_requirements`
- `confidence`
- `notes`

Test only the first five guides:

```bash
python3 -u build_policy_rules.py --limit 5 --rules-file data/xiongan_policy_files/policy_rules_preview.jsonl --force
```

## 4. Match A Company

Company profile input example:

```text
data/xiongan_policy_files/company_xiongan_huaqing.txt
```

The matcher no longer sends every rule to the model at once. It first parses the
company text into a structured profile, then uses code to prefilter policies
that are clearly expired, non-application notices, wrong region, wrong subject,
wrong industry, too early for the company's age, or missing an explicit
qualification. Only the remaining candidate policies are sent to the LLM one by
one.

Run full matching:

```bash
python3 -u match_company_policies.py \
  --rules-file data/xiongan_policy_files/policy_rules.jsonl \
  --company-file data/xiongan_policy_files/company_xiongan_huaqing.txt \
  --report-file data/xiongan_policy_files/company_xiongan_huaqing_match_report.md \
  --results-file data/xiongan_policy_files/company_xiongan_huaqing_match_results.jsonl \
  --prefilter-file data/xiongan_policy_files/company_xiongan_huaqing_prefilter.jsonl \
  --company-profile-file data/xiongan_policy_files/company_xiongan_huaqing_profile.json \
  --as-of-date 2026-05-14 \
  --timeout 600 \
  --max-output-tokens 4096 \
  --retries 2
```

Output:

- `data/xiongan_policy_files/company_xiongan_huaqing_match_report.md`
- `data/xiongan_policy_files/company_xiongan_huaqing_match_results.jsonl`
- `data/xiongan_policy_files/company_xiongan_huaqing_prefilter.jsonl`
- `data/xiongan_policy_files/company_xiongan_huaqing_profile.json`

Useful matching options:

- `--skip-llm`: only run the code prefilter and write a report, without calling
  the model.
- `--ignore-deadline`: do not exclude expired policies. Use this when you want
  to assess historical policy fit rather than current申报可行性.
- `--candidate-limit N`: send only the first N prefiltered candidates to the
  model.
- `--limit N`: inspect only the first N rules.
- `--enable-thinking`: opt in to model thinking mode. It is disabled by default.

Run code prefilter only:

```bash
python3 -u match_company_policies.py \
  --rules-file data/xiongan_policy_files/policy_rules.jsonl \
  --company-file data/xiongan_policy_files/company_xiongan_huaqing.txt \
  --report-file data/xiongan_policy_files/company_xiongan_huaqing_match_report_prefilter.md \
  --results-file data/xiongan_policy_files/company_xiongan_huaqing_match_results_prefilter.jsonl \
  --prefilter-file data/xiongan_policy_files/company_xiongan_huaqing_prefilter.jsonl \
  --company-profile-file data/xiongan_policy_files/company_xiongan_huaqing_profile.json \
  --as-of-date 2026-05-14 \
  --skip-llm
```

Test only the first five structured rules:

```bash
python3 -u match_company_policies.py \
  --rules-file data/xiongan_policy_files/policy_rules_preview.jsonl \
  --company-file data/xiongan_policy_files/company_xiongan_huaqing.txt \
  --report-file data/xiongan_policy_files/company_xiongan_huaqing_match_preview.md \
  --results-file data/xiongan_policy_files/company_xiongan_huaqing_match_preview.jsonl \
  --prefilter-file data/xiongan_policy_files/company_xiongan_huaqing_prefilter_preview.jsonl \
  --company-profile-file data/xiongan_policy_files/company_xiongan_huaqing_profile_preview.json \
  --timeout 600 \
  --max-output-tokens 4096 \
  --retries 2
```

## 5. Build And Ask The LLM Wiki

Build the persistent Markdown wiki from the 152 policies:

```bash
python3 -u build_llm_wiki.py --as-of-date 2026-05-15 --clean
```

`--clean` rebuilds `llm_wiki/` from scratch. Omit it if you want to preserve
saved pages in `llm_wiki/qa/`.

This does not call the model. It compiles existing `policy_md/`,
`declaration_guides/`, and `policy_rules.jsonl` into:

- `data/xiongan_policy_files/llm_wiki/index.md`
- `data/xiongan_policy_files/llm_wiki/overview.md`
- `data/xiongan_policy_files/llm_wiki/policies/`
- `data/xiongan_policy_files/llm_wiki/topics/`
- `data/xiongan_policy_files/llm_wiki/industries/`
- `data/xiongan_policy_files/llm_wiki/applicant_types/`
- `data/xiongan_policy_files/llm_wiki/agencies/`
- `data/xiongan_policy_files/llm_wiki/qa/`

Ask a question with Claude Code-style local retrieval plus the default
OpenAI-compatible model:

```bash
python3 -u ask_policy_wiki.py --show-sources --save "软件和人工智能企业现在能关注哪些雄安政策？"
```

By default this streams local tool-call events and then streams the final model
answer. The terminal output is shaped like:

```text
[tool_call] grep_collections {"roots": ["industries", "topics", "applicant_types"], ...}
[tool_result] grep_collections {"matches": 75, "pages": 12}
[tool_call] grep_policies {"roots": ["policies"], ...}
[tool_result] select_evidence_pages {"pages": ["industries/...", "policies/..."]}

[final_answer]
...
```

The QA script does not use vector search or reranking. Retrieval is handled by
`policy_wiki_search.py` with local filesystem operations:

- read `index.md` and `overview.md`;
- extract domain keywords and intent keywords from the question;
- grep `topics/`, `industries/`, and `applicant_types/`;
- follow wiki links from the best collection pages into `policies/`;
- grep policy pages directly;
- keep a small number of navigation pages and reserve most context for concrete
  policy pages;
- for questions about current availability, demote expired policies after more
  relevant active or open-ended policies.

Test retrieval only, without calling the model:

```bash
python3 -u ask_policy_wiki.py --no-llm --trace-search "高新技术企业申报条件和截止时间"
```

Use the standalone local search tool when you only want to inspect evidence:

```bash
python3 -u policy_wiki_search.py --top-k 8 "专精特新 小巨人 申报 条件"
```

Useful wiki QA options:

- `--top-k N`: retrieve N wiki pages before answering.
- `--max-context-chars N`: maximum context sent to the model.
- `--trace-search`: print the local retrieval trace and selected evidence pages.
- `--no-llm`: run retrieval only and skip model generation.
- `--no-stream-tools`: suppress live local tool-call events.
- `--no-stream-answer`: request the model in non-streaming mode.
- `--save`: save the answer into `llm_wiki/qa/` and append `log.md`.
- `--enable-thinking`: opt in to model thinking mode. It is disabled by default.
- `--disable-thinking`: explicitly keep model thinking mode off.

Enable model thinking mode only when needed:

```bash
python3 -u ask_policy_wiki.py --enable-thinking "这条政策和之前的专精特新政策有什么关系？"
```

## Useful Downloader Options

Test a small download:

```bash
python3 -u xiongan_policy_pdf_to_md.py --limit 2 --output-dir data/test_xiongan_policy_files
```

Redownload and reconvert:

```bash
python3 -u xiongan_policy_pdf_to_md.py --force
```

Only fetch metadata and write manifests:

```bash
python3 -u xiongan_policy_pdf_to_md.py --dry-run
```

Use local PDF extraction instead of MinerU:

```bash
python3 -u xiongan_policy_pdf_to_md.py --converter local
```

Local PDF mode uses `pymupdf` if installed, otherwise `pdftotext` from Poppler.
OCR can be enabled with:

```bash
python3 -u xiongan_policy_pdf_to_md.py --converter local --ocr
```

## Cleanup

Safe-to-delete test or preview outputs:

```text
data/test_mineru_dry_run/
data/test_policy_combined/
data/test_xiongan_policy_files/
data/xiongan_policy_files/policy_rules_preview.jsonl
data/xiongan_policy_files/company_xiongan_huaqing_match_preview.md
data/xiongan_policy_files/company_xiongan_huaqing_match_preview.jsonl
data/xiongan_policy_files/company_xiongan_huaqing_prefilter_preview.jsonl
data/xiongan_policy_files/company_xiongan_huaqing_profile_preview.json
data/xiongan_policy_files/company_xiongan_huaqing_match_report_prefilter_test.md
data/xiongan_policy_files/company_xiongan_huaqing_match_results_prefilter_test.jsonl
data/xiongan_policy_files/company_xiongan_huaqing_prefilter_test.jsonl
data/xiongan_policy_files/company_xiongan_huaqing_profile_test.json
data/xiongan_policy_files/summaries/
__pycache__/
.site-cache/
data/.DS_Store
data/xiongan_policy_files/.DS_Store
```

Keep these for the formal workflow:

```text
data/xiongan_policy_files/pdfs/
data/xiongan_policy_files/files/
data/xiongan_policy_files/policy_md/
data/xiongan_policy_files/declaration_guides/
data/xiongan_policy_files/manifest.jsonl
data/xiongan_policy_files/manifest.json
data/xiongan_policy_files/manifest.csv
data/xiongan_policy_files/policy_rules.jsonl
data/xiongan_policy_files/llm_wiki/
data/xiongan_policy_files/company_xiongan_huaqing.txt
data/xiongan_policy_files/company_xiongan_huaqing_match_report.md
data/xiongan_policy_files/company_xiongan_huaqing_match_results.jsonl
data/xiongan_policy_files/company_xiongan_huaqing_prefilter.jsonl
data/xiongan_policy_files/company_xiongan_huaqing_profile.json
```

`data/xiongan_policy_files/md/` is the older per-attachment Markdown cache.
It can be useful for reusing PDF parse results and avoiding repeated MinerU
calls, so keep it unless you are sure you will not rerun conversion and do not
need cached image assets.
