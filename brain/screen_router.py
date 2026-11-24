from __future__ import annotations

import base64
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from brain.config import AppConfig, OCRConfig, ProfileConfig
from brain.llm_router import LLMError
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
    """Dual-path screen understanding router: Vision direct vs Cloud OCR fallback."""

    def __init__(self, ocr_provider: OCRProvider) -> None:
        self.ocr_provider = ocr_provider

    async def prepare_user_message(
        self,
        *,
        user_text: str,
        image_bytes: bytes,
        content_type: str,
        profile: ProfileConfig,
        screen_ref: ContextScreenRefPayload,
    ) -> Tuple[Union[str, List[Dict[str, Any]]], str]:
        window_desc = (
            f"当前主窗口：{screen_ref.target_app} - {screen_ref.target_title}".strip(" -")
            if screen_ref.target_app or screen_ref.target_title
            else "当前桌面主屏幕"
        )
        anti_selfie_note = (
            "（注意：截图中可能包含「启明星」自己的聊天网页区域，请忽略启明星自身网页，"
            "聚焦用户正在操作的目标工作窗口或核心画面。）"
        )

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
