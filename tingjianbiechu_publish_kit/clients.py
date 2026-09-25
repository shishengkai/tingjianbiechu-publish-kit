"""HTTP clients for DeepSeek and DashScope (chat / VL / OCR / image)."""
from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx

from .config import load_models_config
from .costing import CostLedger
from .credentials import get_credential


class ClientError(RuntimeError):
    pass


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            softened = (
                blob.replace("\u201c", "「")
                .replace("\u201d", "」")
                .replace("\u2018", "『")
                .replace("\u2019", "』")
            )
            # Replace bare ASCII quotes inside Chinese sentences is unsafe;
            # only retry curly-quote softening.
            try:
                parsed = json.loads(softened)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    raise ClientError(f"returned non-JSON: {content[:400]}")


def _provider(cfg: dict, name: str) -> dict:
    providers = cfg.get("providers") or {}
    if name not in providers:
        raise ClientError(f"unknown provider: {name}")
    return providers[name]


def _api_key(provider: dict) -> str:
    env_name = provider["api_key_env"]
    key = get_credential(env_name)
    if not key:
        raise ClientError(f"missing credential {env_name}")
    return key


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    raw = path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


class ModelClients:
    def __init__(self, *, models_cfg: dict | None = None, ledger: CostLedger | None = None):
        self.cfg = models_cfg if models_cfg is not None else load_models_config()
        self.ledger = ledger if ledger is not None else CostLedger()
        self._http = httpx.Client(timeout=httpx.Timeout(300.0, connect=30.0))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ModelClients":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _role(self, role: str) -> dict:
        roles = self.cfg.get("roles") or {}
        if role not in roles:
            raise ClientError(f"unknown role: {role}")
        return roles[role]

    def chat_json(
        self,
        *,
        role: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.4,
        max_tokens: int = 2048,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Call OpenAI-compatible chat; parse assistant JSON object."""
        spec = self._role(role)
        provider = _provider(self.cfg, spec["provider"])
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": spec["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {_api_key(provider)}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(url, headers=headers, json=body)
        data = resp.json()
        if resp.status_code >= 400:
            raise ClientError(f"{role} HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:500]}")
        self.ledger.add_response(role=role, model=data.get("model") or spec["model"], response=data)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ClientError(f"{role} missing message content") from exc
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        try:
            parsed = _parse_json_object(str(content))
        except ClientError as exc:
            raise ClientError(f"{role} {exc}") from exc
        return parsed, data

    def vision_text(
        self,
        *,
        role: str,
        prompt: str,
        image_path: Path,
        max_tokens: int = 512,
    ) -> tuple[str, dict[str, Any]]:
        spec = self._role(role)
        provider = _provider(self.cfg, spec["provider"])
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": spec["model"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": _data_url(image_path)}},
                    {"type": "text", "text": prompt},
                ],
            }],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {_api_key(provider)}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(url, headers=headers, json=body)
        data = resp.json()
        if resp.status_code >= 400:
            raise ClientError(f"{role} HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:500]}")
        self.ledger.add_response(role=role, model=data.get("model") or spec["model"], response=data)
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return str(content).strip(), data

    def generate_image(
        self,
        *,
        prompt: str,
        size: str,
        reference_images: list[Path] | None = None,
        negative_prompt: str | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        """Generate one image via DashScope multimodal API; return saved temp path + response."""
        spec = self._role("image")
        provider = _provider(self.cfg, spec["provider"])
        url = provider["base_url"].rstrip("/") + "/services/aigc/multimodal-generation/generation"
        content: list[dict[str, Any]] = []
        for path in reference_images or []:
            content.append({"image": _data_url(path)})
        content.append({"text": prompt})
        body: dict[str, Any] = {
            "model": spec["model"],
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": {
                "size": size,
                "n": 1,
                "prompt_extend": bool(spec.get("prompt_extend", True)),
                "watermark": bool(spec.get("watermark", False)),
            },
        }
        if spec.get("prompt_extend_mode"):
            body["parameters"]["prompt_extend_mode"] = str(spec["prompt_extend_mode"])
        if "enable_thinking" in spec:
            body["parameters"]["enable_thinking"] = bool(spec["enable_thinking"])
        if negative_prompt:
            body["parameters"]["negative_prompt"] = negative_prompt
        headers = {
            "Authorization": f"Bearer {_api_key(provider)}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(url, headers=headers, json=body)
        data = resp.json()
        if resp.status_code >= 400:
            raise ClientError(f"image HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:800]}")
        self.ledger.add_response(role="image", model=spec["model"], response=data)
        image_url = _find_image_url(data)
        if not image_url:
            raise ClientError(f"image response missing url: {json.dumps(data, ensure_ascii=False)[:500]}")
        img_resp = self._http.get(image_url)
        if img_resp.status_code >= 400:
            raise ClientError(f"download generated image failed: HTTP {img_resp.status_code}")
        import os
        import tempfile

        fd, name = tempfile.mkstemp(prefix="tjbc-cover-", suffix=".png")
        try:
            os.write(fd, img_resp.content)
        finally:
            os.close(fd)
        return Path(name), data

    def soft_qa_json(
        self,
        *,
        reference: Path,
        cover: Path,
        prompt: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        spec = self._role("soft_qa")
        provider = _provider(self.cfg, spec["provider"])
        url = provider["base_url"].rstrip("/") + "/chat/completions"
        body = {
            "model": spec["model"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _data_url(reference)}},
                        {"type": "image_url", "image_url": {"url": _data_url(cover)}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": 400,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        # Prepend system via separate message for models that support it
        body["messages"].insert(0, {"role": "system", "content": (
            "你是封面质检员。只输出 JSON："
            '{"pass":true/false,"face_ok":true/false,"extra_text":true/false,'
            '"blocks_face":true/false,"thumbnail_ok":true/false,"issues":[]}'
        )})
        headers = {
            "Authorization": f"Bearer {_api_key(provider)}",
            "Content-Type": "application/json",
        }
        resp = self._http.post(url, headers=headers, json=body)
        data = resp.json()
        if resp.status_code >= 400:
            raise ClientError(f"soft_qa HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:500]}")
        self.ledger.add_response(role="soft_qa", model=data.get("model") or spec["model"], response=data)
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ClientError(f"soft_qa non-JSON: {content[:300]}") from exc
        if not isinstance(parsed, dict):
            raise ClientError("soft_qa JSON root must be object")
        return parsed, data


def _find_image_url(data: dict[str, Any]) -> str | None:
    try:
        content = data["output"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                for key in ("image", "url", "image_url"):
                    value = item.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        return value
                    if isinstance(value, dict) and isinstance(value.get("url"), str):
                        return value["url"]
    return None
