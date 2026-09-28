"""
Smart Approval Engine for agy-telegram.
Evaluates commands against security policies and manages asynchronous timeout lifecycles.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
from dataclasses import dataclass
from typing import Callable, Coroutine, Any, Dict, List, Optional

logger = logging.getLogger("agy_telegram.approval")


class Decision(enum.Enum):
    AUTO_APPROVE = "auto_approve"
    HARD_DENY = "hard_deny"
    MANUAL = "manual"


@dataclass
class PendingApproval:
    turn_id: str
    chat_id: int
    message_id: int
    command: str
    timer_task: Optional[asyncio.Task] = None


class ApprovalManager:
    def __init__(
        self,
        auto_patterns: Optional[List[str]] = None,
        hard_deny_patterns: Optional[List[str]] = None,
        timeout_seconds: int = 180,
        fallback_action: str = "reject",
    ):
        self.timeout_seconds = timeout_seconds
        self.fallback_action = fallback_action

        # Compile regular expressions with case insensitivity
        self._auto_regex = [re.compile(p, re.IGNORECASE) for p in (auto_patterns or [])]
        self._deny_regex = [re.compile(p, re.IGNORECASE) for p in (hard_deny_patterns or [])]

        # Active pending requests: turn_id -> PendingApproval
        self._pending: Dict[str, PendingApproval] = {}

    def evaluate(self, command: str) -> Decision:
        """Classifies a command before requesting confirmation or sending input."""
        cleaned = command.strip()
        if not cleaned:
            return Decision.MANUAL

        # 1. Hard deny takes immediate precedence for destructive commands
        for pattern in self._deny_regex:
            if pattern.search(cleaned):
                logger.warning(f"Command matched hard-deny policy pattern '{pattern.pattern}': {cleaned}")
                return Decision.HARD_DENY

        # 2. Check read-only / diagnostic allowlist
        for pattern in self._auto_regex:
            if pattern.search(cleaned):
                logger.info(f"Command matched auto-approve policy pattern '{pattern.pattern}': {cleaned}")
                return Decision.AUTO_APPROVE

        return Decision.MANUAL

    def register_pending(
        self,
        turn_id: str,
        chat_id: int,
        message_id: int,
        command: str,
        on_timeout: Callable[[PendingApproval, str], Coroutine[Any, Any, None]],
    ) -> None:
        """Registers a manual approval request and arms the expiration timer."""
        # Cancel any preexisting pending entry for this turn
        self.resolve(turn_id)

        entry = PendingApproval(
            turn_id=turn_id,
            chat_id=chat_id,
            message_id=message_id,
            command=command,
        )

        async def _timer_worker():
            try:
                await asyncio.sleep(self.timeout_seconds)
                # Timeout expired: trigger fallback action
                if turn_id in self._pending:
                    pending = self._pending.pop(turn_id)
                    logger.info(
                        f"Approval timeout ({self.timeout_seconds}s) expired for turn {turn_id}. "
                        f"Applying fallback action '{self.fallback_action}'."
                    )
                    await on_timeout(pending, self.fallback_action)
            except asyncio.CancelledError:
                # Timer cancelled because the operator reacted before expiration
                pass
            except Exception as e:
                logger.error(f"Error executing approval timeout handler for turn {turn_id}: {e}")

        entry.timer_task = asyncio.create_task(_timer_worker())
        self._pending[turn_id] = entry

    def resolve(self, turn_id: str) -> Optional[PendingApproval]:
        """Cancels the active timeout timer when the user acts before expiration."""
        entry = self._pending.pop(turn_id, None)
        if entry and entry.timer_task and not entry.timer_task.done():
            entry.timer_task.cancel()
        return entry

    def get_pending(self, turn_id: str) -> Optional[PendingApproval]:
        """Returns the pending approval entry for turn_id if still active."""
        return self._pending.get(turn_id)
