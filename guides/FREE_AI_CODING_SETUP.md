# Free, Professional-Grade AI Coding Setup (personal laptop)

> Checked on **27 Oct 2026**. Free tiers change often, so recheck the links before relying on them.
> Use **personal accounts only** on your personal laptop. Never use your company Copilot/GitHub account for personal projects.

## The honest summary

- No permanently free option is as strong and unlimited as paid Claude Code. But you can get **frontier-model quality for free** by combining 2–3 tools and using each one's free quota wisely.
- Several free options **ended this year**:
  - Gemini CLI's free tier stopped on **18 Jun 2026**; it was replaced by Google Antigravity.
  - Qwen Code's free login stopped on **15 Apr 2026**.
  - Cerebras now has only a 30-day $5 trial that needs a card.
- Local models on a laptop are fine for small edits and autocomplete, but **not** for building whole features correctly.

---

## Recommended stack (in priority order)

### 1. Google Antigravity (main driver), FREE
Agent-first IDE + desktop app + **Antigravity CLI**: it plans, edits multiple files, runs terminals and tests.
- **Free plan** (as of today) includes **Gemini 3.8 Flash**, **GPT-OSS-120b** and, for a few more days, **Claude Opus 4.6 (thinking)** and **Claude Sonnet 4.6 (thinking)**.
  - The Claude 4.6 models leave the free plan on **2 Nov 2026**. Use them NOW for the hardest work (architecture, tricky logic, reviews).
  - The quota is weekly; harder tasks use more of it.
- Free plan also has unlimited Tab completions, MCP support, skills and subagents.
- **Student bonus:** Google offers a **student plan at no cost for 1 year** with higher usage limits (https://gemini.google/students/). If your SRM email still works, claim it on your personal Google account for a higher Antigravity quota. Check which models it unlocks (some premium models are excluded on trial/promotional plans).
- Links: https://antigravity.google/download · plans: https://antigravity.google/docs/plans · models: https://antigravity.google/docs/models

### 2. GitHub Copilot Free in VS Code (second agent), FREE
- Copilot Free includes **agent mode, MCP, Copilot CLI**, a limited monthly allowance of chat/agent credits and 2,000 inline completions per month.
- **Copilot Student** (free, fuller access) is available if GitHub Education still verifies you as a student. Eligibility is rechecked monthly, so after graduating you may not qualify. Try https://github.com/settings/education/benefits with your SRM email.
- Plans: https://github.com/features/copilot/plans

### 3. Open-source agent + free API models (overflow / backup), FREE
When the quotas above run out:
- **Agent harness** (pick one): **Cline**, **Kilo Code** or **Roo Code** (VS Code extensions), or **OpenCode / Qwen Code / Aider** (terminal). All support any OpenAI-compatible endpoint.
- **Free model sources:**
  - **OpenRouter**: free models (`:free` suffix) with a small daily request cap; check `GET https://openrouter.ai/api/v1/key` → `free_model_daily_requests`.
  - **Groq** free tier: fast open models, rate-limited.
  - **Google AI Studio** API key: free tier for Gemini Flash models.
- Use these for simple, well-specified tasks (write tests, small functions, docs). Keep complex design work for tier 1.

### 4. Local models (offline / unlimited small tasks), FREE
- Install **Ollama** or **LM Studio**, then connect them to Cline / Continue / Qwen Code as a "custom OpenAI-compatible" provider (`http://localhost:11434/v1`).
- Pick a coder model by hardware (check the current Ollama library):

| Your laptop | Model size that runs well | Good for |
|---|---|---|
| 8–16 GB RAM, no GPU | 3–8B coder models | autocomplete, small edits |
| 16–32 GB RAM or 8–12 GB GPU | ~14–20B (e.g., gpt-oss-20b, mid-size Qwen coder) | single-file features, tests |
| 32 GB+ RAM or 24 GB GPU | ~30B MoE coder models | multi-file edits (still below frontier) |

---

## How to get Claude-level results from free tools (this matters more than the model)

1. **Use the strongest model for thinking, cheaper ones for typing.** Ask Opus/Sonnet (while free) or Gemini 3.8 Flash at "High" thinking to write the plan and acceptance criteria; let the cheaper model implement the pieces.
2. **Give the agent memory of your project.** Put an `AGENTS.md` at the repo root: stack, folder layout, commands (`make test`, `make dev`), coding rules, "never commit secrets". Antigravity, Copilot, Cline and Qwen Code all read project instruction files.
3. **Work in small, testable steps.** One phase per prompt (like `AGENT_PROMPTS.md`). Ask the agent to write tests first and run them after every change. Never accept "it should work" without a test run.
4. **Make it verify itself:** end every prompt with *"Run the tests and the app, paste the output, and fix failures before stopping."*
5. **Review diffs before accepting.** Ask a second model (a different tool) to review risky changes: *"Review this diff for bugs, security issues and missing edge cases."*
6. **Save quota:**
   - Start new chats per task (long chats burn tokens).
   - Point the agent at the exact files to read.
   - Paste error logs, not whole repos.
7. **Commit after every green step** so you can roll back bad AI edits instantly.

---

## Setup checklist (about 30 minutes)

1. Install **VS Code**, **Git**, **Node 22+**, **Python 3.11**, **uv**, **Docker Desktop**.
2. Install **Google Antigravity** and sign in with your personal Google account. If eligible, claim the **student plan** first.
3. In VS Code, sign in to **GitHub Copilot** with your personal GitHub (Kedareswar13) and enable Copilot Free (or Student).
4. Install the **Cline** (or Kilo Code) extension. Add providers: OpenRouter (free key), Groq (free key) and, optionally, local Ollama.
5. Optional: install **Ollama** and pull one coder model sized for your RAM.
6. In each project: add `AGENTS.md` and `.env.example`, and git-ignore `.env`.

## Suggested daily flow for NormBridge AI

| Task | Tool / model |
|---|---|
| Plan a phase, design APIs, debug a hard bug | Antigravity → Claude Opus 4.6 (until 2 Nov), then Gemini 3.8 Flash (High) |
| Implement the phase across files, run tests | Antigravity agent (or Copilot agent mode) |
| Unit tests, docstrings, README sections | Cline + OpenRouter/Groq free model, or local model |
| Code review before commit | A *different* tool from the one that wrote the code |
| Autocomplete while you code by hand | Antigravity Tab (unlimited) or Copilot inline |
