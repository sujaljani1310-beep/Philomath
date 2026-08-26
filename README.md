# Philomath

**Philomath** is a full-stack multi-AI workspace that allows users to connect their own AI providers and interact with multiple models through one unified interface.

🌐 **Live App:** https://philomath-umber.vercel.app/

---

## ✨ Features

- 🔐 Google authentication
- 🔑 Bring Your Own API Key (BYOK)
- 🤖 Multiple AI provider integrations
- 🔒 Encrypted API key storage
- 💬 Persistent conversation history
- 👤 Per-user AI integrations
- 🎛 Manual provider selection
- 📎 Multi-file upload support
- 🌙 Dark AI workspace interface
- ☁️ Fully deployed frontend and backend

### Supported AI Providers

- Google Gemini
- OpenRouter
- Cerebras
- NVIDIA
- xAI Grok

---

## 💡 Concept

Most AI platforms require users to switch between different applications, accounts, and interfaces.

Philomath brings multiple AI services into one workspace.

Users connect their own API keys, select the provider they want to use, upload files, and maintain persistent conversations from a single interface.

Philomath uses a **Bring Your Own API Key (BYOK)** architecture, allowing users to control which AI services are connected to their account.

---

## 🔐 Security

API keys are encrypted by the backend before being stored.

Each authenticated user only has access to their own:

- AI integrations
- API keys
- Conversations
- Messages

Authentication, database storage, and user management are handled through **Supabase**.

---

## 🛠 Tech Stack

### Frontend

- Next.js
- React
- TypeScript
- Tailwind CSS

### Backend

- FastAPI
- Python

### Database & Authentication

- Supabase
- PostgreSQL

### Deployment

- **Vercel** — Frontend
- **Render** — Backend

---

## 🏗 Architecture

```text
                    ┌─────────────────────┐
                    │        User         │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Next.js / Vercel   │
                    │      Frontend       │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  FastAPI / Render   │
                    │       Backend       │
                    └───────┬─────┬───────┘
                            │     │
                  ┌─────────┘     └──────────────┐
                  ▼                              ▼
        ┌─────────────────┐          ┌────────────────────┐
        │    Supabase     │          │    AI Providers    │
        │ Auth + Database │          │ Gemini / Grok /    │
        └─────────────────┘          │ OpenRouter / etc.  │
                                     └────────────────────┘
```

---

## ⚙️ How It Works

1. The user signs in with Google.
2. The user connects one or more supported AI providers using their own API keys.
3. Philomath encrypts and stores the credentials.
4. The user selects an AI provider and sends a prompt.
5. The FastAPI backend routes the request to the selected provider.
6. The response is returned to the frontend.
7. Conversations and messages are saved to the user's account.

This allows Philomath to operate as a **provider-independent AI workspace** instead of being tied to one AI model or company.

---

## 🎯 Why I Built It

AI tools are powerful, but using multiple providers often means managing different tabs, interfaces, accounts, and conversation histories.

I built Philomath to explore what a unified interface for multiple AI ecosystems could look like while working with:

- Full-stack application architecture
- REST API development with FastAPI
- Third-party AI APIs
- Authentication and authorization
- Secure credential handling
- Relational databases
- Persistent user data
- Frontend/backend communication
- Cloud deployment

---

## 🚀 Status

**Philomath is deployed and functional.**

Users can authenticate, connect supported AI providers, manage their integrations, upload files, and maintain persistent conversations.

🌐 **Live App:**  
https://philomath-umber.vercel.app/

---

## 🔮 Future Improvements

- Automatic AI provider routing
- Additional AI providers
- Streaming responses
- Improved file understanding
- Usage analytics
- Provider performance comparison
- Better conversation search and organization

---

## 👨‍💻 Author

**Sujal Jani**

Computer Science student interested in software engineering, AI systems, and building practical full-stack applications.
