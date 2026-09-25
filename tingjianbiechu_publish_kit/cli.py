"""CLI entry: tjbc-publish."""
from __future__ import annotations

import json
import sys

from . import __version__
from .clients import ClientError
from .file_kit import KitError, deliver, inspect, verify
from .pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="tjbc-publish",
        description="听见别处：为 MagicDub 成片生成抖音发布文案与三比例封面",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="一条命令生成文案+三封面+花费账本")
    run_p.add_argument("--project", required=True, help="magicdub-cli 任务目录")
    run_p.add_argument(
        "--source-dir",
        help="原素材下载目录；默认尝试 ~/Downloads/<与任务同名的前缀>",
    )
    run_p.add_argument("--preview", help="可选人物参考图")

    for name in ("inspect", "deliver"):
        p = sub.add_parser(name, help="定位成片与素材" if name == "inspect" else "落盘文案与封面")
        p.add_argument("--project", required=True, help="magicdub-cli 任务目录或 legacy project 目录")
        for flag in ("source-dir", "video", "subtitle", "original-text", "preview"):
            p.add_argument("--" + flag)
        if name == "deliver":
            p.add_argument("--content", required=True, help="文案与提示词 JSON")
            for role in ("first_frame", "portrait", "landscape"):
                p.add_argument("--" + role.replace("_", "-"))
            p.add_argument("--only", nargs="+", choices=("copy", "first_frame", "portrait", "landscape"))
            p.add_argument("--version", type=int)

    sub.add_parser("verify", help="校验 provenance 记录与成品").add_argument("--record", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run_pipeline(
                project=args.project,
                source_dir=args.source_dir,
                preview=args.preview,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if result.get("cost_report"):
                print(result["cost_report"], file=sys.stderr)
            return int(result.get("exit_hint") or 0)
        if args.command == "inspect":
            result = inspect(args)
        elif args.command == "deliver":
            result = deliver(args)
        else:
            result = verify(args.record)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (KitError, ClientError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
