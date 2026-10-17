-- Core tables supplied by the four metadata exports; runner adds unchanged 001.
-- Column order is not known; types/defaults/nullability/constraints are known.
-- Fails if either core table already exists; never replaces existing tables.
CREATE TABLE public.conversations (
    id text CONSTRAINT conversations_pkey PRIMARY KEY,
    title text,
    mode text DEFAULT 'basic'::text,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    user_id uuid CONSTRAINT conversations_user_id_fkey
        REFERENCES auth.users(id) ON DELETE CASCADE ON UPDATE NO ACTION NOT DEFERRABLE
);
CREATE TABLE public.messages (
    id uuid DEFAULT gen_random_uuid() CONSTRAINT messages_pkey PRIMARY KEY,
    conversation_id text CONSTRAINT messages_conversation_id_fkey
        REFERENCES public.conversations(id) ON DELETE CASCADE ON UPDATE NO ACTION NOT DEFERRABLE,
    role text NOT NULL,
    content text NOT NULL,
    provider_used text,
    model_used text,
    created_at timestamptz DEFAULT now(),
    user_id uuid CONSTRAINT messages_user_id_fkey
        REFERENCES auth.users(id) ON DELETE CASCADE ON UPDATE NO ACTION NOT DEFERRABLE
);
