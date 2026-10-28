---
title: NormBridge AI API
emoji: 🧭
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# NormBridge AI API

Backend for NormBridge AI, a RAG assistant for SRM University-AP policies. The full guide is `docs/DEPLOYMENT.md` in the repository.

Space **secrets** (Settings → Variables and secrets):

| Name | Required | Example / note |
|---|---|---|
| `DATABASE_URL` | yes | `postgresql://USER:PASSWORD@ep-xxx.aws.neon.tech/neondb?sslmode=require` (free Neon database) |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | yes | your admin login |
| `SESSION_SECRET` | yes | 40+ random characters; signs login tokens (changing it signs everyone out) |
| `AUDIT_SALT` | yes | 20+ random characters; pseudonymises users in the audit log |
| `LLM_PROVIDER` | yes | `openai` |
| `LLM_API_BASE` | yes | `https://api.groq.com/openai/v1` |
| `LLM_API_KEY` | yes | your free Groq API key |
| `LLM_API_MODEL` | yes | `llama-3.1-8b-instant` |
| `YESNO_API_MODEL` | recommended | `llama-3.3-70b-versatile` (yes/no questions go to the bigger model) |
| `CORS_ORIGINS` | yes | `https://<your-frontend>.vercel.app` |
| `ALLOWED_EMAIL_DOMAIN` | no | `srmap.edu.in` (default) |
| `STAFF_EMAILS` | no | comma-separated emails that may read staff-only policies |
| `SMTP_EMAIL` / `SMTP_PASSWORD` | no | Gmail address + app password to email OTPs. Without them, staff OTPs appear in the Space **Logs** |