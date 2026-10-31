"""Generate Recall's single-user authentication settings for .env."""

import base64
import getpass
import hashlib
import secrets
import re

ITERATIONS = 600_000


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def main() -> None:
    print("Recall authentication setup")
    print("The password is read without displaying it and is never written to a file.\n")

    username = input("Login [owner]: ").strip() or "owner"
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", username):
        raise SystemExit("Login must be 3–64 characters: letters, numbers, . _ @ -")

    password = getpass.getpass("New password (at least 12 characters): ")
    confirmation = getpass.getpass("Repeat password: ")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    )
    password_hash = f"pbkdf2_sha256${ITERATIONS}${b64(salt)}${b64(digest)}"
    secret_key = b64(secrets.token_bytes(48))

    print("\nAdd these lines to the root .env file:")
    print(f"RECALL_USERNAME={username}")
    print(f"RECALL_PASSWORD_HASH={password_hash}")
    print(f"RECALL_SECRET_KEY={secret_key}")
    print("RECALL_SESSION_TTL_SECONDS=28800")
    print("\nKeep .env private. Do not commit it or send these values to anyone.")


if __name__ == "__main__":
    main()
