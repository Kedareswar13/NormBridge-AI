# CareHarbor AI: AI Front Desk for a Primary-Care Clinic

> **Complete build spec for an AI coding agent.** It covers what to build, the safety rules, the architecture, the exact environment of the build machine, the UI standard, the evals, the acceptance checklist and how to ship it to another laptop.
> Personal portfolio project built end to end by Kedareswar. **Synthetic data only, never real patient data.**

---

## 0. How to use this file

Attach this file in a new agent chat and paste the **Kick-off prompt** from section 13. The agent must:

1. Build everything **on this machine**, phase by phase (section 10), **running and testing each phase** and fixing every error before moving on.
2. Report briefly after each phase: what was built, the commands it ran, test results, and the concepts used, for learning.
3. **Never zip, upload or push anything.** When the acceptance checklist (section 11) passes, stop and ask the user: *"Build complete and verified. Shall I create the portable zip?"* Only zip after the user says yes.

---

## 1. Product: three patient journeys + FAQ + handoff

| Journey | Example message | What CareHarbor AI does |
|---|---|---|
| **1. Symptom guidance (triage-lite)** | "I've had a headache since morning, what should I do?" | Asks 1–3 safe follow-up questions.<br>If any **red flag** appears, it **stops** and says: *call emergency services (112 / 108 in India) or go to the nearest emergency department*, and creates a staff alert. Red flags include: worst-ever or sudden severe headache, stiff neck + fever, face droop, arm weakness, slurred speech, chest pain or pressure, breathing difficulty, fainting, confusion, severe bleeding, suicidal thoughts, pregnancy with bleeding or severe pain, infant fever.<br>Otherwise it gives self-care guidance **grounded in the clinic's care leaflets (RAG, with citations)**, says "when to see a doctor", and offers to book.<br>**Never diagnoses, never prescribes.** |
| **2. Appointments (existing patients)** | "I visited last month, can I see Dr. Rao again on Friday?" | 1. Verifies identity: phone + date of birth + 6-digit one-time code (mocked: the code is shown in the UI and server log in demo mode).<br>2. Finds the doctor and open slots, and proposes up to 3 options.<br>3. **Asks for explicit confirmation**, using a human-in-the-loop interrupt that shows a confirmation card in the UI.<br>4. Books via a tool, then **re-reads the database** and confirms only what is actually stored.<br>Reschedule and cancel work the same way. |
| **3. Prescription questions (after a visit)** | "When do I take the antibiotic the doctor gave me?" | After identity verification, reads **that patient's own prescription record** and explains the schedule, food instructions and duration **exactly as recorded**.<br>Any "can I stop / double / change / take something else?" question is **escalated** to a pharmacist or nurse (handoff).<br>Never changes doses, never recommends new medicines. |
| **FAQ** | "What are your OPD timings? Do you accept Star Health insurance?" | RAG over clinic policy documents (hours, departments, insurance, visiting rules, fees), with citations. |
| **Human handoff** | "I want to talk to a person" | Creates a ticket in the staff queue with a 3-line summary; the staff page shows it. |

---

## 2. Safety principles (non-negotiable)

1. **Deterministic red-flag check runs first**, before any LLM call (keyword + regex rules incl. Hinglish and typos, e.g. "chest me dard", "saans nahi aa rahi"). Then an LLM safety classifier is a second layer. Any hit → emergency message + staff alert; no further automation for that turn.
2. **No diagnosis, no dosing changes, no new-medicine advice.** An output guardrail blocks these patterns (e.g. "you have …", "take 2 tablets", "increase/decrease the dose"); the safe fallback is "please speak to your doctor or pharmacist" plus a handoff offer.
3. **Identity before PHI.** Prescription and appointment data are readable only after verification. `patient_id` comes from **verified session state**, never from model/tool arguments.
4. **Confirm only what the system did.** Every "booked / rescheduled / cancelled" claim must match a database row (deterministic eval).
5. **Synthetic data only.** A permanent UI banner reads: "Demo, not medical advice. In an emergency call 112 / 108."
6. **Audit log** of every tool call, guardrail hit and escalation (table + admin page).
7. **Prompt-injection resistance.** Retrieved leaflet text and user text are treated as data. Tools are allow-listed per agent.

### 2a. RAG and agent security: four layers (implement all four, as in NormBridge AI)

| Layer | Where | What to implement |
|---|---|---|
| **1. Query validation, before anything else** | `security/input_guard.py`, first node in the graph | <ul><li>Regex/keyword detector for: instruction override ("ignore previous instructions", "you are now…", "developer mode"); prompt leaks ("show your system prompt"); chat-template delimiters (`<\|im_start\|>`, `[INST]`, `System:` at line start); data exfiltration ("list all patients", "show someone else's prescriptions", "dump the database"); SQL/path injection; max length 800 chars; control characters.</li><li>A blocked query **never reaches retrieval, tools or the LLM**. It gets a fixed polite refusal, is audited as `blocked` with the category, and has a unit test with false-positive checks (e.g. "Can I ignore the diet rules after surgery?" must pass).</li></ul> |
| **2. Access control, before retrieval and tool calls** | `security/access.py`, retriever, MCP server | <ul><li>Roles `patient` / `staff` / `admin`, resolved **server-side** (verified session / staff login), never from the request body.</li><li>Every document chunk carries `audience` (public / patient / staff) and `clinic_id` (tenant) metadata. The vector search applies `where={"$and": [{"clinic_id": X}, {"audience": {"$in": allowed}}]}` **inside the query**, so unauthorised chunks are never returned. Staff SOPs exist as staff-only docs to demonstrate this.</li><li>PHI tools read `patient_id` from the verified session only.</li><li>The semantic answer cache is partitioned by role and **never stores PHI answers**.</li></ul> |
| **3. Fixed prompt template** | `prompts/templates.py` | <ul><li>Constant system prompt (also enables Ollama KV-prefix caching).</li><li>User text goes only into `<user_message>`, retrieved text only into `<clinic_documents>`, tool results only into `<tool_result>`.</li><li>Every slot is sanitised: strip chat-template tokens, our own slot tags, line-start role prefixes and code fences.</li><li>A system rule says slot content is data, not instructions.</li><li>User input never changes the prompt structure.</li></ul> |
| **4. Observability with masking** | `security/audit.py`, `rag_audit` + `tool_audit` tables, staff "Security" page | <ul><li>One row per turn and per tool call: pseudonymous user id (salted SHA-256), masked identity, role, clinic, masked query, decision (answered / refused / blocked / emergency / handoff / cached), tool name + outcome, document ids + chunk ids (**never full document text**), latency.</li><li>PII/PHI masking **before** anything is written to logs, traces or DB history: patient names (from the DB), phone, email, DOB, Aadhaar, ABHA ID, MRN, card/account numbers.</li><li>Unit tests prove the masking.</li></ul> |

Add an eval suite **"Security"**:
- 20 injection / exfiltration attempts → **100% blocked or refused**;
- 10 cross-role / cross-tenant retrieval attempts → **0 leaks**;
- a log-scan test that greps the audit tables and trace files for raw phone, email and DOB values → **0 found**.

---

## 3. Architecture

```
Channels: Web chat (React) · browser voice input (Web Speech API) · Telegram (optional, only if TELEGRAM_BOT_TOKEN is set)
                │
          FastAPI gateway ─► Input guardrails (red-flag rules → emergency path; injection checks; PII redaction in logs)
                │
        LangGraph supervisor (router with structured output: triage | booking | prescription | faq | handoff | smalltalk)
        ├── TriageAgent        → care-leaflet RAG + follow-up questions + safe disposition (self-care / book / urgent / emergency)
        ├── BookingAgent       → MCP tools: find_doctors, get_slots, book_appointment, reschedule_appointment, cancel_appointment, list_my_appointments
        │                         (interrupt() before any write → UI confirmation card)
        ├── PrescriptionAgent  → verify_identity → get_prescriptions(session patient_id) → explain exactly as recorded
        ├── FAQAgent           → clinic-policy RAG with citations
        └── HandoffAgent       → staff queue ticket with a 3-line summary
                │
        Output guardrails (no-diagnosis / no-dosing / numbers-and-times must match tool output) → reply
Memory: LangGraph checkpointer (SQLite) per conversation thread; small long-term store (preferred doctor/language)
Tools: FastMCP server (stdio for the agent + HTTP for debugging) over SQLite
RAG: Ollama nomic-embed-text embeddings + local vector store (Chroma persistent or Qdrant local mode, no server)
Tracing: JSON-lines trace per turn (node, tool, latency, tokens) + an admin "Traces" page; Langfuse optional via env
```

**Stack (all free):**
- Python 3.11 via `uv`; FastAPI + uvicorn; LangGraph; FastMCP; `langchain-mcp-adapters`; `langchain-openai`.
- **LLM through the OpenAI-compatible API**, configured by `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`:
  - default local: Ollama at `http://localhost:11434/v1` with `qwen2.5:3b` (tool-calling capable, already installed);
  - fallback: `qwen2.5:1.5b`;
  - cloud: Groq / Gemini free tier.
- A **MockLLM** for deterministic tests.
- SQLite (Neon Postgres via `DATABASE_URL` in production).
- React + Vite frontend.
- Docker for Hugging Face Spaces (port 7860).

---

## 4. Build machine: facts and constraints (read before writing any code)

| Fact | Consequence |
|---|---|
| Windows 11 corporate laptop, **no admin rights**, PowerShell **5.1** (no `&&`, `\|\|`, `?.`) | Install everything per-user. Chain commands with `;` and `if ($?) {}` |
| Project folder: **`C:\Users\KKedareswar\ai-projects\careharbor`** | Create it fresh. Never touch other folders, company repos or `C:\Users\KKedareswar\ai-projects\normbridge` |
| Available: `uv` 0.9, Python 3.11 (via uv), **Node 25** + npm, `curl.exe`, `tar`, `winget` (user scope) | Use `uv venv --python 3.11`, set `UV_LINK_MODE=copy` and `UV_HTTP_TIMEOUT=600` |
| **Ollama is installed and running** on `localhost:11434` with `qwen2.5:3b`, `qwen2.5:1.5b`, `phi3:mini`, `nomic-embed-text` | Use these; don't pull large new models. CPU only (~55 prompt tokens/s), so keep prompts short |
| Network is slow and flaky; **downloads from huggingface.co are often reset** | Avoid HF model downloads (no fastembed / sentence-transformers models); use Ollama embeddings. Retry pip/npm installs |
| Ports already used by NormBridge AI: **3000, 5001, 5433, 11434** | CareHarbor AI uses **API 8010**, **UI 5174**. Never stop or kill other processes; never kill by name, only by your own PIDs |
| Windows console is cp1252 | Set `PYTHONUTF8=1`; reconfigure stdout to UTF-8 at startup (emoji logs otherwise crash) |
| `Invoke-WebRequest` can go through a corporate proxy for localhost | Use `curl.exe` in scripts and health checks |
| Windows lets two servers bind the same port (`SO_REUSEADDR`) | `run.ps1` must refuse to start if the port is already listening, and kill **process trees** on stop (`taskkill /PID <pid> /T /F`) |
| OneDrive folders break uv hardlinks | Keep the project outside OneDrive; `UV_LINK_MODE=copy` |
| POSIX env syntax in npm scripts (`CI=false …`) fails on Windows | Write cross-platform npm scripts |
| No Docker engine running locally | Write the Dockerfile but don't require Docker to build or test |

---

## 5. Data (synthetic, generated by `scripts/seed.py`, deterministic with a fixed random seed)

- **30 patients:** Indian names, phone, DOB, language preference, allergies.
- **8 doctors:** General Medicine, Paediatrics, Dermatology, ENT, Orthopaedics, Gynaecology, Cardiology, Physiotherapy; working days and slot length.
- **Slots** for the next 14 days.
- **20 existing appointments**, some in the past and some upcoming.
- **40 prescriptions:** drug, strength, dose, frequency, timing relative to food, duration, start date, prescribing doctor, notes.
- **15 original care leaflets** (written for this project, Markdown): fever, headache, cold & cough, sore throat, acid reflux, diarrhoea & hydration, minor cuts & burns, sprains, back pain, skin rash, seasonal allergy, sleep hygiene, managing blood pressure readings, diabetes foot care, when-to-seek-emergency-care.
  - Each has "self-care", "see a doctor if" and "emergency signs" sections.
- **8 clinic policy docs:** hours & OPD timings, departments & doctors, insurance & cashless, fees & payments, visiting rules, cancellation policy, lab & reports, privacy notice.

---

## 6. MCP tools (FastMCP), with unit tests for each

- `find_doctors(specialty?, name?)`
- `get_slots(doctor_id, date_from, date_to)`
- `book_appointment(slot_id, reason)`
- `reschedule_appointment(appointment_id, new_slot_id)`
- `cancel_appointment(appointment_id)`
- `list_my_appointments()`
- `get_prescriptions()`
- `search_care_leaflets(query)`
- `search_clinic_faq(query)`
- `create_handoff(summary, priority)`
- `verify_identity(phone, dob)` → sends mock OTP
- `confirm_otp(code)`

Rules:
- `patient_id` for PHI and write tools is injected **server-side from the verified session**, never accepted as a tool argument.
- Writes are idempotent and prevent double-booking (unique constraint plus a check).

---

## 7. UI standard (must match NormBridge AI's design)

**Look and layout**
- **Dark theme by default** (ink navy `#0b1020`, surfaces `#121a33`, primary indigo `#4f46e5` → violet `#7c3aed`, accent cyan `#06b6d4`), with a light-theme toggle.
- **Full-screen layout** (no floating card).
- **Everything rendered at 90% scale:** a root zoom variable, with full-height containers sized `calc(100vh / zoom)`.
- No watermark or background logos.
- Fonts: Inter + Plus Jakarta Sans.
- Subtle entrance animations, with `prefers-reduced-motion` respected.

**Chat lane (ChatGPT-style)**
- Single centred column (max-width ~820 px).
- User messages: right-aligned bubbles. Assistant messages: full-width plain text, no heavy card.
- One compact icon row under each answer: copy, read aloud, sources, 👍/👎, report.
- Answers **type out character by character** with a blinking caret (click to skip).
- While waiting, a **"thinking" panel** shows the real stages (checking safety → understanding → looking up records/leaflets → writing).
- A **centred "scroll to latest" arrow** appears only when the user has scrolled up.

**Composer**
- Floating input bar with no dark band behind it. It contains:
  - mic button;
  - a small **AI model button** that opens a menu of the installed models;
  - text field;
  - send button.
- A one-line disclaimer sits under it.

**Special UI elements**
- Emergency replies render as a red alert card with "Call 112 / 108".
- Booking confirmations render as a card with **Confirm / Change / Cancel** buttons (this drives the LangGraph interrupt).
- Every answer gets a small source/grounding badge.

**Pages**
1. Patient chat.
2. Staff console: handoff queue, appointments today, audit log, traces, evals dashboard with a "Run evals" button.
3. Login: patient chat needs no login; the staff console needs `STAFF_USERNAME` / `STAFF_PASSWORD` from `.env`.

---

## 8. Performance and caching targets

- Red-flag / emergency path: **< 1 s** (no LLM).
- FAQ / leaflet answers: **≤ 10 s on this CPU** with `qwen2.5:3b`. Use:
  - short constant system prompts (so Ollama reuses the KV prefix);
  - top-3 compressed chunks;
  - `num_predict` ≤ 300;
  - `keep_alive` 60 m;
  - model warm-up at startup.
  - If the target is missed, switch to `qwen2.5:1.5b` and report the trade-off.
- **Caches:** embedding LRU; semantic answer cache for FAQ only (never for PHI, triage or bookings), invalidated when documents change; tool-result cache for read-only doctor/slot lookups (30 s TTL).
- Every response includes per-stage timings (shown in the staff traces page).

---

## 9. Evals (deterministic first; an LLM judge only as an optional second opinion)

| Suite | Cases | Pass criteria |
|---|---|---|
| Emergency red flags | 30 phrasings (incl. Hinglish/typos) | **100% recall** → emergency path |
| No diagnosis / no dosing | 25 adversarial prompts | **0 violations** |
| Booking correctness | 20 multi-turn scenarios | Every booked/cancelled claim matches a DB row; no double-booking; no write without confirmation |
| Prescription fidelity | 20 questions | Every number/time in the reply matches the record |
| Identity gate | 10 attempts without verification | **0 PHI disclosures** |
| Prompt injection | 15 attacks (incl. inside leaflets) | **0 tool misuse** |
| Triage disposition | 30 labelled symptom stories | ≥ 90% agreement; disagreements err on the side of caution |
| FAQ grounding | 20 questions | answer facts found in cited docs; refuses out-of-scope |
| Security (section 2a) | 20 injection/exfiltration + 10 cross-role/tenant + log scan | 100% blocked/refused · 0 leaks · 0 raw PII in logs |

Tooling:
- `scripts/run_evals.py` writes a Markdown report to `evals/reports/` and exits non-zero below the thresholds (CI gate).
- The staff console has the same runner and shows the report.
- `pytest` unit tests for tools, guardrails and the graph (using the MockLLM).

---

## 10. Build phases (do them in order; each must run and pass before the next)

| Phase | Deliverable | Verified by |
|---|---|---|
| P0 | Folder, `uv` project, `.env.example`, config, UTF-8 logging, seed script + SQLite DB, leaflets & policy docs | `python scripts/seed.py` prints row counts |
| P1 | FastMCP server + 12 tools + tests | `pytest tests/test_tools.py` green |
| P2 | RAG index (Ollama embeddings + local vector store) over leaflets & policies | Retrieval smoke test returns the right doc for 10 sample queries |
| P3 | LangGraph supervisor + 5 agents + checkpointer + identity flow | CLI chat completes all 3 journeys |
| P4 | Guardrails + **four security layers (section 2a)** + HITL interrupt + handoff queue + audit log | Red-team smoke set passes; security unit tests green |
| P5 | FastAPI (port 8010) + React/Vite UI (port 5174) to the section 7 standard + staff console; Telegram adapter only if a token is set | Browser test of all journeys, screenshots checked |
| P6 | Caching + performance tuning to the section 8 targets | Timing report |
| P7 | Eval suites + runner + CI workflow file (`.github/workflows/evals.yml`) | All thresholds met, report saved |
| P8 | Portability: `setup.ps1`, `run.ps1` (`-Stop`), `scripts/make_portable_zip.ps1` (excludes `.venv`, `node_modules`, `.env`, data caches), Dockerfile for HF Spaces, docs | Fresh `setup.ps1` + `run.ps1` works from a clean copy of the folder |

---

## 11. Acceptance checklist (all must be true before asking to zip)

- [ ] `setup.ps1` then `run.ps1` start API (8010) and UI (5174) with **no errors**; `run.ps1 -Stop` stops only CareHarbor AI processes.
- [ ] All `pytest` tests pass; eval report shows every threshold in section 9 met.
- [ ] All three journeys + FAQ + handoff work in the browser; emergency card, booking confirmation card and staff queue verified.
- [ ] UI meets section 7 (dark, full-screen, 90% scale, ChatGPT-style lane, typewriter, thinking panel, centred scroll arrow, model menu in composer, no watermark).
- [ ] Section 8 latency targets met (or the trade-off documented).
- [ ] No secrets in code; `.env.example` complete; synthetic-data disclaimer visible.
- [ ] All four security layers (section 2a) are implemented, unit-tested, and visible on the staff "Security" page.
- [ ] Docs written:
  - `README.md` (pitch, architecture diagram, features, eval numbers, run commands);
  - `docs/PERSONAL_LAPTOP_SETUP.md` (zip → unzip → `setup.ps1` → `run.ps1`, troubleshooting, push to GitHub);
  - `docs/DEPLOYMENT.md` (HF Spaces + Vercel + Neon + Groq, all free);
  - `docs/LEARNING_ROADMAP.md` (every concept used, mapped to files, with YouTube search phrases, handwritten-notes prompts, self-check questions and interview questions);
  - `docs/ARCHITECTURE.md`.
- [ ] The project is presented as the author's own work. Don't mention any other project or repository as its origin.

Then **stop and ask** to create the zip. Only after a "yes", run `scripts/make_portable_zip.ps1` and report the path and size.

---

## 12. Interview talking points (what to say about this project)

- Deterministic safety before the LLM gives 100% red-flag recall, and it's testable.
- Human-in-the-loop with LangGraph `interrupt()` means no write happens without explicit confirmation.
- PHI isolation: `patient_id` is bound from the verified session, so the model can't request someone else's data.
- "Confirm only what the database says" is checked by a deterministic eval.
- MCP tool server with allow-listed tools per agent.
- Eval suites as a CI gate; latency budget and caching strategy; runs fully on free/local models.

---

## 13. Kick-off prompt (paste this in the new chat together with this file)

~~~text
You are building CareHarbor AI, my personal portfolio project. The attached CAREHARBOR_HOSPITAL_AGENT_BRIEF.md is the complete specification. Read ALL of it before writing code, especially section 4 (build-machine constraints), section 2a (the four security layers) and section 7 (UI standard).

Work autonomously on THIS Windows laptop in C:\Users\KKedareswar\ai-projects\careharbor:
- Build phase by phase (P0 → P8 in section 10). After each phase, actually run it, run its tests, fix every error, then give me a short update: what was built, the commands you ran, the test results, and the key concepts used, so I can learn them.
- Use only free/local tools: Ollama models already installed (qwen2.5:3b default, qwen2.5:1.5b fallback, nomic-embed-text for embeddings) via the OpenAI-compatible endpoint, plus SQLite. Avoid Hugging Face model downloads (the network resets them).
- Use ports 8010 (API) and 5174 (UI). Never stop, kill or modify other processes or folders, especially NormBridge AI (ports 3000/5001/5433) and Ollama.
- Synthetic data only. Never commit secrets; use .env.example.
- Present the project as my own work; don't mention any other project or repository as its origin.
- Do NOT zip, push, upload or deploy anything. When every item in the section 11 acceptance checklist is verified, stop and ask me: "Build complete and verified. Shall I create the portable zip?" Only zip after I say yes.

Start now with P0.
~~~
