"""End-to-end publish pipeline for v0.1."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from argparse import Namespace
from pathlib import Path
from typing import Any

from .clients import ClientError, ModelClients
from .config import load_models_config
from .costing import CostLedger
from .file_kit import KitError, deliver, inspect
from .prompts import (
    COPY_SYSTEM,
    IMAGE_PROMPT_SYSTEM,
    OCR_PROMPT,
    build_copy_user,
    build_image_prompt_user,
    build_soft_qa_user,
)
from .qa import ocr_matches_cover_title, soft_qa_passed

ROLES = ("first_frame", "portrait", "landscape")
NEGATIVE_DEFAULT = (
    "stock photo, screenshot, flat caption, collage, hexagon inset, montage, "
    "extra text, watermark, logo, subtitle, account name, 听见别处, 魔法配音, "
    "AI配音, UI, blurry characters, deformed face, english gibberish, "
    "latin letters spam, news lower-third, phone UI"
)


def infer_source_dir(project: Path, explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        return path if path.is_dir() else None
    name = project.name
    match = re.match(r"(.+)_\d{8}_\d{6}$", name)
    base = match.group(1) if match else name
    candidate = Path.home() / "Downloads" / base
    return candidate if candidate.is_dir() else None


def read_text_file(path: Path, limit: int = 20000) -> str:
    return path.read_text(encoding="utf-8-sig")[:limit]


def extract_preview_frame(video: Path, work: Path) -> Path:
    out = work / "preview_frame.jpg"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-ss", "1", "-i", str(video),
            "-frames:v", "1", "-q:v", "2", str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if not out.is_file():
        raise KitError("无法从成片抽取预览帧")
    return out


def _size_str(width: int, height: int) -> str:
    return f"{width}*{height}"


def run_pipeline(
    *,
    project: str,
    source_dir: str | None = None,
    preview: str | None = None,
) -> dict[str, Any]:
    cfg = load_models_config()
    qa_cfg = cfg.get("qa") or {}
    max_retries = int(qa_cfg.get("max_image_retries", 2))
    ledger = CostLedger()
    project_path = Path(project).expanduser().resolve()
    source = infer_source_dir(project_path, source_dir)

    inspect_args = Namespace(
        project=str(project_path),
        source_dir=str(source) if source else None,
        video=None,
        subtitle=None,
        original_text=None,
        preview=preview,
    )
    info = inspect(inspect_args)
    if not info.get("reference_image"):
        # Fall back to a frame from the dubbed video so I2I has a face anchor.
        pass

    zh_srt = read_text_file(Path(info["subtitle"]))
    meta = info.get("source_metadata") or {}

    work = Path(tempfile.mkdtemp(prefix="tjbc-run-"))
    try:
        with ModelClients(models_cfg=cfg, ledger=ledger) as clients:
            copy_obj, _ = clients.chat_json(
                role="copy",
                messages=[
                    {"role": "system", "content": COPY_SYSTEM},
                    {
                        "role": "user",
                        "content": build_copy_user(
                            zh_srt=zh_srt,
                            source_title=meta.get("title"),
                            source_text=meta.get("text"),
                            source_transcript=info.get("source_transcript"),
                        ),
                    },
                ],
                temperature=0.7,
                max_tokens=4096,
            )
            title = str(copy_obj.get("title") or "").strip()
            description = str(copy_obj.get("description") or "").strip()
            cover_title = str(copy_obj.get("cover_title") or title).strip()
            hashtags = copy_obj.get("hashtags") or []
            if not title or not description or not cover_title:
                raise KitError("文案模型未返回完整 title/description/cover_title")
            if not isinstance(hashtags, list) or not hashtags:
                raise KitError("文案模型未返回 hashtags")
            hashtags = [str(t).lstrip("#").strip() for t in hashtags if str(t).strip()]

            dims = info["dimensions"]
            prompt_obj, _ = clients.chat_json(
                role="image_prompt",
                messages=[
                    {"role": "system", "content": IMAGE_PROMPT_SYSTEM},
                    {
                        "role": "user",
                        "content": build_image_prompt_user(
                            title=title,
                            cover_title=cover_title,
                            description=description,
                            width=dims["width"],
                            height=dims["height"],
                        ),
                    },
                ],
                max_tokens=4096,
            )
            prompts = {
                role: str(prompt_obj.get(role) or "").strip()
                for role in ROLES
            }
            if any(not prompts[r] for r in ROLES):
                raise KitError("生图提示词不完整")
            negative = str(prompt_obj.get("negative_prompt") or NEGATIVE_DEFAULT)

            ref = Path(info["reference_image"]) if info.get("reference_image") else None
            if ref is None or not ref.is_file():
                ref = extract_preview_frame(Path(info["video"]), work)

            sizes = {
                "first_frame": _size_str(dims["width"], dims["height"]),
                "portrait": "1080*1440",
                "landscape": "1440*1080",
            }

            cover_paths: dict[str, Path] = {}
            generation_history: list[dict[str, Any]] = []
            ocr_notes: dict[str, Any] = {}

            # Generate master first_frame with retries on OCR
            master = _generate_with_ocr(
                clients=clients,
                role="first_frame",
                prompt=prompts["first_frame"],
                size=sizes["first_frame"],
                refs=[ref],
                cover_title=cover_title,
                negative=negative,
                max_retries=max_retries,
                work=work,
                history=generation_history,
                ocr_notes=ocr_notes,
            )
            cover_paths["first_frame"] = master

            for role in ("portrait", "landscape"):
                cover_paths[role] = _generate_with_ocr(
                    clients=clients,
                    role=role,
                    prompt=prompts[role],
                    size=sizes[role],
                    refs=[master, ref],
                    cover_title=cover_title,
                    negative=negative,
                    max_retries=max_retries,
                    work=work,
                    history=generation_history,
                    ocr_notes=ocr_notes,
                )

            soft_results: dict[str, Any] = {}
            soft_fail = False
            for role, path in cover_paths.items():
                soft_results[role] = _soft_qa(
                    clients=clients,
                    role=role,
                    cover_path=path,
                    reference=ref,
                    cover_title=cover_title,
                )
                if not soft_qa_passed(soft_results[role]):
                    soft_fail = True

            content = {
                "title": title,
                "description": description,
                "hashtags": hashtags,
                "cover_title": cover_title,
                "prompts": prompts,
                "reviewed_covers": list(ROLES),
                "editorial_notes": str(copy_obj.get("editorial_notes") or ""),
                "visual_review_notes": {
                    "ocr": ocr_notes,
                    "soft_qa": soft_results,
                    "method": "programmatic OCR + qwen-vl-max",
                },
                "generation_history": generation_history,
                "costs": ledger.summary(),
            }
            if soft_fail:
                content["needs_human_review"] = True
                content["review_reasons"] = {
                    role: soft_results[role].get("issues")
                    for role in ROLES
                    if not soft_qa_passed(soft_results[role])
                }

            content_path = work / "content.json"
            content_path.write_text(
                json.dumps(content, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            deliver_args = Namespace(
                project=str(project_path),
                source_dir=str(source) if source else None,
                video=None,
                subtitle=None,
                original_text=None,
                preview=str(ref) if ref else None,
                content=str(content_path),
                first_frame=str(cover_paths["first_frame"]),
                portrait=str(cover_paths["portrait"]),
                landscape=str(cover_paths["landscape"]),
                only=None,
                version=None,
            )
            delivered = deliver(deliver_args)

            # Attach costs into provenance record
            record_path = Path(delivered["record"])
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["costs"] = ledger.summary()
            if soft_fail:
                record["needs_human_review"] = True
            record_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            result = {
                "status": "delivered",
                "needs_human_review": soft_fail,
                "exit_hint": 1 if soft_fail else 0,
                "title": title,
                "cover_title": cover_title,
                "files": delivered.get("files"),
                "record": str(record_path),
                "version": delivered.get("version"),
                "costs": ledger.summary(),
                "cost_report": ledger.format_report(),
                "source_dir": str(source) if source else None,
                "soft_qa": soft_results,
                "ocr": ocr_notes,
            }
            return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _generate_with_ocr(
    *,
    clients: ModelClients,
    role: str,
    prompt: str,
    size: str,
    refs: list[Path],
    cover_title: str,
    negative: str,
    max_retries: int,
    work: Path,
    history: list[dict[str, Any]],
    ocr_notes: dict[str, Any],
) -> Path:
    last_error = None
    last_ocr = ""
    attempts = max_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            use_prompt = prompt
            use_refs = refs
            if attempt > 1:
                correction = ""
                if last_ocr:
                    correction = (
                        f" Previous render was OCR-read as 「{last_ocr}」 which is WRONG; "
                        f"redraw so glyphs match 「{cover_title}」 character-by-character."
                    )
                use_prompt = (
                    f"{prompt}\nCRITICAL: the ONLY readable Chinese text must be exactly 「{cover_title}」."
                    f"{correction} "
                    "Keep premium AIGC editorial impact: cinematic rim light, hyperreal materials, "
                    "designed typography — not a flat photo with white caption. "
                    "No violence, no political symbols, no maps with disputed borders."
                )
            if attempt >= 3:
                # Drop secondary refs; keep AIGC impact while simplifying composition for safety/OCR.
                use_refs = refs[:1]
                use_prompt = (
                    f"Premium AIGC Douyin editorial cover, exact size {size}. "
                    f"Hyperreal cinematic portrait of the speaker from the reference, "
                    f"dramatic rim light, rich depth, cohesive color grade. "
                    f"Bold designed Chinese headline only, glyph-exact: 「{cover_title}」. "
                    "No screenshot look, no stock collage, no extra text, logos, watermarks, "
                    "flags, maps, or UI."
                )
                if last_ocr:
                    use_prompt += f" Do not render the wrong text 「{last_ocr}」."
            path, raw = clients.generate_image(
                prompt=use_prompt,
                size=size,
                reference_images=use_refs,
                negative_prompt=negative + ", violence, blood, nsfw, political symbol, flag",
            )
        except ClientError as exc:
            last_error = exc
            history.append({"role": role, "attempt": attempt, "error": str(exc)})
            # Content inspection failures are worth another attempt with safer prompt.
            if "DataInspectionFailed" not in str(exc) and "Green net" not in str(exc):
                if attempt >= attempts:
                    break
            continue
        stable = work / f"{role}_v{attempt}.png"
        shutil.copy2(path, stable)
        Path(path).unlink(missing_ok=True)
        ocr_text, _ = clients.vision_text(role="ocr", prompt=OCR_PROMPT, image_path=stable)
        matched = ocr_matches_cover_title(ocr_text, cover_title)
        history.append({
            "role": role,
            "attempt": attempt,
            "ocr_text": ocr_text,
            "ocr_match": matched,
            "path": str(stable),
        })
        ocr_notes[role] = {"text": ocr_text, "match": matched, "attempts": attempt}
        if matched:
            return stable
        last_ocr = ocr_text
        last_error = KitError(f"{role} OCR mismatch: {ocr_text!r} vs {cover_title!r}")
    raise last_error or KitError(f"{role} image generation failed")


def _soft_qa(
    *,
    clients: ModelClients,
    role: str,
    cover_path: Path,
    reference: Path,
    cover_title: str,
) -> dict[str, Any]:
    try:
        parsed, _ = clients.soft_qa_json(
            reference=reference,
            cover=cover_path,
            prompt=build_soft_qa_user(cover_title=cover_title, role=role),
        )
        # Normalize through parse_soft_qa fields
        return {
            "pass": bool(parsed.get("pass")),
            "face_ok": bool(parsed.get("face_ok", True)),
            "extra_text": bool(parsed.get("extra_text", False)),
            "blocks_face": bool(parsed.get("blocks_face", False)),
            "thumbnail_ok": bool(parsed.get("thumbnail_ok", True)),
            "aigc_impact": parsed.get("aigc_impact"),
            "issues": list(parsed.get("issues") or []),
        }
    except Exception as exc:
        return {
            "pass": False,
            "face_ok": True,
            "extra_text": False,
            "blocks_face": False,
            "thumbnail_ok": True,
            "issues": [f"soft QA failed: {exc}"],
        }
