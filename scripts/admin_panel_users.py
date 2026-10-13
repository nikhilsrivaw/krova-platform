"""
Make the ADMIN_PANEL_USERS value for the server's .env (who may sign in to /admin).

    python -m scripts.admin_panel_users alice@example.com:password1 bob@example.com:password2

Prints one line to paste into .env. It holds salted hashes, never the passwords themselves.
After changing .env, recreate the app container (docker compose -f docker-compose.prod.yml up -d --force-recreate app).
"""
import sys

from shared.auth import admin_panel


def main() -> None:
    pairs = [a for a in sys.argv[1:] if ":" in a]
    if not pairs:
        print(__doc__)
        return
    entries = []
    for pair in pairs:
        email, password = pair.split(":", 1)
        entries.append(admin_panel.format_entry(email, password))
    print("ADMIN_PANEL_USERS=" + ",".join(entries))


if __name__ == "__main__":
    main()
