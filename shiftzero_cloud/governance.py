"""Model Armor ingress screening with deterministic policy fallback."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from shiftzero_core.contracts import SecurityFinding, make_id
from shiftzero_core.safety_kernel import screen_text


@dataclass(frozen=True)
class ScreeningDecision:
    provider: str
    blocked: bool
    latency_ms: int
    finding: Optional[SecurityFinding]
    match_state: str
    invocation_result: str
    fallback_reason: Optional[str] = None

    def evidence(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "blocked": self.blocked,
            "latency_ms": self.latency_ms,
            "match_state": self.match_state,
            "invocation_result": self.invocation_result,
            "fallback_reason": self.fallback_reason,
            "finding": asdict(self.finding) if self.finding else None,
        }


class ContentGuard:
    def __init__(
        self,
        *,
        mode: str = "local",
        project_id: str = "",
        location: str = "asia-southeast1",
        template_id: str = "shiftzero-ingress",
    ) -> None:
        self.mode = mode if mode in {"auto", "modelarmor", "local"} else "local"
        self.project_id = project_id
        self.location = location
        self.template_id = template_id
        self.last_decision: Optional[ScreeningDecision] = None

    @classmethod
    def from_env(cls) -> "ContentGuard":
        return cls(
            mode=os.getenv("SHIFTZERO_CONTENT_GUARD_MODE", "auto").strip().lower(),
            project_id=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            location=os.getenv("SHIFTZERO_MODEL_ARMOR_LOCATION", "asia-southeast1"),
            template_id=os.getenv("SHIFTZERO_MODEL_ARMOR_TEMPLATE", "shiftzero-ingress"),
        )

    @property
    def configured(self) -> bool:
        return self.mode in {"auto", "modelarmor"} and bool(
            self.project_id and self.location and self.template_id
        )

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "provider": "model-armor" if self.configured else "local-policy",
            "configured": self.configured,
            "location": self.location if self.configured else None,
            "template": self.template_id if self.configured else None,
            "last_decision": self.last_decision.evidence() if self.last_decision else None,
        }

    async def inspect(
        self,
        text: str,
        source: str,
        tick: int,
        trace_id: str = "",
    ) -> ScreeningDecision:
        started = time.perf_counter()
        local_finding = screen_text(text, source, tick, trace_id)
        if not self.configured:
            decision = ScreeningDecision(
                provider="local-policy",
                blocked=local_finding is not None,
                latency_ms=round((time.perf_counter() - started) * 1000),
                finding=local_finding,
                match_state="MATCH_FOUND" if local_finding else "NO_MATCH_FOUND",
                invocation_result="LOCAL_POLICY",
            )
            self.last_decision = decision
            return decision

        try:
            response = await asyncio.to_thread(self._sanitize, text)
            result = response.get("sanitization_result", {})
            state = str(result.get("filter_match_state", "UNKNOWN"))
            matched = state == "MATCH_FOUND"
            finding = local_finding
            if matched and finding is None:
                finding = SecurityFinding(
                    finding_id=make_id("sec", source, "model-armor", tick),
                    source=source,
                    category="MODEL_ARMOR_MATCH",
                    blocked_reason="Google Cloud Model Armor matched an ingress filter",
                    excerpt=text[:160],
                    detected_at=tick,
                    trace_id=trace_id,
                )
            decision = ScreeningDecision(
                provider="model-armor+local-policy",
                blocked=matched or local_finding is not None,
                latency_ms=round((time.perf_counter() - started) * 1000),
                finding=finding,
                match_state=state,
                invocation_result=str(result.get("invocation_result", "UNKNOWN")),
            )
        except Exception as exc:
            decision = ScreeningDecision(
                provider="local-policy-fallback",
                blocked=local_finding is not None,
                latency_ms=round((time.perf_counter() - started) * 1000),
                finding=local_finding,
                match_state="MATCH_FOUND" if local_finding else "NO_MATCH_FOUND",
                invocation_result="MODEL_ARMOR_ERROR",
                fallback_reason=f"{type(exc).__name__}: {exc}"[:500],
            )
        self.last_decision = decision
        return decision

    def _sanitize(self, text: str) -> dict[str, Any]:
        from google.api_core.client_options import ClientOptions
        from google.cloud import modelarmor_v1
        from google.protobuf.json_format import MessageToDict

        client = modelarmor_v1.ModelArmorClient(
            transport="rest",
            client_options=ClientOptions(
                api_endpoint=f"modelarmor.{self.location}.rep.googleapis.com"
            ),
        )
        request = modelarmor_v1.SanitizeUserPromptRequest(
            name=(
                f"projects/{self.project_id}/locations/{self.location}/templates/"
                f"{self.template_id}"
            ),
            user_prompt_data=modelarmor_v1.DataItem(text=text),
        )
        response = client.sanitize_user_prompt(request=request)
        return MessageToDict(response._pb, preserving_proto_field_name=True)
