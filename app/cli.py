"""Command-line tasks.

    python -m app.cli migrate
    python -m app.cli create-user --email you@company.com --name "Your Name" --role admin
    python -m app.cli seed-demo
    python -m app.cli process-reports
    python -m app.cli purge-expired
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import func, select


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="Apply database migrations")
    cu = sub.add_parser("create-user", help="Create a recruiter or admin account")
    cu.add_argument("--email", required=True)
    cu.add_argument("--name", required=True)
    cu.add_argument("--role", choices=["admin", "recruiter"], default="recruiter")
    cu.add_argument("--password", help="Omit to be prompted (recommended)")
    sd = sub.add_parser("seed-demo", help="Load synthetic demo jobs and candidates (not for production)")
    sd.add_argument("--email", default="demo@example.com")
    sd.add_argument("--password", default="demo-password-123")
    sub.add_parser("process-reports", help="Generate queued evaluation reports")
    sub.add_parser("purge-expired", help="Delete candidate data past the retention period")
    sub.add_parser("build", help="Vercel build hook: migrate when RUN_MIGRATIONS_ON_BUILD=1")
    args = parser.parse_args(argv)

    from app.config import settings
    from app.database import db_session
    from app.logging_config import configure_logging
    from app.services.bootstrap import run_migrations

    configure_logging("INFO")

    if args.cmd in ("migrate",):
        run_migrations()
        print("Migrations applied.")
        return 0
    if args.cmd == "build":
        if os.environ.get("RUN_MIGRATIONS_ON_BUILD") == "1":
            run_migrations()
            print("Migrations applied during build.")
        else:
            print("Skipping migrations (set RUN_MIGRATIONS_ON_BUILD=1 to enable).")
        return 0

    from app.models import User
    from app.security import hash_password, validate_new_password

    if args.cmd == "create-user":
        password = args.password or getpass.getpass("Password (min 12 chars): ")
        err = validate_new_password(password)
        if err:
            print(f"Password rejected: {err}", file=sys.stderr)
            return 2
        with db_session() as db:
            if db.scalar(select(User).where(func.lower(User.email) == args.email.lower())):
                print("A user with that email already exists.", file=sys.stderr)
                return 2
            db.add(User(email=args.email.lower(), name=args.name, password_hash=hash_password(password), role=args.role))
            db.commit()
        print(f"Created {args.role} {args.email}.")
        return 0

    if args.cmd == "seed-demo":
        if settings().is_production:
            print("Refusing to load demo data in production.", file=sys.stderr)
            return 2
        from app.demo import seed

        with db_session() as db:
            user = db.scalar(select(User).where(func.lower(User.email) == args.email.lower()))
            if user is None:
                user = User(email=args.email.lower(), name="Demo Recruiter", password_hash=hash_password(args.password), role="admin")
                db.add(user)
                db.commit()
                print(f"Created demo admin {args.email} / {args.password}")
            result = seed(db, user)
        print(f"Demo data ready: {result}")
        return 0

    if args.cmd == "process-reports":
        from app.services.evaluation import process_pending

        with db_session() as db:
            n = process_pending(db, limit=20)
        print(f"Processed {n} report(s).")
        return 0

    if args.cmd == "purge-expired":
        from app.services.applications import purge_expired

        with db_session() as db:
            n = purge_expired(db)
        print(f"Purged {n} application(s).")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
