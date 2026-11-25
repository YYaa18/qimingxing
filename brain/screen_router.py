from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from brain.config import AppConfig, OCRConfig, ProfileConfig, is_local_provider
from brain.llm_router import LLMError, LLMUsage, estimate_cny_cost
from protocol.models import ContextScreenRefPayload

logger = logging.getLogger("qimingxing.screen_router")


class OCRProvider(ABC):
    """Pluggable cloud/local OCR interface for text-only LLM fallback."""

    @abstractmethod
    async def extract_text(self, image_bytes: bytes, content_type: str = "image/jpeg") -> str:
        """Extract readable text from compressed screenshot bytes."""


class DeepSeekOCRProvider(OCRProvider):
    """Cloud OCR provider calling DeepSeek POST /v1/tools/ocr multipart endpoint."""

    def __init__(self, ocr_config: OCRConfig, config: AppConfig) -> None:
        self._ocr_cfg = ocr_config
        self._app_cfg = config

    async def extract_text(self, image_bytes: bytes, content_type: str = "image/jpeg") -> str:
        if not self._ocr_cfg.api_key:
            raise LLMError(
                "MISSING_OCR_API_KEY",
                "未配置云端 OCR API Key（DEEPSEEK_API_KEY），无法对屏幕截图进行文字识别。",
            )

        url = f"{self._ocr_cfg.base_url}/tools/ocr"
        headers = {"Authorization": f"Bearer {self._ocr_cfg.api_key}"}
        ext = "png" if "png" in content_type.lower() else "jpg"
        timeout = httpx.Timeout(self._ocr_cfg.timeout_seconds, connect=8.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    headers=headers,
                    files={"file": (f"screenshot.{ext}", image_bytes, content_type)},
                    data={"purpose": self._ocr_cfg.mode or "text_recognition"},
                )
        except httpx.TimeoutException as exc:
            safe = self._app_cfg.redact_secrets(f"DeepSeek OCR 请求超时: {exc}")
            raise LLMError("OCR_TIMEOUT", safe) from exc
        except httpx.HTTPError as exc:
            safe = self._app_cfg.redact_secrets(f"DeepSeek OCR 网络错误: {exc}")
            raise LLMError("OCR_NETWORK_ERROR", safe) from exc

        if resp.status_code >= 400:
            safe = self._app_cfg.redact_secrets(resp.text[:300])
            raise LLMError(
                f"OCR_HTTP_{resp.status_code}",
                f"DeepSeek OCR 接口返回 HTTP {resp.status_code}: {safe}",
            )

        data = resp.json()
        pages = data.get("pages") or []
        texts = [str(p.get("text", "")).strip() for p in pages if p.get("text")]
        return "\n\n".join(texts).strip()


class ScreenUnderstandingRouter:
    """Dual-path screen understanding router: Vision direct vs Vision/OCR slot fallback."""

    def __init__(
        self,
        ocr_provider: OCRProvider,
        *,
        config: Optional[AppConfig] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.ocr_provider = ocr_provider
        self._app_config = config
        self._transport = transport
        self.last_ocr_usage: Optional[LLMUsage] = None

    def reload_config(self, config: AppConfig) -> None:
        self._app_config = config
        if hasattr(self.ocr_provider, "config"):
            self.ocr_provider.config = config.ocr  # type: ignore[attr-defined]
        if hasattr(self.ocr_provider, "_ocr_cfg"):
            self.ocr_provider._ocr_cfg = config.ocr  # type: ignore[attr-defined]
        if hasattr(self.ocr_provider, "_app_cfg"):
            self.ocr_provider._app_cfg = config  # type: ignore[attr-defined]

    async def _call_vision_slot(
        self,
        vision_prof: ProfileConfig,
        image_bytes: bytes,
        content_type: str,
        window_desc: str,
    ) -> str:
        if "ocr" in (vision_prof.model or "").lower():
            ocr_cfg = OCRConfig(
                provider=vision_prof.provider_id or "deepseek_ocr",
                base_url=vision_prof.base_url,
                api_key=vision_prof.api_key,
            )
            assert self._app_config is not None
            ocr_helper = DeepSeekOCRProvider(ocr_cfg, self._app_config)
            text = await ocr_helper.extract_text(image_bytes, content_type=content_type)
            self.last_ocr_usage = LLMUsage(
                model=vision_prof.model,
                profile=vision_prof.name,
                provider=vision_prof.provider_id or vision_prof.provider,
                slot="vision_ocr",
                estimated_cost_cny=0.001,
            )
            return text

        api_key = vision_prof.api_key
        if not api_key and is_local_provider(vision_prof.provider_id, vision_prof.base_url, vision_prof.provider):
            api_key = "ollama"
        if not api_key or not vision_prof.base_url:
            raise LLMError("MISSING_VISION_API_KEY", "未配置视觉与 OCR (vision_ocr) 槽位服务商密钥或地址。")

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{content_type};base64,{b64}"
        url = f"{vision_prof.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": vision_prof.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"请精确提取并简要描述这张截图的核心文字与界面状态（{window_desc}），供主对话模型推理使用。",
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "stream": False,
        }
        client_kwargs: Dict[str, Any] = {"timeout": httpx.Timeout(25.0, connect=8.0)}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                safe = (
                    self._app_config.redact_secrets(resp.text[:300])
                    if self._app_config is not None
                    else resp.text[:300]
                )
                raise LLMError(f"VISION_HTTP_{resp.status_code}", f"视觉槽位接口返回 HTTP {resp.status_code}: {safe}")
            data = resp.json()

        choices = data.get("choices") or []
        content = ""
        if choices:
            content = str(((choices[0].get("message") or {}).get("content")) or "").strip()
        raw_usage = data.get("usage") or {}
        pt = int(raw_usage.get("prompt_tokens", 0) or 0)
        ct = int(raw_usage.get("completion_tokens", 0) or 0)
        tt = int(raw_usage.get("total_tokens", pt + ct) or 0)
        self.last_ocr_usage = LLMUsage(
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
            estimated_cost_cny=estimate_cny_cost(vision_prof.model, pt, 0, ct, profile=vision_prof)
            if (pt + ct) > 0
            else 0.001,
            model=vision_prof.model,
            profile=vision_prof.name,
            provider=vision_prof.provider_id or vision_prof.provider,
            slot="vision_ocr",
        )
        return content

    async def prepare_user_message(
        self,
        *,
        user_text: str,
        image_bytes: bytes,
        content_type: str,
        profile: ProfileConfig,
        screen_ref: ContextScreenRefPayload,
    ) -> Tuple[Union[str, List[Dict[str, Any]]], str]:
        self.last_ocr_usage = None
        window_desc = (
            f"当前主窗口：{screen_ref.target_app} - {screen_ref.target_title}".strip(" -")
            if screen_ref.target_app or screen_ref.target_title
            else "当前桌面主屏幕"
        )
        anti_selfie_note = (
            "（注意：截图中可能包含「启明星」自己的聊天网页区域，请忽略启明星自身网页，"
            "聚焦用户正在操作的目标工作窗口或核心画面。）"
        )

        if self._app_config is not None and self._app_config.has_explicit_routing:
            vision_prof = self._app_config.resolve_slot_profile("vision_ocr")
            same_as_chat = (
                profile.supports_vision
                and (vision_prof.provider_id == (profile.provider_id or ""))
                and (vision_prof.model == profile.model)
            )
            if not same_as_chat and vision_prof.model:
                logger.info(
                    "Routing screenshot via vision_ocr slot: provider=%s model=%s bytes=%d",
                    vision_prof.provider_id,
                    vision_prof.model,
                    len(image_bytes),
                )
                extracted_text = await self._call_vision_slot(
                    vision_prof, image_bytes, content_type, window_desc
                )
                if len(extracted_text) > 6000:
                    extracted_text = extracted_text[:6000] + "\n...（页面内容过长已截断）"
                ocr_block = extracted_text or "（屏幕截图未识别出明确文字）"
                combined = (
                    f"{user_text}\n\n"
                    f"【当前屏幕视觉与 OCR 解析】（{window_desc} · {vision_prof.provider_id}/{vision_prof.model}）{anti_selfie_note}\n"
                    f"```text\n{ocr_block}\n```"
                )
                return combined, "vision_slot"

        if profile.supports_vision:
            b64 = base64.b64encode(image_bytes).decode("ascii")
            data_url = f"data:{content_type};base64,{b64}"
            prompt_text = f"{user_text}\n\n[桌面画面参考: {window_desc}] {anti_selfie_note}"
            logger.info(
                "Routing screenshot via Vision direct (Path A): profile=%s model=%s bytes=%d",
                profile.name,
                profile.model,
                len(image_bytes),
            )
            return (
                [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
                "vision",
            )

        logger.info(
            "Routing screenshot via DeepSeek OCR fallback (Path B): profile=%s model=%s bytes=%d",
            profile.name,
            profile.model,
            len(image_bytes),
        )
        extracted_text = await self.ocr_provider.extract_text(image_bytes, content_type=content_type)
        if len(extracted_text) > 6000:
            extracted_text = extracted_text[:6000] + "\n...（页面内容过长已截断）"
        ocr_block = extracted_text or "（屏幕截图未识别出明确文字）"
        combined = (
            f"{user_text}\n\n"
            f"【当前屏幕 OCR 提取内容】（{window_desc}）{anti_selfie_note}\n"
            f"```text\n{ocr_block}\n```"
        )
        return combined, "ocr"
