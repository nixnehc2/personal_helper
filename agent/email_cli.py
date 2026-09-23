"""Local-only email commands. No mailbox credentials or transport required."""
import argparse
from dataclasses import asdict
import getpass
import json
import os
from pathlib import Path

from .email_parser import parse_email
from .email_workflow import draft_email, ingest_email, initialize_test_memory, learn_from_edit
from .llm import Client, load_config
from .main import confirm_batch, safe_display
from .tools import FileTools


def main():
    parser = argparse.ArgumentParser(description="Local Email V1 (never sends mail)")
    parser.add_argument("--root", type=Path, default=Path("tmp/test_memory"))
    commands = parser.add_subparsers(dest="command", required=True)
    parse = commands.add_parser("parse")
    parse.add_argument("path", type=Path)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--authored-by-user", action="store_true", help="Only use for emails actually written/approved by you")
    ingest.add_argument("--reprocess", action="store_true", help="Explicitly retry archived mail after a failed processing attempt")
    draft = commands.add_parser("draft")
    draft.add_argument("--request", required=True)
    draft.add_argument("--email", type=Path)
    draft.add_argument("--output", type=Path, default=Path("tmp/draft.json"))
    learn = commands.add_parser("learn")
    learn.add_argument("--draft", required=True, type=Path)
    learn.add_argument("--final", required=True, type=Path, help="UTF-8 file containing your edited final body")
    args = parser.parse_args()
    files = None
    try:
        if args.command == "parse":
            print(safe_display(json.dumps(asdict(parse_email(args.path)), ensure_ascii=False, indent=2)))
            return 0
        initialize_test_memory(args.root)
        config = load_config()
        token = config.get("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_AUTH_TOKEN") or getpass.getpass("API token: ")
        if not token:
            raise ValueError("API token required")
        client = Client(token, config)
        files = FileTools(args.root, confirm_batch)
        if args.command == "ingest":
            result = ingest_email(args.path, client, files, authored_by_user=args.authored_by_user, reprocess=args.reprocess)
        elif args.command == "draft":
            if args.output.resolve().is_relative_to(args.root.resolve()):
                raise ValueError("draft artifact must stay outside memory root")
            if args.output.exists():
                raise ValueError("output exists; select a new --output path")
            result = draft_email(args.request, client, args.root, incoming_path=args.email)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            print("草稿已保存，未发送：" + str(args.output))
        else:
            draft = json.loads(args.draft.read_text(encoding="utf-8-sig"))
            final = args.final.read_text(encoding="utf-8-sig")
            # Preserve both versions in one local artifact, outside memory.
            record = args.draft.with_name(args.draft.stem + ".learning.json")
            if record.resolve().is_relative_to(args.root.resolve()):
                raise ValueError("learning artifact must stay outside memory root")
            with record.open("x", encoding="utf-8") as stream:
                json.dump(dict(agent_draft=draft, user_final_text=final), stream, ensure_ascii=False, indent=2)
            result = learn_from_edit(draft, final, client, files)
        print(safe_display(json.dumps(result, ensure_ascii=False, indent=2)))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print("操作未完成：" + safe_display(str(error)))
        if files is not None:
            print("已写入（不会自动回滚）：" + safe_display(", ".join(files.writes) or "无"))
            print("未提交 Temporary 已保留；下一次命令启动会从 Formal Memory 重新初始化。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
