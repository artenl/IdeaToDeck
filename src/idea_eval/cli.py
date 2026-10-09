"""Command line: run an evaluation, manage whitelisted users, or start the web server."""

import argparse
import asyncio
import getpass
import json
import sys
from datetime import UTC, datetime

from .auth import hash_password, normalize_email, valid_email
from .config import get_settings
from .db import Database
from .keys import KEY_NAMES, check_keys, public_keys, runtime_settings

MIN_PASSWORD = 8


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != password:
            sys.exit("Passwords do not match.")
    if len(password) < MIN_PASSWORD:
        sys.exit(f"Password must be at least {MIN_PASSWORD} characters.")
    return password


def _db() -> Database:
    return Database(get_settings().db_path)


def cmd_run(args: argparse.Namespace) -> None:
    from .graph import run_pipeline
    from .llm import AnthropicLLM
    from .nodes import Deps
    from .report import to_markdown
    from .search import TavilySearch

    db = _db()
    settings = runtime_settings(get_settings(), db)
    if not settings.anthropic_api_key or not settings.tavily_api_key:
        sys.exit("Set the API keys in the app (KEYS button) or in .env.")
    deps = Deps(
        llm=AnthropicLLM(settings),
        search=TavilySearch(settings.tavily_api_key, db, settings.search_cache_days),
        settings=settings,
    )

    def progress(event: dict) -> None:
        if event.get("type") == "node" and event.get("status") in ("done", "error"):
            print(f"[{event['node']:>18}] {event['status']:5} {event['detail']}", file=sys.stderr)

    result = asyncio.run(run_pipeline(
        args.idea, deps=deps, mode="deep" if args.deep else "cheap", profile=args.profile,
        lang=args.lang, on_event=progress,
    ))
    output = json.dumps(result, indent=2) if args.json else to_markdown(result)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(output)
        print(f"Saved to {args.out}", file=sys.stderr)
    else:
        print(output)


def cmd_keys_test(_args: argparse.Namespace) -> None:
    db = _db()
    base = get_settings()
    settings = runtime_settings(base, db)
    shown = public_keys(base, db)
    results = asyncio.run(check_keys(
        {name: getattr(settings, name) for name in KEY_NAMES}, settings.cheap_model
    ))
    for name in KEY_NAMES:
        r = results[name]
        print(f"{name:<18} {shown[name]['masked'] or '-':<16} ({shown[name]['source']}) "
              f"{r['status'].upper()}: {r['message']}")


def cmd_users_add(args: argparse.Namespace) -> None:
    email = normalize_email(args.email)
    if not valid_email(email):
        sys.exit(f"Not a valid email: {email}")
    password = _read_password(args.password_stdin)
    _db().upsert_user(email, hash_password(password), is_admin=args.admin, monthly_limit=args.limit)
    role = "admin (unlimited)" if args.admin else f"user (limit {args.limit or 'default'}/month)"
    print(f"Whitelisted {email} as {role}.")


def cmd_users_list(_args: argparse.Namespace) -> None:
    users = _db().list_users()
    if not users:
        print("No users. Add one with: idea-eval users add EMAIL --admin")
    for u in users:
        limit = "unlimited" if u["is_admin"] else (u["monthly_limit"] or "default")
        role = "admin" if u["is_admin"] else "user"
        print(f"{u['email']:<40} {role:<6} runs/month: {limit}")


def cmd_users_remove(args: argparse.Namespace) -> None:
    email = normalize_email(args.email)
    print("Removed." if _db().delete_user(email) else f"No such user: {email}")


def cmd_users_passwd(args: argparse.Namespace) -> None:
    email = normalize_email(args.email)
    db = _db()
    if db.get_user(email) is None:
        sys.exit(f"No such user: {email}")
    db.set_password(email, hash_password(_read_password(args.password_stdin)))
    print("Password updated. Existing sessions are signed out.")


def cmd_waitlist(_args: argparse.Namespace) -> None:
    rows = _db().list_waitlist()
    if not rows:
        print("Nobody on the waitlist yet.")
    for r in rows:
        seen = datetime.fromtimestamp(r["last_seen"], UTC).strftime("%Y-%m-%d")
        print(f"{r['email']:<40} attempts: {r['attempts']:<4} last: {seen}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("idea_eval.web:create_app", factory=True, host=args.host, port=args.port,
                proxy_headers=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="idea-eval", description="Is this idea good?")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Evaluate an idea and print the report")
    run.add_argument("idea")
    run.add_argument("--deep", action="store_true", help="Use the deep model for judge/strategist")
    run.add_argument("--profile", default="", help="About you: skills, budget, time")
    run.add_argument("--lang", choices=["en", "fr"], default="en", help="Report language")
    run.add_argument("--json", action="store_true", help="Print raw JSON instead of Markdown")
    run.add_argument("--out", help="Write the report to a file")
    run.set_defaults(func=cmd_run)

    users = sub.add_parser("users", help="Manage whitelisted users")
    usub = users.add_subparsers(dest="users_command", required=True)
    add = usub.add_parser("add", help="Whitelist a user (or update an existing one)")
    add.add_argument("email")
    add.add_argument("--admin", action="store_true", help="Admin: unlimited runs")
    add.add_argument("--limit", type=int, default=None, help="Runs per month for non-admins")
    add.add_argument("--password-stdin", action="store_true")
    add.set_defaults(func=cmd_users_add)
    lst = usub.add_parser("list", help="List whitelisted users")
    lst.set_defaults(func=cmd_users_list)
    rm = usub.add_parser("remove", help="Remove a user and their runs")
    rm.add_argument("email")
    rm.set_defaults(func=cmd_users_remove)
    pw = usub.add_parser("passwd", help="Change a user's password")
    pw.add_argument("email")
    pw.add_argument("--password-stdin", action="store_true")
    pw.set_defaults(func=cmd_users_passwd)

    keys = sub.add_parser("keys", help="API keys")
    ksub = keys.add_subparsers(dest="keys_command", required=True)
    kt = ksub.add_parser("test", help="Check the Anthropic and Tavily keys in use")
    kt.set_defaults(func=cmd_keys_test)

    wl = sub.add_parser("waitlist", help="Show emails that got 'coming soon'")
    wl.set_defaults(func=cmd_waitlist)

    serve = sub.add_parser("serve", help="Start the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
