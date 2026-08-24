# Philomath — Google Login + Add AI Setup

This build changes Philomath to a per-user BYOK (bring your own key) app:

1. Sign in with Google.
2. If the account has no AI connected, Philomath opens **Add AI** automatically.
3. Enter an AI name and API key.
4. The backend encrypts the key before saving it in Supabase.
5. Manual Mode only shows AIs that user connected.
6. Basic and Auto modes only use that user's connected AIs.
7. Conversations are scoped to the signed-in user.

Supported AI names in this version: **OpenRouter, Cerebras, NVIDIA, Gemini, Grok**.

## 1. Install the new dependencies

### Backend

From `Philomath/backend` with your existing virtual environment active:

```bash
pip install -r requirements.txt
```

### Frontend

From `Philomath/frontend`:

```bash
npm install
```

`@supabase/supabase-js` was added to `package.json`.

## 2. Run the Supabase migration

Open the SQL Editor for the Supabase project Philomath already uses and run:

```text
supabase/migrations/001_google_auth_byok.sql
```

The migration adds user ownership to conversations/messages and creates the encrypted `user_ai_integrations` table.

## 3. Backend environment

Keep your existing `backend/.env` private. Add these values:

```text
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVER_ONLY_SECRET_OR_SERVICE_ROLE_KEY
PHILOMATH_ENCRYPTION_KEY=YOUR_FERNET_KEY
FRONTEND_ORIGINS=http://localhost:3000,http://127.0.0.1:3000
```

Generate the encryption key once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy that result into `PHILOMATH_ENCRYPTION_KEY` and keep it stable. If you change it later, previously saved API keys cannot be decrypted.

Never put `SUPABASE_SERVICE_ROLE_KEY` or `PHILOMATH_ENCRYPTION_KEY` in the frontend.

## 4. Frontend environment

Create `frontend/.env.local`:

```text
NEXT_PUBLIC_SUPABASE_URL=https://YOUR_PROJECT.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=YOUR_SUPABASE_PUBLISHABLE_KEY
NEXT_PUBLIC_BACKEND_URL=http://127.0.0.1:8000
```

The Supabase publishable key is intended for browser use. Do not use the service-role/secret key here.

## 5. Enable Google login in Supabase

In Supabase Authentication, enable the Google provider and configure it with the Google OAuth client ID and client secret.

For local development, make sure `http://localhost:3000` is allowed as a redirect URL/site URL. When Philomath is deployed, add the final Vercel URL too.

## 6. Start Philomath

From the project root:

```bash
./start.sh
```

Expected flow:

```text
Google Login
    ↓
Philomath
    ↓
No AI connected? → Add AI opens
    ↓
AI name + API key
    ↓
AI appears in Manual Mode
    ↓
Basic / Manual / Auto use only the user's saved AIs
```

## Security behavior

- API keys are sent to FastAPI only when being added/replaced.
- Keys are encrypted before being stored in Supabase.
- The frontend only receives the provider name and the last four key characters.
- Chat responses never include the saved API key.
- Every conversation/message query is filtered by the authenticated Supabase user ID.
- The integration table is backend-only.
