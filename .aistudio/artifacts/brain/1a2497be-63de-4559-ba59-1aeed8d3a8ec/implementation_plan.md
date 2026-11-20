# Stage 5 Three-Model Agent Evaluation Arena (Gemini + GPT-OSS-120B + Qwen)

Upgrade MIRAI's Stage 5 Agent Evaluation workflow from a 2-model runner into a 3-model live coding benchmark arena that evaluates **Gemini**, **GPT-OSS-120B (Groq)**, and **Qwen (Groq)** head-to-head against the same validated repository snapshot, reference controls, and test verifier.

## User Review & Critical Decisions

> [!IMPORTANT]
> The decisions below reflect your confirmed choices from our alignment questions and govern the implementation. Review and approve to begin building.

- **Confirmed Decision 1 — Inference Providers**: **Gemini** runs through the existing server-side Google GenAI client (`gemini/gemini-4-hydrogen`), while both **GPT-OSS-120B** (`groq/openai/gpt-oss-120b`) and **Qwen** (`groq/qwen/qwen3.8-27b`, editable to any Groq-hosted Qwen identifier) run through Groq's OpenAI-compatible API using server-side `GROQ_API_KEY`.
- **Confirmed Decision 2 — Three Pre-Filled Model Slots**: The Stage 5 Evaluation workspace displays 3 pre-filled competitor slots (**Gemini**, **GPT-OSS-120B**, and **Qwen**) with individual enable/disable checkboxes so you can run all 3 together or toggle any subset without retyping identifiers.
- **Confirmed Decision 3 — Missing-Key Resilience**: If `GROQ_API_KEY` is not yet configured in environment secrets, the Evaluation page clearly marks Groq slots as awaiting server credentials while still letting **Gemini** run live trials and recording unconfigured Groq slots transparently as unscored `provider_unavailable` trials instead of blocking or crashing the campaign.
- **Trial Budget Adjustment (Recommended Default)**: Increase the live campaign limit from 6 trials to **9 trials** so all 3 models can run up to **3 independent repeats** (`3 models × 3 repeats = 9 trials`) in a single benchmark report.

---

## 1. Overview & Core Concept

- **What It Does**: Transforms Stage 5 (`Agent Evaluation`) into a unified 3-family coding benchmark arena where three distinct LLM families attempt the exact same validated coding task on the exact same repository snapshot—without seeing the reference solution—and are graded under identical controls and metrics.
- **Target Audience / Persona**: Hackathon judges, benchmark engineers, and developers comparing coding performance across frontier proprietary (`Gemini`) and open-weight (`GPT-OSS-120B`, `Qwen`) model families.
- **Key Value**: Eliminates single-model self-bias by separating task authoring (Stages 1–3) from multi-family evaluation (Stage 5), producing exportable head-to-head metrics (`summary.csv` and `trials.csv`) with latency, token usage, Wilson 95% confidence intervals, and per-trial evidence logs.

---

## 2. User Experience & Visual Design

### Key User Flows

1. **Zero-State & Provider Readiness Check**:
   - Upon opening **Agent Evaluation** for a validated task and selecting **Live coding agent**, the page fetches read-only provider availability from the backend (`Gemini: Ready`, `Groq: Ready` or `Groq: Awaiting GROQ_API_KEY in Secrets`).
   - Three pre-filled model slots appear in a clean 3-column grid (collapsing gracefully on smaller viewports):
     1. **Slot 1 — Gemini**: Checked by default, model `gemini/gemini-4-hydrogen`
     2. **Slot 2 — GPT-OSS-120B (Groq)**: Checked by default, model `groq/openai/gpt-oss-120b`
     3. **Slot 3 — Qwen (Groq)**: Checked by default, model `groq/qwen/qwen3.8-27b`
2. **Customizing & Launching a 3-Model Run**:
   - Users can check/uncheck any of the 3 slots, edit a model ID (for instance switching `groq/qwen/qwen3.8-27b` to `groq/qwen/qwen3-32b`), and select **Independent repeats** (1–3 when 3 models are enabled, up to 5 when 1 model is enabled).
   - If `GROQ_API_KEY` is missing, an inline informational banner notes that Groq slots will be marked `provider_unavailable` while Gemini executes live trials—never blocking the user and never exposing an API key input field in the UI.
   - Clicking **Run 3-model evaluation** (dynamic count label) queues the asynchronous campaign job and displays live job progress.
3. **Comparing Results & Exporting Evidence**:
   - When the campaign finishes, the **Benchmark report** opens with a **Head-to-Head Model Comparison** table comparing all enabled agents side-by-side (Model Label, Identifier, Scored/Scheduled, Verified Success Rate, Mean Duration, Input/Output Tokens, Status).
   - Below the summary table, users can inspect detailed per-model metric grids, download **Summary CSV** or **Trials CSV** (covering all 3 models plus Oracle and No-op controls), and expand individual **Trial Evidence** drawers with full execution logs and submitted file diffs.

### Visual Identity & Theme

- **Aesthetic Direction**: Preserves MIRAI's existing high-clarity developer console aesthetic in both **Light mode** and the **Charcoal Dark theme** (`#121214` light-black canvas, `#1b1b1f` neutral-grey surfaces, `#f5f5f7` crisp white text), adhering strictly to SaaS dashboard discipline.
- **Color Palette & Mood**: Uses MIRAI's semantic theme variables (`--bg-app`, `--bg-card`, `--bg-muted`, `--border-default`, `--text-primary`, `--text-secondary`, `--accent-primary`) with 60-30-10 discipline and semantic state colors (`--success-text`, `--warning-text`, `--danger-text`) always paired with explicit text labels—never hue-only signaling.
- **Typography & Hierarchy**: Uses the workspace's established display/body typography paired with monospace tabular figures (`font-variant-numeric: tabular-nums`) across all pass rates, durations, token counts, and Wilson intervals so comparison columns align cleanly.
- **Component Styling & Restraint**:
  - Single-elevation flat cards with 1px hairline borders—no nested card sandwiches and no thick left accent bars.
  - Zero-pill metadata discipline: provider names, readiness notes, and trial summaries render as clean unboxed inline text separated by `·` rather than pill clusters.
  - Responsive 3-column model selector strip and compact high-density comparison table (`36px–44px` rows, left-aligned text, right-aligned tabular numbers).

### Interactive Feedback & States

- **Loading**: Disables launch controls while a campaign job is queued/running and shows live job state feedback.
- **Empty / Unvalidated Guard**: Clearly links back to Validation if the task has not passed all validation gates yet.
- **Partial / Provider Error Recovery**: If one model hits a Groq rate limit or missing key, the remaining models still complete and score normally, with exact upstream error diagnostics saved into that trial's console log.

---

## 3. Key Product Decisions & Trade-Offs

- **Decision 1 — Native Server-Side Groq Runner (Zero Extra SDK Bloat)**
  - *Chosen Approach*: Implement a dedicated server-side Groq coding agent runner using Node 22's built-in `fetch` against `https://api.groq.com/openai/v1/chat/completions` with structured JSON output enforcement, automatic `<think>` block stripping for reasoning models (`gpt-oss-120b` and `qwen`), and retry/backoff on transient `429`/`5xx` errors.
  - *Why*: Groq's API is strictly OpenAI-compatible; using native `fetch` keeps dependencies minimal, avoids version conflicts, and gives full control over reasoning-token parsing and error categorization.
  - *Alternatives Considered*: Adding a third-party `groq-sdk` or routing Qwen through Hugging Face Inference API (passed over due to cold-start latency and extra credential complexity).

- **Decision 2 — Non-Blocking Partial Execution When `GROQ_API_KEY` Is Unset**
  - *Chosen Approach*: Let the 3-model campaign proceed even if `GROQ_API_KEY` is absent; Gemini runs real live trials while Groq trials record structured `provider_unavailable` logs with `trustworthy: false` and `reward: null` so they never skew scored pass rates.
  - *Why*: Matches your preference so you can test and demo the 3-model workflow immediately before adding `GROQ_API_KEY` in Settings > Secrets, without ever fabricating or mocking Groq outputs.
  - *Alternatives Considered*: Blocking the entire campaign or silently substituting Gemini for Groq (passed over because silent substitution would misrepresent benchmark integrity).

- **Decision 3 — Unified Evaluation Schema & CSV Parity**
  - *Chosen Approach*: Keep `BenchmarkMetrics` and `Trial` fully backward-compatible while enriching `BenchmarkMetrics` with optional `model` metadata (preserving existing history reports cleanly) and raising the campaign cap from 6 to 9 trials.
  - *Why*: Previously saved evaluation history remains viewable with zero breaking changes, while new 3-model campaigns export cleanly into `summary.csv` and `trials.csv`.

---

## 4. Technical Architecture & Data Strategy *(Technical Reference)*

### Architecture & Component Diagram

```text
┌────────────────────────────────────────────────────────────────────────────┐
│                        Stage 5: Evaluation Page UI                         │
│                                                                            │
│  ┌──────────────────────┐ ┌──────────────────────┐ ┌─────────────────────┐ │
│  │ Slot 1: Gemini       │ │ Slot 2: GPT-OSS-120B │ │ Slot 3: Qwen        │ │
│  │ [x] gemini/gemini-4  │ │ [x] groq/openai/     │ │ [x] groq/qwen/      │ │
│  │     -hydrogen        │ │     gpt-oss-120b     │ │     qwen3.8-27b     │ │
│  │ Status: Ready        │ │ Status: Ready / Key? │ │ Status: Ready / Key?│ │
│  └──────────┬───────────┘ └──────────┬───────────┘ └──────────┬──────────┘ │
│             └────────────────────────┼────────────────────────┘            │
│                                      ▼                                     │
│             [ Run 3-model evaluation ]  ──►  Head-to-Head Matrix & CSVs    │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       │ Authenticated REST (Firebase Bearer)
                                       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                         Express Backend API Layer                          │
│                                                                            │
│  GET /evaluation/providers  ──► Returns safe booleans { gemini, groq }     │
│  POST /tasks/:id/jobs       ──► Queues background evaluation campaign      │
└──────────────────────────────────────┬─────────────────────────────────────┘
                                       ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                     Evaluation Campaign Orchestrator                       │
│                                                                            │
│  1. Run deterministic controls: Oracle (1.0) + No-op Baseline (0.0)        │
│  2. Loop enabled agent slots (1..3) × independent repeats (up to 9 total): │
│     ├── Prefix "gemini/" ──► Server Gemini Runner (@google/genai)          │
│     ├── Prefix "groq/"   ──► Server Groq Runner (GROQ_API_KEY via fetch)   │
│     └── Missing Key      ──► Unscored Trial (provider_unavailable + log)   │
│  3. Aggregate per-model BenchmarkMetrics + Wilson 95% CI + CSV exporters   │
└────────────────────────────────────────────────────────────────────────────┘
```

### Data Model & State

- **Provider Readiness Payload**: Read-only server response (`{ gemini: { configured, default_model }, groq: { configured, default_models: { gpt_oss, qwen } } }`). Never transmits secret values to the client.
- **Agent Slot Configuration (Client State)**: Array of 3 structured slots (`id`, `label`, `provider`, `enabled`, `model`), defaulting to Gemini (`gemini/gemini-4-hydrogen`), GPT-OSS-120B (`groq/openai/gpt-oss-120b`), and Qwen (`groq/qwen/qwen3.8-27b`).
- **Evaluation Report & Metrics (Server Persisted per User UID)**: Existing `EvaluationReport`, `Trial`, and `BenchmarkMetrics` records stored under the authenticated user's isolated store; each agent slot gets a clear human-readable label (`gemini`, `gpt-oss-120b`, `qwen`) and preserves exact model identifier, token counts, duration, and verifier logs.
- **Environment Configuration**: Document `GROQ_API_KEY=` in `.env.example` so it can be added anytime in **Settings > Secrets**.

### Interactive Component & State Mapping

- **Provider Readiness Fetch**: On mount, queries the authenticated provider readiness endpoint and updates non-blocking status hints on each of the 3 slots.
- **Slot Enable Checkbox & Model Input**: Toggling checkboxes dynamically updates the enabled agent count, recalculates maximum allowed repeats (`total trials <= 9`), and updates the primary CTA label (`Run 3-model evaluation`, `Run 2-model evaluation`, etc.).
- **Validation Guard**: Validates enabled model identifiers (`gemini/...`, `groq/...`, `openai/...`, `huggingface/...`), ensures at least 1 slot is checked in Live Coding Agent mode, and enforces `enabledCount * repeats <= 9`.
- **Campaign Polling & Report Display**: Uses the existing `ProjectContext` job queue and polling loop; upon completion renders the **Head-to-Head Comparison Matrix**, per-model metric sections, CSV downloads, and expandable per-trial console logs.
