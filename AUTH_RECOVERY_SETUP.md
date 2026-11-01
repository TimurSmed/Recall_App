# Recall: email verification and password recovery

This update adds:

- a password recovery page at `/forgot-password`;
- one-time password reset links (30-minute expiry);
- one-time email confirmation links (24-hour expiry);
- `/account/security` for requesting email confirmation;
- session revocation after a password reset using `users.session_version`;
- hashed one-time tokens stored in `recall_auth_tokens`.

## Apply the database migration

Back up the PostgreSQL database first. From the project root, with the normal Python environment active, run:

```powershell
python run_sql_migrations.py
```

This applies `sql_migrations/006_auth_recovery.sql` to the existing multi-tenant database. It does not rewrite notes, tags, relations, or user IDs. Do not run `upgrade_multitenant.py` as part of this update; the existing multi-tenant schema is already in place.

## Configure mail

Copy the relevant settings from `.env.example` into the root `.env` and replace every example value. Keep `.env` private.

- `RECALL_PUBLIC_BASE_URL`: public HTTPS address of Recall (for example, the permanent domain or currently active HTTPS tunnel URL).
- `RECALL_SMTP_HOST` and `RECALL_SMTP_PORT`: SMTP server and port.
- `RECALL_SMTP_USERNAME` and `RECALL_SMTP_PASSWORD`: SMTP credentials, if required by the provider.
- `RECALL_EMAIL_FROM`: sender address accepted by the SMTP provider.
- Port 587 normally uses STARTTLS (`RECALL_SMTP_STARTTLS=1`, `RECALL_SMTP_USE_SSL=0`). Port 465 normally uses implicit SSL (`RECALL_SMTP_USE_SSL=1`, `RECALL_SMTP_STARTTLS=0`).

If you use a temporary Cloudflare Quick Tunnel, its public URL can change after restarting `cloudflared`. Update `RECALL_PUBLIC_BASE_URL` to the new `https://...trycloudflare.com` address whenever that happens, then restart Uvicorn. A permanent domain is preferable for ongoing use.

Restart Uvicorn after changing `.env`.

## Use

- On the sign-in page choose **Забыли пароль?**.
- After signing in, open `/account/security` to request email confirmation.
- Password reset confirms the email address as part of the reset process. This allows existing accounts to recover access even if they were created before email confirmation existed.
- New registrations automatically receive a confirmation email when SMTP and the public base URL are configured. Email confirmation is not yet required to sign in, so existing users are not locked out during this rollout.

The OAuth providers (GitHub and Yandex) are not part of this update yet.
