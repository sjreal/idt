"""LLM Guard input scanners and optional local Llama Guard classification."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import Literal, Protocol
from urllib.parse import quote

import httpx

from llm_app.schemas import GuardrailFinding, GuardrailLevel, GuardrailVerdict
from llm_app.settings import Settings

logger = logging.getLogger(__name__)


class GuardrailUnavailable(Exception):
    """Raised when an enabled guardrail could not run safely."""


@dataclass
class GuardrailResult:
    messages: list[dict[str, str]]
    findings: list[GuardrailFinding]
    outcome: Literal["passed", "redacted", "blocked"]
    latency_ms: float
    blocked_by: str | None = None
    category: str | None = None


@dataclass
class ScannerResult:
    messages: list[dict[str, str]]
    findings: list[GuardrailFinding]
    blocked_by: str | None = None
    category: str | None = None


class ScannerRunner(Protocol):
    def scan_messages(
        self, messages: list[dict[str, str]], message_index: int
    ) -> ScannerResult: ...


class SafetyClassifier(Protocol):
    async def classify_input(self, prompt: str) -> tuple[bool, str | None]: ...

    async def classify_output(self, prompt: str, output: str) -> tuple[bool, str | None]: ...

    async def is_available(self) -> bool: ...


def _optional_score(score: float) -> float | None:
    return score if score >= 0 else None


class LLMGuardScannerRunner:
    """Runs the local LLM Guard prompt-injection and secret scanners."""

    def __init__(self, injection_threshold: float = 0.92) -> None:
        try:
            from llm_guard.input_scanners import PromptInjection, Secrets
        except ImportError as exc:
            raise GuardrailUnavailable("Install the llm-guard project dependencies.") from exc

        self._injection = PromptInjection(threshold=injection_threshold, use_onnx=True)
        self._secrets = Secrets(redact_mode="all")

    def scan_messages(
        self, messages: list[dict[str, str]], message_index: int
    ) -> ScannerResult:
        sanitized = [message.copy() for message in messages]
        findings: list[GuardrailFinding] = []

        for index, message in enumerate(sanitized):
            if index != message_index or message.get("role") != "user":
                continue

            text = message["content"]
            injection_sanitized, is_valid, risk_score = self._injection.scan(text)
            if not is_valid:
                findings.append(
                    GuardrailFinding(
                        scanner="Prompt injection",
                        outcome="blocked",
                        risk_score=_optional_score(risk_score),
                        category="prompt_injection",
                    )
                )
                return ScannerResult(
                    messages=sanitized,
                    findings=findings,
                    blocked_by="LLM Guard · PromptInjection",
                    category="prompt_injection",
                )

            findings.append(
                GuardrailFinding(
                    scanner="Prompt injection",
                    outcome="passed",
                    risk_score=_optional_score(risk_score),
                )
            )

            redacted, secret_is_valid, secret_score = self._secrets.scan(injection_sanitized)
            if not secret_is_valid:
                sanitized[index]["content"] = redacted
                findings.append(
                    GuardrailFinding(
                        scanner="Secrets",
                        outcome="redacted",
                        risk_score=_optional_score(secret_score),
                        category="secret",
                    )
                )
            else:
                sanitized[index]["content"] = injection_sanitized
                findings.append(
                    GuardrailFinding(
                        scanner="Secrets",
                        outcome="passed",
                        risk_score=_optional_score(secret_score),
                    )
                )

        return ScannerResult(messages=sanitized, findings=findings)


class OllamaLlamaGuard:
    """Calls the Ollama-hosted Llama Guard classifier."""

    _category_pattern = re.compile(r"\bS(?:1[0-3]|[1-9])\b", re.IGNORECASE)

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def classify_input(self, prompt: str) -> tuple[bool, str | None]:
        return await self._classify([{"role": "user", "content": prompt}])

    async def classify_output(self, prompt: str, output: str) -> tuple[bool, str | None]:
        return await self._classify(
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": output},
            ]
        )

    async def _classify(self, messages: list[dict[str, str]]) -> tuple[bool, str | None]:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "/api/chat",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "stream": False,
                        "keep_alive": "1m",
                        "options": {"temperature": 0},
                    },
                )
                response.raise_for_status()
                content = response.json()["message"]["content"]
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise GuardrailUnavailable(
                    f"Pull the safety model with `make guard-model-pull` ({self.model})."
                ) from exc
            raise GuardrailUnavailable("Llama Guard returned an API error.") from exc
        except httpx.RequestError as exc:
            raise GuardrailUnavailable(
                "Cannot reach Ollama for Llama Guard. Enable the Ollama Compose profile."
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise GuardrailUnavailable("Llama Guard returned an invalid response.") from exc

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            raise GuardrailUnavailable("Llama Guard returned an empty classification.")

        label = lines[0].lower()
        category_match = self._category_pattern.search(" ".join(lines[1:]))
        category = category_match.group(0).upper() if category_match else None
        if label == "safe":
            return True, None
        if label == "unsafe":
            return False, category or "unsafe_content"
        raise GuardrailUnavailable("Llama Guard returned an unknown classification.")

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=min(self.timeout_seconds, 5),
                transport=self.transport,
            ) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                models = response.json().get("models", [])
            return any(isinstance(item, dict) and item.get("name") == self.model for item in models)
        except (httpx.HTTPError, AttributeError, TypeError, ValueError):
            return False


class CloudflareLlamaGuard:
    """Calls Cloudflare Workers AI's hosted Llama Guard 3 model."""

    _category_pattern = re.compile(r"\bS(?:1[0-3]|[1-9])\b", re.IGNORECASE)

    def __init__(
        self,
        account_id: str,
        api_token: str,
        model: str = "@cf/meta/llama-guard-3-8b",
        base_url: str = "https://api.cloudflare.com/client/v4",
        timeout_seconds: float = 90,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account_id = account_id
        self.api_token = api_token
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def classify_input(self, prompt: str) -> tuple[bool, str | None]:
        return await self._classify([{"role": "user", "content": prompt}])

    async def classify_output(self, prompt: str, output: str) -> tuple[bool, str | None]:
        return await self._classify(
            [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": output},
            ]
        )

    async def _classify(self, messages: list[dict[str, str]]) -> tuple[bool, str | None]:
        if not self.account_id or not self.api_token:
            raise GuardrailUnavailable(
                "Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to enable Full mode."
            )

        model_path = quote(self.model, safe="@/")
        url = f"{self.base_url}/accounts/{self.account_id}/ai/run/{model_path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_token}"},
                    json={
                        "messages": messages,
                        "temperature": 0,
                        "max_tokens": 32,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise GuardrailUnavailable(
                    "Cloudflare Workers AI rejected the token; check its Workers AI permissions."
                ) from exc
            if exc.response.status_code == 429:
                raise GuardrailUnavailable(
                    "Cloudflare Workers AI rate-limited the check; "
                    "verify your daily Neurons allowance."
                ) from exc
            raise GuardrailUnavailable("Cloudflare Workers AI returned an API error.") from exc
        except httpx.RequestError as exc:
            raise GuardrailUnavailable("Cannot reach Cloudflare Workers AI.") from exc
        except (TypeError, ValueError) as exc:
            raise GuardrailUnavailable(
                "Cloudflare Workers AI returned an invalid response."
            ) from exc

        result = data.get("result")
        if data.get("success") is False or not isinstance(result, dict):
            raise GuardrailUnavailable("Cloudflare Workers AI failed to classify the content.")
        content = result.get("response")
        if not isinstance(content, str):
            raise GuardrailUnavailable("Cloudflare Llama Guard returned an invalid response.")

        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if not lines:
            raise GuardrailUnavailable("Cloudflare Llama Guard returned an empty classification.")
        label = lines[0].lower()
        category_match = self._category_pattern.search(" ".join(lines[1:]))
        category = category_match.group(0).upper() if category_match else None
        if label == "safe":
            return True, None
        if label == "unsafe":
            return False, category or "unsafe_content"
        raise GuardrailUnavailable("Cloudflare Llama Guard returned an unknown classification.")

    async def is_available(self) -> bool:
        # Avoid spending Workers AI Neurons on periodic health checks. Actual
        # permissions, availability, and quota are validated on the first check.
        return bool(self.account_id and self.api_token)


class GuardrailPipeline:
    """Coordinates enabled checks and fails closed when a configured check fails."""

    def __init__(
        self,
        settings: Settings,
        scanner_runner: ScannerRunner | None = None,
        classifier: SafetyClassifier | None = None,
    ) -> None:
        self.settings = settings
        self._scanner_runner = scanner_runner
        self._scanner_lock = Lock()
        if classifier is not None:
            self._classifier = classifier
        elif settings.llama_guard_backend == "cloudflare":
            self._classifier = CloudflareLlamaGuard(
                account_id=settings.cloudflare_account_id,
                api_token=settings.cloudflare_api_token,
                model=settings.cloudflare_llama_guard_model,
                base_url=settings.cloudflare_base_url,
                timeout_seconds=settings.llama_guard_timeout_seconds,
            )
        else:
            self._classifier = OllamaLlamaGuard(
                base_url=settings.llama_guard_base_url,
                model=settings.llama_guard_model,
                timeout_seconds=settings.llama_guard_timeout_seconds,
            )

    def _get_scanner_runner(self) -> ScannerRunner:
        if self._scanner_runner is None:
            self._scanner_runner = LLMGuardScannerRunner(
                injection_threshold=self.settings.prompt_injection_threshold
            )
        return self._scanner_runner

    def _scan_messages_synchronously(
        self, messages: list[dict[str, str]], message_index: int
    ) -> ScannerResult:
        with self._scanner_lock:
            return self._get_scanner_runner().scan_messages(messages, message_index)

    async def classifier_is_configured(self) -> bool:
        return await self._classifier.is_available()

    async def inspect_input(
        self,
        messages: list[dict[str, str]],
        level: GuardrailLevel,
    ) -> GuardrailResult:
        started = perf_counter()
        sanitized_messages = [message.copy() for message in messages]
        findings: list[GuardrailFinding] = []
        outcome: Literal["passed", "redacted", "blocked"] = "passed"
        blocked_by = None
        category = None
        latest_user_index = next(
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        )

        if level in {"scanners", "full"}:
            try:
                scan = await asyncio.to_thread(
                    self._scan_messages_synchronously, messages, latest_user_index
                )
            except Exception as exc:
                logger.exception("LLM Guard input scanning failed")
                raise GuardrailUnavailable(
                    "LLM Guard scanners could not run; the request was not sent to the model."
                ) from exc
            sanitized_messages = scan.messages
            findings.extend(scan.findings)
            if scan.blocked_by:
                outcome = "blocked"
                blocked_by = scan.blocked_by
                category = scan.category

        if level == "full" and outcome != "blocked":
            user_prompt = sanitized_messages[latest_user_index]["content"]
            try:
                is_safe, safety_category = await self._classifier.classify_input(user_prompt)
            except GuardrailUnavailable:
                raise
            except Exception as exc:
                logger.exception("Llama Guard input classification failed")
                raise GuardrailUnavailable(
                    "Llama Guard input classification failed; request was not sent to the model."
                ) from exc

            findings.append(
                GuardrailFinding(
                    scanner="Llama Guard input",
                    outcome="passed" if is_safe else "blocked",
                    category=safety_category,
                )
            )
            if not is_safe:
                outcome = "blocked"
                blocked_by = "Llama Guard input"
                category = safety_category

        if outcome == "passed" and any(finding.outcome == "redacted" for finding in findings):
            outcome = "redacted"

        return GuardrailResult(
            messages=sanitized_messages,
            findings=findings,
            outcome=outcome,
            latency_ms=round((perf_counter() - started) * 1000, 2),
            blocked_by=blocked_by,
            category=category,
        )

    async def inspect_output(
        self,
        prompt: str,
        output: str,
        level: GuardrailLevel,
    ) -> GuardrailResult:
        started = perf_counter()
        if level != "full":
            return GuardrailResult(
                messages=[], findings=[], outcome="passed", latency_ms=0
            )

        try:
            is_safe, safety_category = await self._classifier.classify_output(prompt, output)
        except GuardrailUnavailable:
            raise
        except Exception as exc:
            logger.exception("Llama Guard output classification failed")
            raise GuardrailUnavailable(
                "Llama Guard output classification failed; the response was withheld."
            ) from exc

        finding = GuardrailFinding(
            scanner="Llama Guard output",
            outcome="passed" if is_safe else "blocked",
            category=safety_category,
        )
        return GuardrailResult(
            messages=[],
            findings=[finding],
            outcome="passed" if is_safe else "blocked",
            latency_ms=round((perf_counter() - started) * 1000, 2),
            blocked_by=None if is_safe else "Llama Guard output",
            category=safety_category,
        )


def build_verdict(
    level: GuardrailLevel,
    input_result: GuardrailResult,
    output_result: GuardrailResult | None = None,
) -> GuardrailVerdict:
    findings = input_result.findings + (output_result.findings if output_result else [])
    results = [input_result] + ([output_result] if output_result else [])
    blocked_result = next((result for result in results if result.outcome == "blocked"), None)
    if blocked_result is not None:
        outcome: Literal["passed", "redacted", "blocked"] = "blocked"
    elif any(result.outcome == "redacted" for result in results):
        outcome = "redacted"
    else:
        outcome = "passed"

    return GuardrailVerdict(
        level=level,
        outcome=outcome,
        blocked=outcome == "blocked",
        blocked_by=blocked_result.blocked_by if blocked_result else None,
        category=blocked_result.category if blocked_result else None,
        latency_ms=round(sum(result.latency_ms for result in results), 2),
        findings=findings,
    )
