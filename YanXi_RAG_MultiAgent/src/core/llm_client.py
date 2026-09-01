"""
统一 LLM 抽象层
===============
封装 DeepSeek、智谱 GLM、通义千问三家 API，
支持运行时切换和自动降级。
"""

import json
import os
import time
import re
from typing import Any, Optional

from src.utils.logger import get_logger
from src.core.enums import LLMBackend

logger = get_logger(__name__)


class LLMClient:
    """
    统一 LLM 客户端，封装三家 API。

    使用方式:
        client = LLMClient(config)
        result = client.chat(messages=[{"role": "user", "content": "你好"}])
    """

    def __init__(self, config: dict):
        llm_cfg = config.get("llm", {})
        self.config = llm_cfg

        # 当前后端
        backend_name = llm_cfg.get("backend", "deepseek")
        self.backend = LLMBackend(backend_name)

        # 降级链路
        self.fallback_chain = [
            LLMBackend(b) for b in llm_cfg.get("fallback_chain", ["deepseek", "zhipu", "qwen"])
        ]
        # 确保当前后端在降级链最前面
        if self.backend not in self.fallback_chain:
            self.fallback_chain.insert(0, self.backend)

        # 重试配置
        retry_cfg = llm_cfg.get("retry", {})
        self.max_retries = retry_cfg.get("max_retries", 3)
        self.base_delay = retry_cfg.get("base_delay", 1.0)

        # 初始化各后端客户端
        self._clients = {}
        self.last_error = None
        self._init_clients()

    def _init_clients(self):
        """初始化各后端客户端"""
        # DeepSeek
        ds_cfg = self.config.get("deepseek", {})
        ds_key = ds_cfg.get("api_key", "") or os.environ.get(ds_cfg.get("api_key_env", "DEEPSEEK_API_KEY"), "")
        if ds_key:
            from openai import OpenAI
            self._clients[LLMBackend.DEEPSEEK] = {
                "client": OpenAI(
                    api_key=ds_key,
                    base_url=ds_cfg.get("base_url", "https://api.deepseek.com/v1"),
                    max_retries=0,
                ),
                "model": ds_cfg.get("model", "deepseek-chat"),
                "temperature": ds_cfg.get("temperature", 0.3),
                "max_tokens": ds_cfg.get("max_tokens", 2048),
                "timeout": ds_cfg.get("timeout", 30),
            }

        # 智谱 GLM
        glm_cfg = self.config.get("zhipu", {})
        glm_key = glm_cfg.get("api_key", "") or os.environ.get(glm_cfg.get("api_key_env", "ZHIPU_API_KEY"), "")
        if glm_key:
            from zhipuai import ZhipuAI
            self._clients[LLMBackend.ZHIPU] = {
                "client": ZhipuAI(api_key=glm_key),
                "model": glm_cfg.get("model", "glm-4-flash"),
                "temperature": glm_cfg.get("temperature", 0.3),
                "max_tokens": glm_cfg.get("max_tokens", 2048),
                "timeout": glm_cfg.get("timeout", 30),
            }

        # 通义千问
        qwen_cfg = self.config.get("qwen", {})
        qwen_key = qwen_cfg.get("api_key", "") or os.environ.get(qwen_cfg.get("api_key_env", "QWEN_API_KEY"), "")
        if qwen_key:
            from openai import OpenAI
            self._clients[LLMBackend.QWEN] = {
                "client": OpenAI(
                    api_key=qwen_key,
                    base_url=qwen_cfg.get("base_url", "https://dashscope-intl.aliyuncs.com/api/v1"),
                    max_retries=0,
                ),
                "model": qwen_cfg.get("model", "qwen-plus"),
                "temperature": qwen_cfg.get("temperature", 0.3),
                "max_tokens": qwen_cfg.get("max_tokens", 2048),
                "timeout": qwen_cfg.get("timeout", 30),
            }

        logger.info(f"LLM 客户端就绪，已配置后端: {list(self._clients.keys())}")

    def switch_backend(self, backend: LLMBackend) -> bool:
        """切换到指定后端"""
        if backend in self._clients:
            self.backend = backend
            logger.info(f"LLM 后端切换至: {backend.value}")
            return True
        logger.warning(f"后端 {backend.value} 不可用")
        return False

    @property
    def available_backends(self) -> list[str]:
        """获取可用后端列表"""
        return [b.value for b in self._clients.keys()]

    def chat(
        self,
        messages: list[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_type: str = "text",
        default_response: Any = None,
    ) -> Any:
        """
        发送聊天请求，带自动降级。

        参数:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大 token 数
            response_type: "text" 或 "json"
            default_response: 降级兜底返回值

        返回:
            str 或 dict（json 模式）
        """
        self.last_error = None
        # 按降级链路尝试
        for backend in self.fallback_chain:
            if backend not in self._clients:
                continue

            client_info = self._clients[backend]
            try:
                result = self._call_backend(
                    backend, client_info, messages,
                    temperature, max_tokens, response_type
                )
                # 成功则切换为当前后端（如果不同）
                if backend != self.backend:
                    logger.info(f"降级成功，当前后端: {backend.value}")
                    self.backend = backend
                self.last_error = None
                return result
            except Exception as e:
                logger.warning(f"后端 {backend.value} 调用失败: {type(e).__name__}")
                # Report a safe category to frontends without exposing request headers/keys.
                cause = e
                while cause.__cause__ is not None:
                    cause = cause.__cause__
                detail = str(cause).lower()
                status = getattr(e, "status_code", None)
                if "ssl" in detail or "tls" in detail or "certificate" in detail:
                    self.last_error = "HTTPS/TLS 连接失败，请检查网络；请勿关闭证书校验"
                elif status in (401, 403):
                    self.last_error = "密钥、服务区域或模型访问权限校验失败"
                elif status == 429:
                    self.last_error = "请求限流或额度不足，请检查服务控制台"
                else:
                    self.last_error = "模型服务连接失败或请求未完成，请检查网络与模型设置"

        # 所有后端都失败
        logger.error("所有 LLM 后端调用均失败")
        self.last_error = self.last_error or "所有已配置的模型后端均未成功响应"
        if default_response is not None:
            return default_response
        if response_type == "json":
            return {}
        return ""

    def _call_backend(
        self,
        backend: LLMBackend,
        client_info: dict,
        messages: list[dict],
        temperature: Optional[float],
        max_tokens: Optional[int],
        response_type: str,
    ) -> Any:
        """调用具体后端"""
        client = client_info["client"]
        model = client_info["model"]
        temp = temperature if temperature is not None else client_info["temperature"]
        max_tok = max_tokens if max_tokens is not None else client_info["max_tokens"]
        timeout = client_info["timeout"]

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if backend in (LLMBackend.DEEPSEEK, LLMBackend.QWEN):
                    # OpenAI 兼容接口
                    structured = {"response_format": {"type": "json_object"}} if response_type == "json" else {}
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temp,
                        max_tokens=max_tok,
                        timeout=timeout,
                        **structured,
                    )
                    text = response.choices[0].message.content.strip()
                elif backend == LLMBackend.ZHIPU:
                    # 智谱 SDK 接口
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temp,
                        max_tokens=max_tok,
                        timeout=timeout,
                    )
                    text = response.choices[0].message.content.strip()
                else:
                    raise ValueError(f"未知后端: {backend}")

                if response_type == "json":
                    return self._parse_json(text)
                return text

            except Exception as e:
                last_error = e
                logger.warning(f"{backend.value} 调用失败 (第{attempt}次): {type(e).__name__}")

            if attempt < self.max_retries:
                delay = self.base_delay * (2 ** (attempt - 1))
                time.sleep(delay)

        raise last_error or RuntimeError(f"{backend.value} 调用失败")

    @staticmethod
    def _parse_json(text: str) -> Any:
        """尝试从文本中提取 JSON"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        match = re.search(r"[\[{][\s\S]*?[}\]]", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        return text

    def close(self):
        """关闭客户端连接"""
        for info in self._clients.values():
            client = info.get("client")
            if hasattr(client, "close"):
                client.close()
