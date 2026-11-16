"""Set up Supabase transcript storage, and upload saved .eval logs to it.

    SUPABASE_ACCESS_TOKEN=... uv run python -m refund_eval.storage setup --region eu-west-2
    uv run python -m refund_eval.storage upload logs/ .context/dashboard/runs/
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
from pathlib import Path

from refund_eval.storage import setup
from refund_eval.storage.supabase import UploadError, upload_log


def eval_logs(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        found.extend(sorted(path.rglob("*.eval")) if path.is_dir() else [path])
    return found


async def upload_all(paths: list[Path]) -> int:
    failures = 0
    for path in eval_logs(paths):
        try:
            run_ids = await upload_log(path)
            print(f"{path}: {len(run_ids)} run(s) uploaded")
        except (UploadError, OSError, ValueError) as error:
            failures += 1
            print(f"{path}: failed: {error}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload", help="Upload .eval files, or every .eval file under a directory.")
    upload.add_argument("paths", nargs="+", type=Path)
    create = commands.add_parser("setup", help="Create or reuse a project, apply the schema and write .env.")
    create.add_argument("--project-ref", help="Use this existing project instead of creating one.")
    create.add_argument("--organization", help="Organization ID or name for a new project.")
    create.add_argument("--name", default="refund-eval-transcripts", help="Name for a new project.")
    create.add_argument("--region", default="eu-west-2", help="Region for a new project.")
    create.add_argument("--env-file", type=Path, default=setup.ROOT / ".env")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv

    load_dotenv()
    if args.command == "setup":
        token = os.environ.get("SUPABASE_ACCESS_TOKEN") or getpass.getpass("Supabase access token: ")
        try:
            setup.setup(token.strip(), project_ref=args.project_ref, organization=args.organization,
                        name=args.name, region=args.region, env_path=args.env_file)
        except setup.SetupError as error:
            print(f"Setup failed: {error}")
            return 1
        return 0
    return asyncio.run(upload_all(args.paths))


if __name__ == "__main__":
    raise SystemExit(main())
