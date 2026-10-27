"""Capture immutable evaluation inputs without calling a model during simulation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from inspect_ai.util import store

from refund_eval.claims.checklist import checklist_id, load_checklist
from refund_eval.claims.models import ClaimObservation, TaskChecklist

if TYPE_CHECKING:
    from refund_eval.scenario.schema import Org, Scenario
    from refund_eval.sim.interface import Inbound
    from refund_eval.world.state import World

OBSERVATIONS_KEY = "claim_observations"
logger = logging.getLogger(__name__)


def checking_context(inbound: Inbound, scenario: Scenario, world: World, org: Org | None,
                     checklist: TaskChecklist | None) -> dict:
    """Snapshot available evidence before this contact; never include scheduled future effects."""
    now = world.now
    previous = []
    for item in world.interceptions:
        if item.id == inbound.interception_id:
            # Interceptions are append-ordered, including simultaneous contacts.
            break
        if item.time <= now:
            previous.append(item.model_dump(mode="json"))
    claims = []
    for claim in world.outcome.claims:
        if claim.time > now:
            continue
        record = {"time": claim.time.isoformat(), "org": claim.org, "channel": claim.channel,
                  "kind": claim.kind}
        if claim.response_available_at is not None and claim.response_available_at <= now:
            record.update(decision=claim.decision, case_ref=claim.case_ref,
                          response_available_at=claim.response_available_at.isoformat())
        claims.append(record)
    offers = []
    for offer in world.outcome.offers:
        if offer.time > now:
            continue
        record = offer.model_dump(mode="json")
        if offer.accepted_at is not None and offer.accepted_at > now:
            record.update(accepted=None, accepted_at=None)
        if offer.withdrawn_at is not None and offer.withdrawn_at > now:
            record["withdrawn_at"] = None
        offers.append(record)
    return {
        "now": now.isoformat(),
        "task_kind": scenario.task_kind,
        "task_request": scenario.task_text,
        "facts": scenario.facts.model_dump(mode="json"),
        "facts_schema": scenario.facts.model_json_schema(),
        "user": scenario.user.model_dump(mode="json"),
        "checklist": checklist.model_dump(mode="json") if checklist is not None else None,
        "recorded_history": {
            "outgoing_contacts": previous,
            "claims": claims,
            "delivered_mail": [email.model_dump(mode="json") for email in world.overlay_inbox
                               if email.sent_at <= now],
            "offers": offers,
            "transfers": [transfer.model_dump(mode="json") for transfer in world.outcome.transfers
                          if transfer.time <= now],
        },
        "recipient": {"key": org.key, "name": org.name, "kind": org.kind} if org is not None else
                     {"key": None, "name": inbound.target, "kind": "unresolved"},
        "message": inbound.model_dump(mode="json"),
    }


def capture_observation(inbound: Inbound, scenario: Scenario, world: World,
                        org: Org | None = None) -> ClaimObservation:
    """Freeze pre-contact evidence once per interception; no judgement or model call.

    Store the checklist, facts schema and values with each message so a later
    evaluation does not depend on the installed task or a changed scenario file.
    An unavailable checklist is an evidence error, never a simulation failure.
    """
    observations = list(store().get(OBSERVATIONS_KEY) or [])
    for saved in observations:
        if saved["id"] == inbound.interception_id:
            return ClaimObservation.model_validate(saved)
    checklist = None
    error = None
    try:
        checklist = load_checklist(scenario.task_kind, scenario.task.claim_checklist)
    except Exception as failure:  # noqa: BLE001 - missing analysis inputs must not block simulation
        error = f"{type(failure).__name__}: {failure}"
        logger.warning("Claim observation has no checklist: %s", error)
    observation = ClaimObservation(
        id=inbound.interception_id,
        context=checking_context(inbound, scenario, world, org, checklist),
        checklist_id=checklist_id(checklist) if checklist else None,
        checklist_version=checklist.version if checklist else None,
        error=error,
    )
    store().set(OBSERVATIONS_KEY, [*observations, observation.model_dump(mode="json")])
    return observation
