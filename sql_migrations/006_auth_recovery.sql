-- Email verification and password recovery. Existing accounts remain usable
-- their email is verified only after following a confirmation or reset link.
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS public.recall_auth_tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    purpose VARCHAR(32) NOT NULL CHECK (purpose IN ('verify_email', 'password_reset')),
    token_hash CHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_recall_auth_tokens_user_purpose
    ON public.recall_auth_tokens (user_id, purpose, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_recall_auth_tokens_expiry
    ON public.recall_auth_tokens (expires_at) WHERE used_at IS NULL;

CREATE OR REPLACE FUNCTION public.recall_issue_auth_token(
    p_user_id BIGINT,
    p_email TEXT,
    p_purpose TEXT,
    p_token_hash TEXT,
    p_expires_at TIMESTAMPTZ
) RETURNS TABLE(user_id BIGINT, email VARCHAR(254))
LANGUAGE SQL SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH target_user AS MATERIALIZED (
        SELECT u.id, u.email
          FROM public.users AS u
         WHERE u.is_active = TRUE AND u.email IS NOT NULL
           AND p_purpose IN ('verify_email', 'password_reset')
           AND ((p_user_id IS NOT NULL AND u.id = p_user_id)
             OR (p_user_id IS NULL AND p_email IS NOT NULL AND lower(u.email) = lower(p_email)))
         LIMIT 1
    ), deleted AS (
        DELETE FROM public.recall_auth_tokens t
         USING target_user u
         WHERE t.user_id = u.id AND t.purpose = p_purpose
        RETURNING t.id
    ), inserted AS (
        INSERT INTO public.recall_auth_tokens(user_id, purpose, token_hash, expires_at)
        SELECT u.id, p_purpose, p_token_hash, p_expires_at
          FROM target_user u
        RETURNING user_id
    )
    SELECT u.id, u.email
      FROM target_user u JOIN inserted i ON i.user_id = u.id
$$;

CREATE OR REPLACE FUNCTION public.recall_consume_email_verification(p_token_hash TEXT)
RETURNS BOOLEAN
LANGUAGE SQL SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH consumed AS (
        UPDATE public.recall_auth_tokens
           SET used_at = now()
         WHERE token_hash = p_token_hash AND purpose = 'verify_email'
           AND used_at IS NULL AND expires_at > now()
        RETURNING user_id
    ), changed AS (
        UPDATE public.users u
           SET email_verified_at = COALESCE(u.email_verified_at, now())
          FROM consumed c
         WHERE u.id = c.user_id AND u.is_active = TRUE
        RETURNING u.id
    )
    SELECT EXISTS (SELECT 1 FROM changed)
$$;

CREATE OR REPLACE FUNCTION public.recall_consume_password_reset(
    p_token_hash TEXT, p_password_hash TEXT
) RETURNS BOOLEAN
LANGUAGE SQL SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH consumed AS (
        UPDATE public.recall_auth_tokens
           SET used_at = now()
         WHERE token_hash = p_token_hash AND purpose = 'password_reset'
           AND used_at IS NULL AND expires_at > now()
        RETURNING user_id
    ), changed AS (
        UPDATE public.users u
           SET password_hash = p_password_hash,
               email_verified_at = COALESCE(u.email_verified_at, now()),
               session_version = u.session_version + 1
          FROM consumed c
         WHERE u.id = c.user_id AND u.is_active = TRUE
        RETURNING u.id
    )
    SELECT EXISTS (SELECT 1 FROM changed)
$$;

DROP FUNCTION IF EXISTS public.recall_find_user(TEXT);
CREATE FUNCTION public.recall_find_user(p_identifier TEXT)
RETURNS TABLE(
    id BIGINT,
    username VARCHAR(32),
    email VARCHAR(254),
    password_hash TEXT,
    is_active BOOLEAN,
    session_version INTEGER,
    email_verified_at TIMESTAMPTZ
)
LANGUAGE SQL SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT u.id, u.username, u.email, u.password_hash, u.is_active,
           u.session_version, u.email_verified_at
      FROM public.users AS u
     WHERE lower(u.username) = lower(p_identifier)
        OR lower(u.email) = lower(p_identifier)
     LIMIT 1
$$;

REVOKE ALL ON FUNCTION public.recall_issue_auth_token(BIGINT, TEXT, TEXT, TEXT, TIMESTAMPTZ) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.recall_consume_email_verification(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.recall_consume_password_reset(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.recall_find_user(TEXT) FROM PUBLIC;
