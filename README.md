# Philomath

Philomath is a multi-AI assistant that lets users connect their own AI providers and interact with them from one unified interface.

🌐 Live App: https://philomath-umber.vercel.app/

## ✨ 

- Google authentication
- Bring Your Own API Key (BYOK)
- Supports multiple AI providers:
  - Gemini
  - OpenRouter
  - Cerebras
  - NVIDIA
  - Grok
- Encrypted API key storage
- Persistent conversations
- Per-user AI integrations
- Manual provider selection
- Multi-file upload support
- Dark AI workspace interface

## 🔐 Security

API keys are encrypted on the backend before being stored.

Users only have access to their own:

- AI integrations
- API keys
- Conversations
- Messages

Authentication and user data are managed through Supabase.

## 🛠 Tech Stack

Frontend:
- Next.js
- TypeScript
- React
- Tailwind CSS

Backend:
- FastAPI
- Python

Database & Authentication:
- Supabase

Deployment:
- Vercel — Frontend
- Render — Backend

## 🚀 Architecture

User  
↓  
Vercel / Next.js Frontend  
↓  
Render / FastAPI Backend  
↓  
Supabase  
↓  
Connected AI Providers

## 💡 Concept

Instead of switching between multiple AI platforms, Philomath provides one interface where users can connect the AI services they already use.

Each user brings their own API keys, allowing Philomath to act as a personal multi-AI workspace.

## 📍 Status

Philomath is currently deployed and functional.

Live: https://philomath-umber.vercel.app/
