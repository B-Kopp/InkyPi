"""Priority and lifecycle engine for generic dynamic scheduler rules."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from .condition_evaluator import ConditionEvaluator
from .temporal import latest_occurrence, occurrence_key
from .models import (
    RuleActivation,
    SchedulerConfig,
    SchedulerRule,
    SchedulerState,
    SchedulingDecision,
    SchedulingOutcome,
)

logger = logging.getLogger(__name__)


class DynamicScheduler:
    def __init__(self, config: SchedulerConfig, condition_evaluator: ConditionEvaluator | None = None):
        self.config = config
        self.conditions = condition_evaluator or ConditionEvaluator()
        self.state = SchedulerState()

    def evaluate(
        self,
        current_dt: datetime,
        state_lookup: Callable[[Any], tuple[bool, dict[str, Any]]],
    ) -> SchedulingOutcome:
        if not self.config.enabled:
            return SchedulingOutcome()

        logger.info("Dynamic scheduler evaluation started")
        previous_id = self.state.active_rule_id
        previous_rule = self._rule(previous_id)
        matches: dict[str, bool] = {}
        for rule in self.config.rules:
            if not rule.enabled:
                matches[rule.id] = False
                continue
            due = True
            if rule.schedule:
                occurrence = latest_occurrence(rule.schedule, current_dt, self.state.last_evaluated_at)
                key = occurrence_key(occurrence) if occurrence else None
                due = key is not None and key != self.state.occurrence_keys.get(rule.id)
                # Consume even if conditions, cooldown, or priority block it.
                # There is intentionally no pending occurrence queue.
                if due:
                    self.state.occurrence_keys[rule.id] = key
            # Occurrence rules only need fresh application state when due.
            result = self.conditions.evaluate(rule.conditions, current_dt, state_lookup) if due else None
            matches[rule.id] = bool(result and result.matched and result.available)
            if matches[rule.id]:
                logger.info("Dynamic scheduler rule matched: %s priority=%s", rule.id, rule.priority)
            else:
                logger.debug("Dynamic scheduler rule skipped: %s condition=false", rule.id)
            if rule.id in self.state.blocked_until_false and not matches[rule.id]:
                self.state.blocked_until_false.discard(rule.id)

        self.state.last_evaluated_at = current_dt
        self._expire_activations(current_dt, matches)
        candidates = [rule for rule in self.config.rules if self._is_candidate(rule, current_dt, matches[rule.id])]
        winner = self._winner(candidates, previous_id)

        if winner is None:
            self.state.active_rule_id = None
            logger.info("No dynamic scheduler rule matched")
            return SchedulingOutcome(
                ended_rule_id=previous_id,
                return_behavior=previous_rule.return_behavior if previous_rule else None,
            )

        changed = winner.id != previous_id
        activation = self.state.activations.get(winner.id)
        if activation is None:
            activation = self._activate(winner, current_dt)
        self.state.active_rule_id = winner.id
        if changed:
            if previous_id:
                logger.info("Preempting dynamic scheduler rule %s with %s", previous_id, winner.id)
            else:
                logger.info("Dynamic scheduler rule activated: %s priority=%s", winner.id, winner.priority)
        return SchedulingOutcome(decision=SchedulingDecision(
            target=winner.target,
            source="dynamic_scheduler",
            rule_id=winner.id,
            priority=winner.priority,
            activated_at=activation.activated_at,
            expires_at=activation.expires_at,
            return_behavior=winner.return_behavior,
            reason=f"rule {winner.id} matched",
            changed=changed,
        ))

    def _expire_activations(self, now: datetime, matches: dict[str, bool]) -> None:
        for rule_id, activation in list(self.state.activations.items()):
            rule = self._rule(rule_id)
            if rule is None:
                self.state.activations.pop(rule_id, None)
                continue
            elapsed = (now - activation.activated_at).total_seconds()
            expired = activation.expires_at is not None and now >= activation.expires_at
            if rule.duration.mode == "while_true":
                expired = expired or (not matches.get(rule_id, False) and elapsed >= rule.duration.min_seconds)
            if expired:
                self._end(rule, now, block_while_true=matches.get(rule_id, False) and not rule.schedule)

    def _is_candidate(self, rule: SchedulerRule, now: datetime, matched: bool) -> bool:
        if not rule.enabled:
            return False
        if rule.id in self.state.activations:
            return True
        if not matched or rule.id in self.state.blocked_until_false:
            return False
        cooldown = self.state.cooldown_until.get(rule.id)
        return cooldown is None or now >= cooldown

    def _winner(self, candidates: list[SchedulerRule], active_id: str | None) -> SchedulerRule | None:
        if not candidates:
            return None
        return min(candidates, key=lambda rule: (
            -rule.priority,
            0 if rule.id == active_id else 1,
            rule.order,
            rule.id,
        ))

    def _activate(self, rule: SchedulerRule, now: datetime) -> RuleActivation:
        expires_at = None
        if rule.duration.mode == "fixed":
            expires_at = now + timedelta(seconds=rule.duration.seconds or 0)
        elif rule.duration.max_seconds is not None:
            expires_at = now + timedelta(seconds=rule.duration.max_seconds)
        activation = RuleActivation(rule.id, now, expires_at)
        self.state.activations[rule.id] = activation
        return activation

    def _end(self, rule: SchedulerRule, now: datetime, block_while_true: bool) -> None:
        self.state.activations.pop(rule.id, None)
        if rule.cooldown_seconds:
            self.state.cooldown_until[rule.id] = now + timedelta(seconds=rule.cooldown_seconds)
        if block_while_true:
            self.state.blocked_until_false.add(rule.id)
        logger.info("Dynamic scheduler rule ended: %s", rule.id)

    def _rule(self, rule_id: str | None) -> SchedulerRule | None:
        if rule_id is None:
            return None
        return next((rule for rule in self.config.rules if rule.id == rule_id), None)

    def skip_occurrences(self, current_dt):
        """Advance only temporal history during an explicit manual hold."""
        for rule in self.config.rules:
            if rule.enabled and rule.schedule:
                occurrence = latest_occurrence(rule.schedule, current_dt, self.state.last_evaluated_at)
                if occurrence:
                    self.state.occurrence_keys[rule.id] = occurrence_key(occurrence)
        self.state.last_evaluated_at = current_dt
