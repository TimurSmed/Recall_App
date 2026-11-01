-- External sign-in identities. Existing Recall users and owned data are untouched.
CREATE TABLE IF NOT EXISTS public.recall_oauth_accounts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    provider VARCHAR(16) NOT NULL CHECK (provider IN ('github', 'yandex')),
    provider_user_id VARCHAR(128) NOT NULL,
    provider_username VARCHAR(120),
    provider_email VARCHAR(254),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_recall_oauth_provider_subject UNIQUE (provider, provider_user_id),
    CONSTRAINT uq_recall_oauth_user_provider UNIQUE (user_id, provider)
);

CREATE INDEX IF NOT EXISTS ix_recall_oauth_accounts_user_id
    ON public.recall_oauth_accounts (user_id);

ALTER TABLE public.recall_oauth_accounts ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS recall_oauth_owner_isolation ON public.recall_oauth_accounts;
CREATE POLICY recall_oauth_owner_isolation ON public.recall_oauth_accounts
    USING (user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT)
    WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT);

CREATE OR REPLACE FUNCTION public.recall_find_oauth_user(
    p_provider TEXT,
    p_provider_user_id TEXT
) RETURNS TABLE(user_id BIGINT, username VARCHAR(32), session_version INTEGER)
LANGUAGE SQL SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT u.id, u.username, u.session_version
      FROM public.recall_oauth_accounts AS a
      JOIN public.users AS u ON u.id = a.user_id
     WHERE a.provider = p_provider
       AND a.provider_user_id = p_provider_user_id
       AND u.is_active = TRUE
     LIMIT 1
$$;

CREATE OR REPLACE FUNCTION public.recall_list_oauth_accounts()
RETURNS TABLE(provider VARCHAR(16), provider_username VARCHAR(120), provider_email VARCHAR(254), created_at TIMESTAMPTZ)
LANGUAGE SQL SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT a.provider, a.provider_username, a.provider_email, a.created_at
      FROM public.recall_oauth_accounts AS a
     WHERE a.user_id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
     ORDER BY a.provider
$$;

CREATE OR REPLACE FUNCTION public.recall_link_oauth_account(
    p_user_id BIGINT,
    p_provider TEXT,
    p_provider_user_id TEXT,
    p_provider_username TEXT,
    p_provider_email TEXT
) RETURNS TEXT
LANGUAGE SQL SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    WITH owner_user AS MATERIALIZED (
        SELECT u.id
          FROM public.users AS u
         WHERE u.id = p_user_id
           AND u.is_active = TRUE
           AND u.id = NULLIF(current_setting('app.user_id', true), '')::BIGINT
           AND p_provider IN ('github', 'yandex')
           AND p_provider_user_id IS NOT NULL
           AND length(p_provider_user_id) BETWEEN 1 AND 128
    ), identity_match AS MATERIALIZED (
        SELECT a.user_id
          FROM public.recall_oauth_accounts AS a
         WHERE a.provider = p_provider
           AND a.provider_user_id = p_provider_user_id
         LIMIT 1
    ), provider_match AS MATERIALIZED (
        SELECT a.provider_user_id
          FROM public.recall_oauth_accounts AS a
          JOIN owner_user u ON u.id = a.user_id
         WHERE a.provider = p_provider
         LIMIT 1
    ), inserted AS (
        INSERT INTO public.recall_oauth_accounts (
            user_id, provider, provider_user_id, provider_username, provider_email
        )
        SELECT u.id, p_provider, p_provider_user_id,
               NULLIF(left(p_provider_username, 120), ''),
               NULLIF(left(p_provider_email, 254), '')
          FROM owner_user AS u
         WHERE NOT EXISTS (SELECT 1 FROM identity_match)
           AND NOT EXISTS (SELECT 1 FROM provider_match)
        ON CONFLICT DO NOTHING
        RETURNING user_id
    )
    SELECT CASE
        WHEN NOT EXISTS (SELECT 1 FROM owner_user) THEN 'session_invalid'
        WHEN EXISTS (SELECT 1 FROM identity_match WHERE user_id = p_user_id) THEN 'already_linked'
        WHEN EXISTS (SELECT 1 FROM identity_match) THEN 'identity_in_use'
        WHEN EXISTS (SELECT 1 FROM provider_match) THEN 'provider_already_linked'
        WHEN EXISTS (SELECT 1 FROM inserted) THEN 'linked'
        ELSE 'conflict'
    END
$$;

REVOKE ALL ON FUNCTION public.recall_find_oauth_user(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.recall_list_oauth_accounts() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.recall_link_oauth_account(BIGINT, TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
