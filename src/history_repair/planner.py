from __future__ import annotations

from .models import ContinuationMode, PlannerDecision, ProviderCapabilities, RouteTarget


class ContinuationPlanner:
    def select_mode(
        self,
        *,
        transcript_messages: list[dict],
        latest_route: dict | None,
        route_target: RouteTarget,
        capabilities: ProviderCapabilities,
        last_remote_chain_failed: bool,
    ) -> PlannerDecision:
        if self._can_use_remote_chain(
            latest_route=latest_route,
            route_target=route_target,
            capabilities=capabilities,
            last_remote_chain_failed=last_remote_chain_failed,
        ):
            return PlannerDecision(
                mode=ContinuationMode.REMOTE_CHAIN,
                reason="latest route compatible with previous_response_id",
                previous_response_id=latest_route["remote_response_id"],
            )

        total_tokens = self.estimate_tokens_from_messages(transcript_messages)
        if total_tokens <= capabilities.max_context_tokens:
            return PlannerDecision(
                mode=ContinuationMode.LOCAL_REBUILD,
                reason="rebuild from local canonical transcript",
            )
        return PlannerDecision(
            mode=ContinuationMode.SUMMARY_REBUILD,
            reason="transcript exceeds max context budget",
        )

    def estimate_tokens_from_messages(self, transcript_messages: list[dict]) -> int:
        total = 0
        for message in transcript_messages:
            text = message.get("content", "")
            total += max(1, len(text))
        return total

    def _can_use_remote_chain(
        self,
        *,
        latest_route: dict | None,
        route_target: RouteTarget,
        capabilities: ProviderCapabilities,
        last_remote_chain_failed: bool,
    ) -> bool:
        if latest_route is None:
            return False
        if not capabilities.supports_previous_response_id:
            return False
        if last_remote_chain_failed:
            return False
        if not latest_route.get("remote_response_id"):
            return False
        if latest_route["provider"] != route_target.provider:
            return False
        if latest_route["account_id"] != route_target.account_id:
            return False
        if latest_route["model"] != route_target.model:
            return False
        return True
