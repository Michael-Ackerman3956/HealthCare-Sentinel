# Healthcare Sentinel

**An autonomous AI agent that triages healthcare data quality issues by clinical severity.**

Built for the [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com/) — Track 1: Agents That Do Real Work.

## Results

| Metric | Value |
|---|---|
| Records scanned | 55,500 across 4 tables |
| Critical issues caught | 5 (impossible ages, missing IDs, chronology violations) |
| Time to full triage | <90 seconds |
| Cost per run | Free (Gemini Flash) / $0.80–1.50 (Claude Haiku, multi-agent) |
| Architecture | Multi-agent: 3 specialists + adversarial verifier + remediator |
| Agent autonomy | 9 phases, ~60 tool calls, zero human intervention for reads |
| Severity scoring | Deterministic floor from clinical rules; LLM can raise, never lower |
| Audit trail | Every remediation logged — reviewer, device, IP, run mode, full SQL |
| Reversibility | Row-level CDC changelog; `--undo N` restores from before-images |
| LLM backends | Claude Haiku, Gemini Flash (AI Studio / Vertex AI) |
| Tests | 83 passing (`pytest tests/`) |

## For Judges — Quick Evaluation

**No DataHub needed for a first look:**

```bash
git clone https://github.com/Michael-Ackerman3956/HealthCare-Sentinel.git
cd HealthCare-Sentinel
python sentinel.py --dry-run
```

This runs built-in clinical rules against the included SQLite database (55,500 patient records). No API key, no DataHub, no cost. A triage report opens in your browser automatically.

**Pre-generated report:** Open `examples/triage-report.html` directly — no setup needed.

**With DataHub (full agent mode):**

```bash
pip install -r requirements.txt
datahub docker quickstart       # starts DataHub locally

# (optional) register structured properties for Phase 6c trust scores
datahub properties upsert -f sentinel_properties.yaml
# or: python setup_datahub.py
```

Pick your LLM backend — any of these work:

| Backend | Setup | Cost | Best for |
|---|---|---|---|
| **Gemini Flash (AI Studio)** | `export GOOGLE_API_KEY=AIza...` | Free (20 req/day) | Quick testing |
| **Gemini Flash (Vertex AI)** | `export GOOGLE_CLOUD_PROJECT=my-proj` | ~$0.01/run | Production, multi-agent |
| **Claude Haiku** | `export ANTHROPIC_API_KEY=sk-ant-...` | ~$0.80-1.50/run | Best output quality |

```bash
# Then run with your chosen model:
SENTINEL_MODEL=gemini-3.6-flash python sentinel.py    # Gemini
SENTINEL_MODEL=claude-haiku-4-5 python sentinel.py    # Claude
python sentinel.py                                     # defaults to Claude Haiku
```

**Vertex AI setup** (for GCP users with credits):
```bash
gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
SENTINEL_MODEL=gemini-2.0-flash python sentinel.py
```

The agent will discover datasets, generate SQL checks, reason about clinical severity, trace lineage, and ask your approval before every DataHub write and data fix.

## Run Modes

| Command | What happens |
|---|---|
| `python sentinel.py` | Full agent. Asks before every write and data fix. ~$0.80-1.50/run (Haiku), ~$1.00-2.00/run (Gemini). |
| `python sentinel.py --dry-run` | Offline mode. Built-in rules, no API calls, no DataHub. Free. |
| `python sentinel.py --auto-approve` | Unattended mode. Agent writes to DataHub and fixes data without asking. **Use with caution** — all operations are logged and reversible, but no human reviews them before execution. Designed for trusted pipelines, not first runs. |
| `python sentinel.py --watch sample-data/` | Background daemon. Watches for new/changed CSVs, triages automatically. |
| `python sentinel.py --history` | Audit trail — who approved what, from which device, when. |
| `python sentinel.py --show 3` | Full detail of operation #3: SQL, rationale, reviewer, hostname, before/after. |
| `python sentinel.py --undo 3` | Revert operation #3 using CDC before-images. |
| `python sentinel.py --reset` | **Clean slate.** Restores original DB, clears DataHub learnings, removes old reports. Run this before your first evaluation. |

## For Judges — Step-by-Step Demo

```bash
# 0. Start clean (restores DB + clears all prior learnings)
python sentinel.py --reset

# 1. First run — agent triages 55,500 patient records, finds 6 issues
#    You'll see batch approval: approve with "all" or pick specific fixes
SENTINEL_MODEL=gemini-3.5-flash python sentinel.py

# 2. New data arrives from "Clinic 2" (8,000 synthetic records, DIFFERENT issues)
python sample-data/add_clinic2_patients.py

# 3. Second run — agent recalls learnings from Run 1, triages new data
#    Terminal shows: "Prior learnings found (2000 chars)"
SENTINEL_MODEL=gemini-3.5-flash python sentinel.py

# 4. (Optional) Review audit trail
python sentinel.py --history
python sentinel.py --show 1
```

**What to look for:**
- Run 1: Agent discovers issues autonomously, asks for approval before fixing
- Run 2: Agent uses learnings from Run 1 to check new data — zero configuration
- Reports open in your browser automatically after each run

The Clinic 2 data has the same schema but different quality problems (future dates, zero billing, invalid blood types, duplicate names). The agent uses patterns learned from the original data to check the new records — zero configuration, zero new rules written.

## How It Works

Healthcare Sentinel connects to your DataHub catalog, autonomously discovers datasets, generates quality checks, and ranks every finding by **patient harm** — not just row counts. It writes severity tags, structured properties, and warnings back to DataHub so the next person or agent inherits the knowledge.

### Multi-Agent Architecture (default)

Three specialist agents scan in parallel, an adversarial verifier confirms findings, then a single remediation pass applies fixes with human approval:

```
Pass 1: Specialist Scans (read-only, auto-approved)
  ├─ [CS] Clinical Safety — patient harm, dosing, identifiers
  ├─ [FI] Financial Integrity — billing, revenue, fraud indicators
  └─ [DQ] Data Quality — schema types, completeness, contamination

Pass 2: Adversarial Verifier (read-only)
  └─ Re-runs SQL for each finding, attempts to REFUTE it
     Only confirmed findings survive

Pass 3: Remediation + DataHub Writeback (HITL)
  └─ Tags, descriptions, apply_fix, save learnings
```

Each specialist's prompt lives in `skills/` — version-controlled and auditable. Use `--single-agent` to fall back to the original single-pass mode.

### 9 Phases (0–8)

| Phase | Name | What it does | DataHub Tool |
|---|---|---|---|
| 0 | **Recall** | Read past learnings | `search_documents` |
| 1 | **Discover** | Find all datasets in catalog | `search` |
| 2 | **Understand** | Read schemas | `list_schema_fields` |
| 3 | **Investigate** | Generate and run SQL quality checks | `run_sql` (local) |
| 4 | **Trace** | Follow lineage to contaminated downstream | `get_lineage` |
| 5 | **Assess** | Rank by clinical severity + deterministic floor | — |
| 6 | **Record** | Write tags, descriptions, trust scores to DataHub | `add_tags`, `update_description`, `add_structured_properties` |
| 7 | **Remediate** | Fix data with HITL approval | `apply_fix` (local) |
| 8 | **Learn** | Save structured learnings for next run | `save_document` |

### Human-in-the-Loop

In default mode, the agent asks for approval before every DataHub write and data remediation. Reads are autonomous; writes require human confirmation — appropriate for healthcare data governance.

### DataHub Integration (8 of 10 tools used)

- **Read tools**: `search`, `search_documents`, `get_entities`, `list_schema_fields`, `get_lineage` — autonomous, no approval needed
- **Write tools**: `add_tags`, `update_description`, `add_structured_properties`, `save_document` — gated by HITL unless `--auto-approve`
- **Per-entity structured properties**: `sentinel.trust_score`, `sentinel.worst_severity`, `sentinel.finding_count`, `sentinel.top_issue`, `sentinel.last_triage`, `sentinel.remediation_status`

### Self-Learning

The agent saves structured JSON learnings to DataHub after each run. On the next run, it pre-fetches these learnings and injects them into context — prioritizing known-bad columns and verified queries. Trust rules reject learnings that are duplicative, unscoped, or unreproducible.

## Autonomous Operation

**File Watcher** (`--watch`) — Polls a directory for CSV changes and classifies each file:

| Classification | Trigger | Action |
|---|---|---|
| NEW | File not seen before | Full 9-phase triage |
| APPEND | Same header, more rows | Incremental checks |
| UPDATE | Content changed | Re-run full triage |
| NO-OP | Nothing changed | Skip |

Classification uses content hashing, header hashing, and row counts.

## Audit Trail & Reversibility

Every data remediation is captured with **row-level Change Data Capture** — only affected rows are stored as JSON before-images (99% less storage than full-table snapshots).

**What the audit trail captures (HIPAA who/what/when/where/why):**

| Field | Source | Automatic? |
|---|---|---|
| **Who** — `approved_by` | OS login or `SENTINEL_REVIEWER` env | Yes |
| **What** — `op_type`, `sql_text`, `rows_affected` | From the operation | Yes |
| **When** — `ts` | ISO timestamp | Yes |
| **Where** — `hostname`, `ip_address` | `socket.gethostname()` | Yes |
| **Why** — `rationale` | Agent's clinical justification | Yes |
| **How** — `run_mode` | interactive / auto-approve / watch | Yes |
| **Denied** — logged as `DENIED:<user>` | Shows what agent wanted but human refused | Yes |

**Undo any operation:**
```bash
python sentinel.py --history          # see all operations
python sentinel.py --show 3           # inspect operation #3 in detail
python sentinel.py --undo 3           # revert operation #3
```

**Dual audit storage:**
- **Local**: `_sentinel_audit` + `_sentinel_changelog` tables in SQLite (full forensic detail)
- **DataHub**: `save_document` with audit log summary (catalog-searchable by other agents)

## Architecture

```
┌────────────┐
│ File       │  one-shot / --watch / --dry-run
│ Watcher    │
│ (--watch)  │
└─────┬──────┘
      │ triggers
┌─────┴──────────────────────────────────────┐
│  Claude (Haiku) — ReAct Agent Brain        │
│  LangGraph: 9 phases, HITL, checkpoints    │
│  Deterministic severity floor              │
└─────┬──────────────┬───────────────┬───────┘
      │              │               │
┌─────┴──────┐ ┌─────┴────────┐ ┌────┴──────────────┐
│ DataHub    │ │ SQLite data  │ │ Audit trail       │
│ Agent      │ │ run_sql (RO) │ │ _sentinel_audit   │
│ Context Kit│ │ apply_fix    │ │ CDC changelog     │
│ 8/10 tools │ │ 55,500 rows  │ │ who/what/when/    │
└────────────┘ └──────────────┘ │ where/why + undo  │
                                └───────────────────┘
```

## Tech Stack

- **DataHub Agent Context Kit** — Dataset discovery, schema reading, lineage tracing, tag/description/structured property writeback, document memory
- **LangGraph** — ReAct agent loop, human-in-the-loop (interrupt/resume), checkpointing
- **Claude Haiku / Gemini 2.5 Pro** — Clinical reasoning, autonomous SQL generation (~$0.80-1.50/run Haiku, ~$1.00-2.00/run Gemini)
- **SQLite** — Read-only scanning (`run_sql`), data remediation (`apply_fix`), CDC audit trail (`_sentinel_audit` + `_sentinel_changelog`)

## What the Agent Found

On the hackathon's healthcare dataset (55,500 patient records across 4 tables):

| Severity | Finding | Rows | Clinical Impact |
|---|---|---|---|
| CRITICAL | Impossible patient ages (-88, 262) | 832 | Fatal drug dosing errors |
| CRITICAL | Missing patient identifiers | 555 | Invisible in emergency triage |
| CRITICAL | Discharge before admission | 277 | Triggers $10K fraud audits |
| HIGH | Negative billing amounts | 1,215 | Revenue leakage |
| HIGH | Bad ages propagated to downstream marts | 832 | 3 tables contaminated |
| MEDIUM | Age column stored as TEXT | schema | Prevents numeric validation |

## What's Verified vs. What's Scaffolded

| Feature | Status |
|---|---|
| 9-phase autonomous pipeline | Verified end-to-end with DataHub quickstart |
| Dry-run mode (offline, free) | Verified — runs without API key or DataHub |
| HITL approval on mutations | Verified — interrupt/resume via LangGraph |
| Data remediation (apply_fix) | Verified — 6 operation types with CDC |
| Row-level CDC reversibility (--undo) | Verified — before-images, per-row restore |
| Audit trail (--history, --show) | Verified — reviewer, hostname, IP, run mode |
| File watcher (--watch) | Verified — NEW/APPEND/UPDATE/NO-OP classification |
| Deterministic severity floor | Verified — CLINICAL_RULES override LLM downgrades |
| Structured learnings (Phase 8) | Prompt-driven — JSON format with 6 trust rules |
| Structured properties (Phase 6c) | Prompt-driven — trust_score, severity per entity |
| Idempotent description writeback | Prompt-driven — sentinel markers prevent duplication |
| Report history | Verified — timestamped HTML files, symlink to latest |

### Future Work

- **DataHub Assertions** — promote findings to native pass/fail quality checks (not exposed in Agent Context Kit today)
- **DataHub Incidents** — raise active issues on datasets from triage findings
- **DataHub Data Contracts** — SLA-like guarantees built on assertion URNs

## License

Apache 2.0 — see [LICENSE](LICENSE)
