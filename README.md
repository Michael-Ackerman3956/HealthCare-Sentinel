# Healthcare Sentinel

**An autonomous AI agent that triages healthcare data quality issues by clinical severity.**

Built for the [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com/) — Track 1: Agents That Do Real Work.

**[Landing Page](https://healthcare-sentinel-ai.vercel.app/)** · **[Demo Video](https://youtu.be/PLACEHOLDER)** · **[Sample Report](examples/triage-report.html)**

## Results

| Metric | Value |
|---|---|
| Records scanned | 55,500 across 4 tables |
| Critical issues caught | 5 (impossible ages, missing IDs, chronology violations) |
| Time to full triage | <90 seconds |
| Cost per run | Free (Gemini via AI Studio) / ~$0.80–1.50 (Claude Haiku) |
| Architecture | Two-pass: triage agent + adversarial verifier |
| Agent autonomy | 9 phases, ~40-60 tool calls, zero human intervention for reads |
| Severity scoring | Deterministic floor from clinical rules; LLM can raise, never lower |
| Audit trail | Every remediation logged — reviewer, device, IP, run mode, full SQL |
| Reversibility | Row-level CDC changelog; `--undo N` restores from before-images |
| LLM backends | Claude Haiku, Gemini Flash (AI Studio / Vertex AI), Ollama |
| Tests | 83 passing (`pytest tests/`) |

## For Judges — Quick Start

```bash
git clone https://github.com/Michael-Ackerman3956/HealthCare-Sentinel.git
cd HealthCare-Sentinel
pip install -r requirements.txt
```

### Option 0 — No setup, free (offline mode)

```bash
python sentinel.py --dry-run
```

Runs built-in clinical rules against the included SQLite database (55,500 patient records). No API key, no DataHub, no cost. A triage report opens in your browser automatically.

**Pre-generated report:** Open `examples/triage-report.html` directly — no setup needed.

**Pick one LLM backend** — only one API key needed:

### Option 1 — Google Vertex AI (recommended)

1. Enable the API and authenticate:

```bash
gcloud services enable aiplatform.googleapis.com --project=YOUR_PROJECT_ID
gcloud auth application-default login
```

2. Run:

```bash
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
export GOOGLE_GENAI_USE_VERTEXAI=1
SENTINEL_MODEL=gemini-2.5-pro python sentinel.py
```

<details><summary>Option 2 — Google AI Studio (free, no GCP needed)</summary>

1. Get a free API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Run:

```bash
export GOOGLE_API_KEY=AIza...
SENTINEL_MODEL=gemini-2.5-pro python sentinel.py
```

</details>

<details><summary>Option 3 — Anthropic Claude (~$0.80–1.50/run)</summary>

1. Get an API key at [console.anthropic.com](https://console.anthropic.com/)
2. Run:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python sentinel.py
```

</details>

### With DataHub (full agent mode)

All options above work standalone. To see the full DataHub integration (tag writeback, lineage tracing, learning persistence):

```bash
datahub docker quickstart       # starts DataHub locally

# (optional) register structured properties for trust scores
datahub properties upsert -f sentinel_properties.yaml
```

The agent discovers datasets, generates SQL checks, reasons about clinical severity, traces lineage, and asks your approval before data fixes.

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
SENTINEL_MODEL=gemini-2.5-pro python sentinel.py

# 2. New data arrives from "Clinic 2" (8,000 synthetic records, DIFFERENT issues)
python sample-data/add_clinic2_patients.py

# 3. Second run — agent recalls learnings from Run 1, triages new data
#    Terminal shows: "Prior learnings found (2000 chars)"
SENTINEL_MODEL=gemini-2.5-pro python sentinel.py

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

### Two-Pass Architecture

A full triage agent scans all tables, then an adversarial verifier independently re-checks every finding:

```
Pass 1: Triage Scan (9 phases — read + write + remediate)
  └─ Discover → Understand → Investigate → Trace → Assess
     → Record (auto) → Remediate (HITL) → Learn

Pass 2: Adversarial Verifier (read-only)
  └─ Re-runs SQL for each finding, attempts to REFUTE it
     Only confirmed findings survive; new issues added
```

Use `--no-verify` to skip Pass 2 for faster runs. Specialist prompts live in `skills/` — version-controlled and auditable.

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

In default mode, metadata writes (tags, descriptions, learnings) are auto-approved. Data fixes (`apply_fix`) require human approval via batch prompt. Reads are fully autonomous. Appropriate for healthcare data governance.

### DataHub Integration (9 tools)

- **Read tools**: `search`, `search_documents`, `get_entities`, `list_schema_fields`, `get_lineage`, `get_dataset_queries` — autonomous
- **Write tools**: `add_tags`, `update_description`, `save_document` — auto-approved (metadata only)

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
│  LLM — ReAct Agent Brain                   │
│  LangGraph: 9 phases, HITL, checkpoints    │
│  Two-pass: triage + adversarial verifier   │
└─────┬──────────────┬───────────────┬───────┘
      │              │               │
┌─────┴──────┐ ┌─────┴────────┐ ┌────┴──────────────┐
│ DataHub    │ │ SQLite data  │ │ Audit trail       │
│ Agent      │ │ run_sql (RO) │ │ _sentinel_audit   │
│ Context Kit│ │ apply_fix    │ │ CDC changelog     │
│ 9 tools    │ │ 55,500 rows  │ │ who/what/when/    │
└────────────┘ └──────────────┘ │ where/why + undo  │
                                └───────────────────┘
```

## Tech Stack

- **DataHub Agent Context Kit** — Dataset discovery, schema reading, lineage tracing, tag/description/structured property writeback, document memory
- **LangGraph** — ReAct agent loop, human-in-the-loop (interrupt/resume), checkpointing
- **Claude Haiku / Gemini Flash** — Clinical reasoning, autonomous SQL generation (free via AI Studio, ~$0.80–1.50/run Haiku)
- **SQLite** — Read-only scanning (`run_sql`), data remediation (`apply_fix`), CDC audit trail (`_sentinel_audit` + `_sentinel_changelog`)

## Sample Findings

The agent autonomously discovers issues and ranks them by clinical severity. Findings vary per run — the agent generates its own SQL checks, so each run may surface different issues. See `examples/triage-report.html` for a sample report from the included dataset (55,500 patient records across 4 tables).

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

