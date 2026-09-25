#!/usr/bin/env python3
"""Local file mechanics for 听见别处. No model/API calls or publishing.

Supports:
- magicdub-cli task dirs (`state.json` + `exports/final.*`) under ~/Movies/MagicDub/cli/
- legacy MagicDub skill projects (`project.json`)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

VERSION = "0.1.0"
SKILL = "tingjianbiechu-publish-kit"
FOOTER = "MagicDub 魔法配音，非本人中文发言。"
ROLES = ("first_frame", "portrait", "landscape")
ALL = ("copy",) + ROLES
META_STATE = "state.json"
META_PROJECT = "project.json"


class KitError(Exception):
    pass


def obj(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise KitError(f"无法读取 JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise KitError(f"JSON 顶层必须是对象: {path}")
    return value


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inside(root, value, must_exist=True):
    p = Path(value).expanduser()
    p = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.is_relative_to(root):
        raise KitError(f"路径超出项目边界: {p}")
    if must_exist and not p.is_file():
        raise KitError(f"文件不存在: {p}")
    return p


def external_file(value):
    p = Path(value).expanduser().resolve()
    if not p.is_file():
        raise KitError(f"文件不存在: {p}")
    return p


def run(command):
    if not shutil.which(command[0]):
        raise KitError(f"缺少命令: {command[0]}")
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise KitError(f"{command[0]} 执行失败: {result.stderr.strip()[:800]}")
    return result.stdout


def dimensions(stream):
    w, h = int(stream["width"]), int(stream["height"])
    if w <= 0 or h <= 0:
        raise KitError("无效画面尺寸")
    sar = stream.get("sample_aspect_ratio", "1:1")
    if sar not in (None, "N/A", "0:1", "1:1"):
        try:
            a, b = map(int, sar.split(":"))
            if a <= 0 or b <= 0:
                raise ValueError()
            w = round(w * a / b)
        except (ValueError, ZeroDivisionError) as error:
            raise KitError(f"无效像素宽高比: {sar}") from error
    rotation = stream.get("tags", {}).get("rotate", 0)
    for item in stream.get("side_data_list", []):
        if "rotation" in item:
            rotation = item["rotation"]
    rotation = float(rotation)
    if not math.isfinite(rotation) or abs(rotation / 90 - round(rotation / 90)) > 0.001:
        raise KitError("非直角视频旋转，需人工确定实际显示尺寸")
    angle = round(rotation) % 360
    if angle in (90, 270):
        w, h = h, w
    if w <= 0 or h <= 0:
        raise KitError("像素宽高比导致无效显示尺寸")
    return {"width": w, "height": h, "rotation": angle,
            "coded_width": int(stream["width"]), "coded_height": int(stream["height"])}


def probe(path):
    data = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-show_streams", "-show_format", "-of", "json", str(path)]))
    if not data.get("streams"):
        raise KitError(f"没有可读取的画面: {path}")
    return dimensions(data["streams"][0])


def choose(root, explicit, pointer, candidates, label):
    if explicit:
        return inside(root, explicit)
    if pointer:
        if not isinstance(pointer, str):
            raise KitError(f"{label} 指针不是字符串；请显式指定文件")
        return inside(root, pointer)
    files = sorted({p.resolve() for p in candidates if p.is_file()})
    if len(files) != 1:
        raise KitError(f"无法唯一确定{label}；请显式指定。候选: {[str(p) for p in files]}")
    return inside(root, files[0])


def safe_stem(value):
    name = re.sub(r'[^\w.\-]+', '_', Path(value).name, flags=re.UNICODE).strip('._')[:100]
    if not name:
        raise KitError("无法确定素材文件名前缀")
    return name


def record_base(project, stem):
    return inside(project, Path("provenance") / SKILL / stem, must_exist=False)


def next_version(output, base, stem):
    versions = []
    pattern = re.compile(re.escape(stem) + r"_(?:发布文案|封面_.+)_v(\d+)\.(?:txt|png)$")
    for p in output.iterdir():
        m = pattern.fullmatch(p.name)
        if m:
            versions.append(int(m[1]))
    if base.is_dir():
        versions.extend(int(p.name[1:]) for p in base.iterdir()
                        if re.fullmatch(r"v\d+", p.name))
    return max(versions, default=0) + 1


def resolve_project_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if root.is_file() and root.name in (META_STATE, META_PROJECT):
        root = root.parent
    if (root / META_STATE).is_file() or (root / META_PROJECT).is_file():
        return root
    if (root.parent / META_STATE).is_file() or (root.parent / META_PROJECT).is_file():
        return root.parent
    raise KitError(f"未找到 {META_STATE} 或 {META_PROJECT}: {root}")


def load_project(root: Path) -> tuple[str, dict, Path]:
    """Return (engine, metadata, metadata_path). Prefer magicdub-cli state.json."""
    state_path = root / META_STATE
    project_path = root / META_PROJECT
    if state_path.is_file():
        return "magicdub-cli", obj(state_path), state_path
    if project_path.is_file():
        return "magicdub-legacy", obj(project_path), project_path
    raise KitError(f"未找到项目元数据: {root}")


def _asset_path(meta: dict, *keys: str) -> str | None:
    cur: object = meta
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    if isinstance(cur, dict):
        path = cur.get("path")
        return path if isinstance(path, str) else None
    return cur if isinstance(cur, str) else None


def inspect(args):
    root = resolve_project_root(args.project)
    engine, meta, meta_path = load_project(root)
    warnings: list[str] = []
    source_transcript = None

    if engine == "magicdub-cli":
        video_pointer = _asset_path(meta, "assets", "tgt", "final_video")
        subtitle_pointer = _asset_path(meta, "assets", "tgt", "srt")
        video = choose(root, args.video, video_pointer,
                       [root / "exports" / "final.mp4"], "最终成片")
        subtitle_candidates = ([video.with_suffix(".srt")] if video.with_suffix(".srt").is_file()
                               else list(video.parent.glob("*.srt")))
        subtitle = choose(root, args.subtitle, subtitle_pointer,
                          subtitle_candidates, "最终中文字幕")
        source_name = meta.get("title") or root.name
        project_id = meta.get("task_id")
        status = (meta.get("run") or {}).get("status")
        if status != "done":
            warnings.append(f"magicdub-cli 状态为 {status or 'unknown'}；不等于发布验收完成")
        transcript = (meta.get("assets") or {}).get("src", {}).get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            source_transcript = transcript.strip()
        else:
            warnings.append("state.json 无原文 transcript；读取中文字幕并如实说明双语核对范围")
    else:
        video = choose(root, args.video, meta.get("latest_export"),
                       (root / "exports").glob("*.mp4"), "最终成片")
        subtitle_candidates = ([video.with_suffix(".srt")] if video.with_suffix(".srt").is_file()
                               else list(video.parent.glob("*.srt")))
        subtitle = choose(root, args.subtitle, meta.get("latest_subtitle"),
                          subtitle_candidates, "最终中文字幕")
        source_name = meta.get("source_name") or Path(meta.get("source", video.name)).stem
        project_id = meta.get("project_id")
        if meta.get("status") != "completed":
            warnings.append(f"MagicDub 项目状态为 {meta.get('status', 'unknown')}；不等于发布验收完成")

    source_dir = Path(args.source_dir).expanduser().resolve() if args.source_dir else None
    if source_dir and not source_dir.is_dir():
        raise KitError(f"原素材目录不存在: {source_dir}")
    manifest_path = source_dir / "manifest.json" if source_dir else None
    manifest = obj(manifest_path) if manifest_path and manifest_path.exists() else {}
    title = manifest.get("title") if isinstance(manifest.get("title"), str) else None
    description = manifest.get("text") if isinstance(manifest.get("text"), str) else None
    if not title or not title.strip():
        warnings.append("缺少原 title，使用字幕／transcript 理解内容")
    if not description or not description.strip():
        warnings.append("缺少原 text，使用字幕／transcript 理解内容")
    stem = safe_stem(source_name)
    match = "not_checked"
    source_video = None
    if source_dir and isinstance(manifest.get("video_file"), str):
        source_video = inside(source_dir, manifest["video_file"], must_exist=False)
    legacy_sha = meta.get("source_sha256")
    cli_src_sha = None
    if engine == "magicdub-cli":
        src_video = (meta.get("assets") or {}).get("src", {}).get("video") or {}
        if isinstance(src_video, dict):
            cli_src_sha = src_video.get("sha256")
    if source_video and source_video.is_file() and (legacy_sha or cli_src_sha):
        expected = legacy_sha or cli_src_sha
        if digest(source_video) != expected:
            raise KitError("原素材视频与 MagicDub 记录的 source sha256 不匹配")
        match = "source_sha256"
    elif manifest.get("id"):
        video_id = str(manifest["id"])
        if source_name == video_id or str(source_name).endswith("-" + video_id):
            match = "video_id"
        elif str(source_name).startswith("youtube-"):
            raise KitError("manifest 视频 ID 与当前 MagicDub 项目不匹配")
    if source_dir and match == "not_checked":
        warnings.append("来源关联缺少哈希／ID 证据，需核对文件与内容后使用参考图")
    preview = external_file(args.preview) if args.preview else None
    if not preview and source_dir and isinstance(manifest.get("preview_file"), str):
        candidate = inside(source_dir, manifest["preview_file"], must_exist=False)
        if candidate.is_file():
            preview = candidate
    if not preview:
        warnings.append("没有可用预览图，可从当前视频选择代表画面")
    original = external_file(args.original_text) if args.original_text else None
    transcript_file = root / "transcript.json"
    if not original and transcript_file.is_file():
        original = transcript_file
    original_candidates = []
    if not original and source_dir:
        original_candidates = [p.resolve() for p in source_dir.glob("*.srt")
                               if p.resolve() != subtitle]
        if len(original_candidates) == 1:
            original = original_candidates[0]
    if not original and not source_transcript:
        warnings.append("未唯一确定原文；读取中文字幕并如实说明双语核对范围")
    size = probe(video)
    base = record_base(root, stem)
    return {
        "skill": SKILL, "skill_version": VERSION,
        "engine": engine,
        "project": str(root), "project_id": project_id, "stem": stem,
        "metadata_file": str(meta_path),
        "video": str(video), "subtitle": str(subtitle),
        "original_text": str(original) if original else None,
        "source_transcript": source_transcript,
        "original_text_candidates": [str(p) for p in original_candidates],
        "manifest": str(manifest_path) if manifest_path and manifest_path.exists() else None,
        "source_metadata": {"title": title, "text": description, "id": manifest.get("id"),
                            "url": manifest.get("post_url") or manifest.get("url")},
        "reference_image": str(preview) if preview else None,
        "source_match": match, "dimensions": size,
        "output_dir": str(video.parent), "suggested_version": next_version(video.parent, base, stem),
        "warnings": warnings,
    }


def clean_content(value):
    for key in ("title", "description"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise KitError(f"内容缺少非空 {key}")
    title = value["title"].strip()
    if "\n" in title or "\r" in title:
        raise KitError("发布标题必须为单行")
    description = value["description"].strip()
    if "AI配音" in description or "AI译制配音" in description:
        raise KitError("作品描述应使用魔法配音口径")
    if FOOTER in description:
        if description.count(FOOTER) != 1 or not description.endswith(FOOTER):
            raise KitError("魔法配音落款只应出现在描述末尾一次")
    else:
        description += "\n\n" + FOOTER
    tags = value.get("hashtags")
    if not isinstance(tags, list) or not tags:
        raise KitError("hashtags 必须为非空字符串数组")
    normalized = []
    for tag in tags:
        if not isinstance(tag, str):
            raise KitError("话题必须为字符串")
        tag = "#" + tag.strip().lstrip("#")
        if not re.fullmatch(r"#[^\s#]+", tag):
            raise KitError(f"无效话题: {tag}")
        if tag not in normalized:
            normalized.append(tag)
    return {**value, "title": title, "description": description, "hashtags": normalized}


def copy_text(content):
    return (f"标题：\n{content['title']}\n\n描述：\n{content['description']}\n\n"
            f"话题标签：\n{' '.join(content['hashtags'])}\n")


def filenames(info, version):
    d, stem = info["dimensions"], info["stem"]
    return {
        "copy": f"{stem}_发布文案_v{version}.txt",
        "first_frame": f"{stem}_封面_视频首帧_{d['width']}x{d['height']}_v{version}.png",
        "portrait": f"{stem}_封面_列表竖版_3比4_1080x1440_v{version}.png",
        "landscape": f"{stem}_封面_列表横版_4比3_1440x1080_v{version}.png",
    }


def target_sizes(info):
    return {"first_frame": (info["dimensions"]["width"], info["dimensions"]["height"]),
            "portrait": (1080, 1440), "landscape": (1440, 1080)}


def new_file(path, data):
    """Atomic publication without replacing any existing file."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".publish-kit-", delete=False) as f:
        temporary = Path(f.name)
        f.write(data)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def png_bytes(source, width, height):
    if source.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        raise KitError("封面输入必须为 PNG、JPEG 或 WebP 图片")
    actual = probe(source)
    error = abs(actual["width"] / actual["height"] / (width / height) - 1)
    if error > 0.01:
        raise KitError(f"图片比例与 {width}×{height} 相差超过 1%，请用生图工具重新构图: {source}")
    with source.open("rb") as f:
        is_png = f.read(8) == b"\x89PNG\r\n\x1a\n"
    if (is_png and (actual["coded_width"], actual["coded_height"]) == (width, height)
            and (actual["width"], actual["height"]) == (width, height) and actual["rotation"] == 0):
        return source.read_bytes()
    with tempfile.TemporaryDirectory(prefix="publish-kit-resample-") as work:
        target = Path(work) / "cover.png"
        run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-frames:v", "1",
             "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1", str(target)])
        final = probe(target)
        if (final["width"], final["height"]) != (width, height):
            raise KitError("PNG 导出尺寸错误")
        return target.read_bytes()


def deliver(args):
    info = inspect(args)
    content = clean_content(obj(args.content))
    selected = tuple(args.only or ALL)
    if len(set(selected)) != len(selected):
        raise KitError("--only 不能重复指定角色")
    sizes = target_sizes(info)
    reviewed = content.get("reviewed_covers", [])
    prompts = content.get("prompts", {})
    if not isinstance(reviewed, list) or any(role not in ROLES for role in reviewed):
        raise KitError("reviewed_covers 必须是实际查看过的图片角色数组")
    if args.version is not None and args.version < 1:
        raise KitError("版本号必须为正整数")
    payloads = {}
    generated_sources = {}
    if "copy" in selected:
        payloads["copy"] = copy_text(content).encode("utf-8")
    for role in ROLES:
        if role not in selected:
            continue
        value = getattr(args, role)
        if not value:
            raise KitError(f"缺少图片 --{role.replace('_', '-')}")
        if role not in reviewed or not isinstance(prompts, dict) or not isinstance(prompts.get(role), str) or not prompts[role].strip():
            raise KitError(f"{role} 需要完整提示词与真实目视检查声明；不能用程序检查代替目视")
        source = external_file(value)
        payloads[role] = png_bytes(source, *sizes[role])
        generated_sources[role] = {"path": str(source), "sha256": digest(source)}
    root, output = Path(info["project"]), Path(info["output_dir"])
    input_files = {key: {"path": info[key], "sha256": digest(info[key])}
                   for key in ("video", "subtitle", "original_text", "manifest", "reference_image")
                   if info.get(key)}
    meta_file = info.get("metadata_file") or str(root / META_PROJECT)
    input_files["project_metadata"] = {"path": meta_file, "sha256": digest(meta_file)}
    base = record_base(root, info["stem"])
    base.mkdir(parents=True, exist_ok=True)
    version = args.version if args.version is not None else next_version(output, base, info["stem"])
    names = filenames(info, version)
    if any((output / name).exists() for name in names.values()):
        raise KitError(f"v{version} 已有成品，请使用新版本")
    folder = base / f"v{version}"
    try:
        folder.mkdir()
    except FileExistsError as error:
        raise KitError(f"v{version} 已有记录，请使用新版本") from error
    record = {"schema_version": 1, "skill": SKILL, "skill_version": VERSION,
              "created_at": datetime.now(timezone.utc).isoformat(), "version": version,
              "scope": "full" if set(selected) == set(ALL) else "partial_update",
              "inspection": info, "input_files": input_files,
              "content": content, "generated_sources": generated_sources,
              "files": {}, "status": "writing"}
    try:
        for role, data in payloads.items():
            target = output / names[role]
            new_file(target, data)
            record["files"][role] = {"path": str(target.relative_to(root)), "sha256": digest(target)}
            if role in sizes:
                record["files"][role]["dimensions"] = list(sizes[role])
        record["status"] = "delivered"
        new_file(folder / "record.json", (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except Exception as error:
        record.update(status="failed", error=str(error))
        if not (folder / "record.json").exists():
            new_file(folder / "record.json", (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        raise
    return {"status": record["status"], "scope": record["scope"], "version": version,
            "record": str(folder / "record.json"),
            "files": {k: str(root / v["path"]) for k, v in record["files"].items()}}


def verify(path):
    path = external_file(path)
    record = obj(path)
    if record.get("skill") != SKILL or record.get("schema_version") != 1 or record.get("status") != "delivered":
        raise KitError("这不是已交付的本技能记录")
    if len(path.parents) < 5 or path.parents[2].name != SKILL or path.parents[3].name != "provenance":
        raise KitError("记录不在预期的项目 provenance 目录内")
    root = path.parents[4]
    files = record.get("files", {})
    if record.get("scope") not in ("full", "partial_update"):
        raise KitError("未知交付范围")
    if not files or (record.get("scope") == "full" and set(files) != set(ALL)):
        raise KitError("交付文件不完整")
    expected_sizes = target_sizes(record["inspection"])
    for role, item in files.items():
        if role not in ALL:
            raise KitError(f"未知交付角色: {role}")
        p = inside(root, item["path"])
        if digest(p) != item["sha256"]:
            raise KitError(f"成品内容已变化: {p}")
        if role in ROLES:
            size = probe(p)
            if ([size["width"], size["height"]] != item["dimensions"]
                    or tuple(item["dimensions"]) != expected_sizes[role]):
                raise KitError(f"图片尺寸不匹配: {p}")
        else:
            text = p.read_text(encoding="utf-8")
            if text.count(FOOTER) != 1:
                raise KitError("发布文案缺少唯一的魔法配音落款")
    return {"status": "file_checks_passed", "scope": record["scope"],
            "files_checked": len(files), "visual_review": "agent_attestation_in_record",
            "note": "文件检查不证明视觉效果、流量或平台发布状态"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "deliver"):
        p = sub.add_parser(name)
        p.add_argument("--project", required=True)
        for flag in ("source-dir", "video", "subtitle", "original-text", "preview"):
            p.add_argument("--" + flag)
        if name == "deliver":
            p.add_argument("--content", required=True)
            for role in ROLES:
                p.add_argument("--" + role.replace("_", "-"))
            p.add_argument("--only", nargs="+", choices=ALL)
            p.add_argument("--version", type=int)
    sub.add_parser("verify").add_argument("--record", required=True)
    args = parser.parse_args()
    try:
        result = inspect(args) if args.command == "inspect" else deliver(args) if args.command == "deliver" else verify(args.record)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (KitError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
