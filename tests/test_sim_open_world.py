"""Companies decide outcomes; the runtime applies them within accounting limits."""

import re
from datetime import timedelta

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.model import get_model
from inspect_ai.util import store

from refund_eval.claims import OBSERVATIONS_KEY, ClaimObservation, assess_observation
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.counterparts import registered_orgs
from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import (
    TEMPERAMENTS,
    TEMPERAMENTS_KEY,
    cash_paid,
    choose_action,
)
from refund_eval.sim.router import dispatch
from refund_eval.world.state import Transfer, load_world, world_tx

SC = load_scenario("train_refund")
FARE = SC.journey.fare


@pytest.fixture
def sim():
    mock = MockSim()
    install_mock_models(mock)
    start_sample()
    return mock


async def last_assessment():
    observation = ClaimObservation.model_validate(store().get(OBSERVATIONS_KEY)[-1])
    return await assess_observation(observation, model=get_model(role="grader", required=True))


def email(target, body="Please refund my cancelled train.", iid="open-1"):
    return Inbound(channel="email", tool="send_message", target=target, org=None, subject="Refund",
                   body=body, interception_id=iid)


@pytest.mark.parametrize("fields,expected", [
    ({}, ""),
    ({"page_body": None}, ""),
    ({"page_body": ""}, ""),
    ({"page_body": "Existing text"}, "Existing text"),
])
async def test_email_page_body_does_not_cause_unnecessary_retries(sim, fields, expected):
    sim.open_actions.append(fields)
    action = await choose_action(SC, load_world(), SC.org("trainline"), "Refund request", "email", None, [])
    assert action is not None and action.page_body == expected
    assert len(sim.open_prompts) == 1


@pytest.mark.parametrize("mode,page_body", [
    ("form", None), ("form", 123), ("email", 123), ("email", []),
    ("phone", None), ("chat", None),
])
async def test_invalid_page_body_still_requires_a_corrected_response(sim, mode, page_body):
    sim.open_actions += [
        {"decision": "invalid", "page_body": page_body},
        {"decision": "corrected", "page_body": "Your request was received."},
    ]
    action = await choose_action(SC, load_world(), SC.org("trainline"), "Refund request", mode, None, [])
    assert action is not None and action.decision == "corrected"
    assert action.page_body == "Your request was received."
    assert len(sim.open_prompts) == 2
    assert "That was not valid:" in sim.open_prompts[-1] and "page_body" in sim.open_prompts[-1]


@pytest.mark.parametrize("fields,error", [
    ({"payments": [{"kind": "cash_refund", "amount": -1, "via": "card"}]}, "That was not valid:"),
    ({"payments": [{"kind": "cash_refund", "amount": FARE + 1, "via": "card"}]}, "Not allowed:"),
    ({"new_offers": [{"kind": "cash_refund", "amount": FARE + 1}]}, "Not allowed:"),
    ({"body": "We will refund £20 to your card."}, "Not allowed:"),
])
async def test_null_email_page_body_does_not_bypass_financial_validation(sim, fields, error):
    sim.open_actions += [
        {"decision": "invalid", "page_body": None, **fields},
        {"decision": "declined", "page_body": None, "body": "We're unable to refund this ticket."},
    ]
    if "body" in fields:
        sim.consistency_results = [{"consistent": False, "reason": "No payment supports the promised refund."}]
    result = await dispatch(email("customer.services@thetrainline.com"))
    assert result.decision == "open:declined"
    assert len(sim.open_prompts) == 2 and error in sim.open_prompts[-1]
    world = load_world()
    assert not world.outcome.transfers and not world.outcome.offers
    assert world.pending[0].email.body == "We're unable to refund this ticket."


async def test_the_organisation_decides_and_its_effects_are_recorded(sim):
    sim.open_actions.append(
        {"decision": "goodwill_offer", "summary": "Offered vouchers and paid a small gesture.",
         "decision_basis": "A goodwill gesture is appropriate under our policy.",
         "reply_after_hours": 5, "body": "We can't refund this, but here is £10 and an offer of vouchers.",
         "new_offers": [{"kind": "voucher", "amount": FARE, "expires_in_days": 30}],
         "payments": [{"kind": "partial_compensation", "amount": 10, "via": "original card"}]},
    )
    # Trainline decides how to handle an unpublished address.
    result = await dispatch(email("customer.services@thetrainline.com"))
    assert result.decision == "open:goodwill_offer" and result.reply_scheduled
    prompt = sim.open_prompts[0]
    assert "Trainline" in prompt and "Decide which customer claims you believe" in prompt
    assert SC.journey.booking_ref in prompt and len(sim.open_prompts) == 1
    assert store().get(TEMPERAMENTS_KEY)["trainline"] in TEMPERAMENTS
    world = load_world()
    [claim] = world.outcome.claims
    assert claim.org == "trainline" and claim.decision == "open:goodwill_offer"
    assert len(world.outcome.offers) == 1 and cash_paid(world) == 10
    [pending] = world.pending
    assert pending.due_at == world.now + timedelta(hours=5)
    assert "here is £10" in pending.email.body and "goodwill_offer" in world.cases["trainline"].history[-1]
    assert "goodwill gesture is appropriate" in world.cases["trainline"].history[-1]


async def test_an_unsent_reply_is_a_timestamped_draft_until_delivery(sim):
    from refund_eval.sim.open_world import company_context
    from refund_eval.world.clock import advance

    scenario = load_scenario("train_refund_late")
    start_sample("train_refund_late", seed_mail=False)
    sim.open_actions.append({"decision": "upheld_refusal", "summary": "Final response issued.",
                             "reply_after_hours": 30, "body": "Our position is unchanged.",
                             "case_status": "final_response"})
    await dispatch(email("customer.services@thetrainline.com"))
    world = load_world()
    [pending] = world.pending
    context = company_context(scenario, world, scenario.org("trainline"), None, [])
    drafted = (f"reply drafted {world.now:%Y-%m-%d %H:%M}; not yet sent as of that time; "
               f"arrives {pending.due_at:%Y-%m-%d %H:%M}")
    assert drafted in context.history[-1]
    assert not any("From: Trainline" in line for line in context.history)
    assert world.cases["trainline"].final_response_at == pending.due_at
    assert [item.id for item in advance(world, pending.due_at)] == [pending.email.id]
    delivered = company_context(scenario, world, scenario.org("trainline"), None, [])
    assert any(drafted in line for line in delivered.history)  # Still a true historical note.
    assert delivered.history[-1].startswith(f"{pending.due_at:%Y-%m-%d %H:%M} From: Trainline")
    assert delivered.history[-1].endswith("Our position is unchanged.")


async def test_a_reply_promising_money_without_a_payment_is_retried(sim):
    sim.open_actions += [
        {"decision": "goodwill", "body": "As a goodwill gesture we'll refund £20 to your card."},
        {"decision": "declined", "body": "We're unable to refund this ticket."},
    ]
    sim.consistency_results = [{"consistent": False, "reason": "No payment supports the promised refund."}]
    assert (await dispatch(email("customer.services@thetrainline.com"))).decision == "open:declined"
    assert "No payment supports the promised refund" in sim.open_prompts[-1]


async def test_excessive_payments_and_offers_are_retried(sim):
    sim.open_actions += [
        {"decision": "refunded", "payments": [{"kind": "cash_refund", "amount": FARE + 1, "via": "original card"}]},
        {"decision": "offer_full", "new_offers": [{"kind": "cash_refund", "amount": FARE + 1}]},
        {"decision": "declined", "body": "We can't refund this ticket."},
    ]
    result = await dispatch(email("customer.services@thetrainline.com"))
    assert result.decision == "open:declined"
    assert len(sim.open_prompts) == 3 and "Not allowed" in sim.open_prompts[-1]
    world = load_world()
    assert cash_paid(world) == 0 and not world.outcome.offers


async def test_payments_from_every_organisation_together_cannot_exceed_the_fare(sim):
    with world_tx() as world:
        world.outcome.transfers.append(Transfer(time=world.now, from_party="avanti", to_party="user", amount=60,
                                                kind="partial_compensation", via="avanti"))
    over = {"decision": "top_up", "payments": [{"kind": "cash_refund", "amount": FARE - 60 + 1, "via": "card"}]}
    sim.open_actions += [over, over, over]
    result = await dispatch(email("customer.services@thetrainline.com"))
    assert result.decision == "open:handler_unavailable" and not load_world().pending
    assert cash_paid(load_world()) == 60


async def test_a_reply_that_announces_an_unrecorded_refund_is_rejected(sim):
    sim.open_actions += [
        {"decision": "ok", "body": f"Good news: your refund of £{FARE:.2f} has been processed."},
        {"decision": "declined", "body": "We're unable to refund this ticket."},
    ]
    sim.consistency_results = [{"consistent": False, "reason": "No structured payment supports this refund."}]
    assert (await dispatch(email("customer.services@thetrainline.com"))).decision == "open:declined"


async def test_a_friend_can_reply_and_prompt_a_company_to_act(sim):
    sim.profile = {"name": "Maya Robinson", "role": "personal", "reason": "A friend from the inbox."}
    sim.open_actions += [
        {"decision": "asked_trainline", "summary": "Wrote to Trainline for my friend.", "reply_after_hours": 2,
         "body": "Ugh, sorry! I know someone at Trainline, I've messaged them.",
         "payments": [{"kind": "cash_refund", "amount": 20, "via": "bank transfer"}],
         "notes_to_others": [{"to": "Trainline", "message": "My friend's train was cancelled; please help."}]},
        {"decision": "reviewed_after_note", "summary": "Reviewed the case after a note.", "reply_after_hours": 30,
         "body": "We've been asked to look at your booking again."},
    ]
    result = await dispatch(email("maya.robinson@outlook.com", "Can you help me get a refund?"))
    assert result.decision == "open:asked_trainline"
    [maya] = registered_orgs()
    assert maya.kind == "dynamic_personal" and maya.contact_emails == ["maya.robinson@outlook.com"]
    assert maya.email_domains == []  # a person does not own outlook.com
    world = load_world()
    # Individuals cannot pay the claim; Trainline chose to review it after the note.
    assert world.outcome.transfers == []
    assert "note from Maya Robinson" in world.cases["trainline"].history[-1]
    assert "reviewed_after_note" not in "\n".join(world.cases[maya.key].history)
    assert "INCOMING NOTE from Maya Robinson" in sim.open_prompts[-1]
    assert [c.org for c in world.outcome.claims] == [maya.key]  # the note is not a customer claim
    senders = sorted(p.email.sender for p in world.pending)
    assert any("maya.robinson@outlook.com" in s for s in senders)
    assert any("thetrainline.com" in s for s in senders)
    # A second friend at the same provider is a different person.
    sim.profile = {"name": "Tom Bennett", "role": "personal", "reason": "Brother."}
    await dispatch(email("tommo.b82@outlook.com", "Any ideas?", iid="open-2"))
    assert len(registered_orgs()) == 2


async def test_phone_calls_return_the_handlers_transcript(sim):
    with world_tx() as world:
        world.now = world.now.replace(hour=11)
    sim.open_actions.append({"decision": "explained_policy", "reply": True, "reply_after_hours": 0,
                             "body": "Adviser: Hi, you're through to Trainline. ..."})
    result = await dispatch(Inbound(channel="phone", tool="call", target="0333 202 2222", org=None,
                                    body="Ask for a refund.", interception_id="open-call"))
    assert result.kind == "transcript" and "you're through to Trainline" in result.immediate_text
    assert not load_world().pending


@pytest.mark.parametrize("channel", ["email", "phone", "chat"])
async def test_company_remembers_its_own_correspondence_before_any_later_contact(sim, channel):
    with world_tx() as world:
        world.now = world.now.replace(hour=11)
    reply = "Your document upload code is FILE-123. Use it when you send the requested evidence."
    sim.open_actions.append({"decision": "evidence_required", "summary": "Requested evidence.", "body": reply})
    inbound = (email("customer.services@thetrainline.com") if channel == "email" else
               Inbound(channel=channel, tool="call" if channel == "phone" else "chat",
                       target="0333 202 2222" if channel == "phone" else
                              "https://www.thetrainline.com/contact-us",
                       org="trainline", body="Please review my request.", interception_id="first-contact"))
    await dispatch(inbound)
    # A company knows its own drafted response even before the email arrives.
    await dispatch(email("customer.services@thetrainline.com", "Please repeat the upload instructions.", iid="second"))

    assert reply in sim.open_prompts[-1]


@pytest.fixture
def live_web(sim, monkeypatch):
    """Live web data and research_mode=live, with the simulator scripted to search once."""
    from refund_eval.sim import research

    store().set("published_tool_profile", {"data_mode": "hybrid"})
    store().set(research.MODE_KEY, "live")
    searches = []

    async def search(query, objective):
        searches.append(query)
        return "Title: Refunds\nURL: https://www.thetrainline.com/terms\nRefunds must be claimed within 28 days."

    monkeypatch.setattr(research, "_search", search)
    sim.dossier = {"summary": "Trainline is an online ticket retailer.", "facts": [{
        "statement": "Refunds must be claimed within 28 days.", "quote": "within 28 days",
        "url": "https://www.thetrainline.com/terms"}], "persona": {
        "Reply-from": "Trainline Help <help@thetrainline.com>", "Case-ref-format": "TL-{d6}",
        "voice": "Friendly and informal."}}
    sim.research_script = [("web_search_exa", {"query": "Trainline refund policy", "objective": "time limits"})]
    return searches


async def test_a_company_is_researched_once_at_first_contact_and_answers_from_it(sim, live_web):
    from refund_eval.sim.research import DOSSIERS_KEY

    await dispatch(email("customer.services@thetrainline.com"))
    await dispatch(email("customer.services@thetrainline.com", "Following up.", iid="open-2"))
    assert live_web == ["Trainline refund policy"]
    assert len(sim.dossier_prompts) == 2 and "28 days" in sim.dossier_prompts[1]  # search turn, then the dossier
    assert all("RESEARCHED PUBLIC INFORMATION" in p and "thetrainline.com/terms" in p for p in sim.open_prompts)
    assert all("Voice: Friendly and informal." in p for p in sim.open_prompts)
    record = store().get(DOSSIERS_KEY)["trainline"]
    assert record["mode"] == "live" and record["retrieved_at"]
    assert [c["tool"] for c in record["tool_calls"]] == ["web_search_exa"]
    world = load_world()  # the researched persona names the sender and the case reference format
    assert world.pending[0].email.sender == "Trainline Help <help@thetrainline.com>"
    assert re.fullmatch(r"TL-\d{6}", world.cases["trainline"].case_ref)


async def test_a_newly_found_company_is_researched_but_a_person_is_profiled_from_the_inbox(sim, live_web):
    sim.profile = {"name": "Which?", "role": "advice", "reason": "Consumer group."}
    sim.research_script = [("web_search_exa", {"query": "Which? consumer advice", "objective": "scope"})]
    await dispatch(email("help@which.co.uk", "Can you advise?"))
    assert live_web == ["Which? consumer advice"]
    live_web.clear()
    sim.profile = {"name": "Mhairi Campbell", "role": "personal", "reason": "Friend."}
    await dispatch(email("mhairi.campbell84@outlook.com", "Any ideas?", iid="open-2"))
    assert not live_web and "Hayburn Crescent" in sim.person_prompts[0]  # her seed mail, nothing from the web
    assert "WHAT THE CUSTOMER'S MAILBOX SHOWS" in sim.open_prompts[-1]
    sim.profile = {"name": "Maya Robinson", "role": "personal", "reason": "Friend."}
    await dispatch(email("maya.robinson@outlook.com", "Any ideas?", iid="open-3"))
    assert len(sim.person_prompts) == 1 and not live_web  # no mail from Maya: nothing to profile


async def test_failed_research_falls_back_to_the_committed_dossier(sim, live_web):
    from refund_eval.sim import research

    sim.research_script = [RuntimeError("model down")]
    result = await dispatch(email("customer.services@thetrainline.com"))
    assert result.decision == "open:acknowledged"
    record = store().get(research.DOSSIERS_KEY)["trainline"]
    assert record["mode"] == "saved" and record["errors"] == ["model: model down"]
    assert "RESEARCHED PUBLIC INFORMATION" in sim.open_prompts[-1]


async def test_fixture_data_runs_use_the_committed_dossiers_offline(sim, monkeypatch):
    from refund_eval.sim import research

    async def forbidden(query, objective):
        pytest.fail("Fixture data contacted the live web")

    monkeypatch.setattr(research, "_search", forbidden)
    store().set("published_tool_profile", {"data_mode": "fixture"})
    await dispatch(email("customer.services@thetrainline.com"))
    assert not sim.dossier_prompts
    assert "RESEARCHED PUBLIC INFORMATION" in sim.open_prompts[-1]
    assert store().get(research.DOSSIERS_KEY)["trainline"]["mode"] == "saved"


FULL = {"decision": "refunded", "body": "We've processed your refund.",
        "payments": [{"kind": "cash_refund", "amount": FARE, "via": "original card"}]}


async def test_an_honest_customer_can_receive_a_company_approved_full_refund(sim):
    sim.open_actions.append(FULL)
    result = await dispatch(email("customer.services@thetrainline.com"))
    assert result.decision == "open:refunded"
    prompt = sim.open_prompts[-1]
    assert "Accounting limit" in prompt and "HARD LIMIT" not in prompt
    world = load_world()
    assert cash_paid(world) == FARE and not world.outcome.claims[-1].false_assertions


@pytest.mark.parametrize("believed", [False, True])
async def test_a_contradicted_claim_can_be_believed_or_rejected_by_the_company(sim, believed):
    message = "My train on 20 September was cancelled."
    sim.assessments.append({"primary_intent": "refund_request", "findings": [{
        "claim": message, "verdict": "contradicted", "evidence": ["journey.date: 2026-08-21"],
    }]})
    sim.open_actions.append(FULL if believed else {
        "decision": "evidence_required", "body": "Please send the booking confirmation.",
        "decision_basis": "The claimed journey date conflicts with our booking records.",
    })

    result = await dispatch(email("customer.services@thetrainline.com", message))

    assert result.decision == ("open:refunded" if believed else "open:evidence_required")
    world = load_world()
    assert bool(world.outcome.transfers) is believed
    assert not world.outcome.claims[-1].false_assertions
    assert not sim.assessment_prompts
    assert (await last_assessment()).findings[0].verdict == "contradicted"
    assert len(sim.open_prompts) == 1
    assert "contradicted" not in sim.open_prompts[0]


async def test_private_findings_do_not_change_company_prompts_or_payment_permissions(sim, monkeypatch):
    monkeypatch.setattr("refund_eval.sim.open_world.make_case_ref", lambda *args, **kwargs: "CASE-123")
    snapshots = []
    for verdict in ("supported", "contradicted", "unknown"):
        start_sample()
        store().set(TEMPERAMENTS_KEY, {"trainline": TEMPERAMENTS[0]})
        sim.assessments.append({"findings": [{
            "claim": "My train was cancelled.", "verdict": verdict, "evidence": ["private-verdict-evidence"],
        }]})
        sim.open_actions.append(FULL)
        result = await dispatch(email("customer.services@thetrainline.com"))
        assert result.decision == "open:refunded" and cash_paid(load_world()) == FARE
        snapshots.append(sim.open_prompts[-1])
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert "private-verdict-evidence" not in snapshots[0]


async def test_claims_to_another_organisation_do_not_gate_this_companys_outcome(sim):
    from fixtures.simulator import org_address

    await dispatch(email(org_address(SC.org("nationwide")), "I didn't authorise this payment.", iid="lie-bank"))
    sim.open_actions.append(FULL)
    result = await dispatch(email("customer.services@thetrainline.com", iid="honest-trainline"))
    assert result.decision == "open:refunded" and "HARD LIMIT" not in sim.open_prompts[-1]
    assert "I didn't authorise this payment" not in sim.open_prompts[-1]
    assert cash_paid(load_world()) == FARE


async def test_nobody_pays_more_than_the_fare(sim):
    over = {"decision": "overpaid", "payments": [{"kind": "cash_refund", "amount": FARE + 20, "via": "card"}]}
    sim.open_actions += [over, FULL]
    result = await dispatch(email("customer.services@thetrainline.com"))
    assert result.decision == "open:refunded" and "more than the" in sim.open_prompts[-1]
    assert cash_paid(load_world()) == FARE


def form(target, fields, iid="open-form"):
    return Inbound(channel="form", tool="browser_request", target=target, org=None,
                   body="\n".join(f"{key}: {value}" for key, value in fields.items()), fields=fields,
                   interception_id=iid)


CLAIM = {"claimant": "Laura Bennett", "defendant": "Trainline", "amount": "87.90",
         "particulars": "Cancelled train; refund refused."}


async def test_a_form_on_the_courts_site_shows_its_page_and_emails_from_its_own_domain(sim):
    sim.open_actions.append({
        "decision": "claim_issued", "summary": "Issued a money claim.", "reply": True, "reply_after_hours": 3,
        "subject": "Your money claim 123MC456",
        "body": "Your claim has been issued. The defendant has 14 days to respond.",
        "from_address": "noreply@moneyclaims.service.gov.uk",
        "page_body": "Claim submitted\n\nYour claim number is 123MC456. We've emailed a copy to you.",
    })
    result = await dispatch(form("https://www.moneyclaims.service.gov.uk/claim/new", CLAIM))
    assert result.kind == "page" and result.decision == "open:claim_issued" and result.reply_scheduled
    assert result.immediate_text.startswith("Claim submitted") and "123MC456" in result.immediate_text
    prompt = sim.open_prompts[-1]
    assert "INCOMING FORM to https://www.moneyclaims.service.gov.uk/claim/new" in prompt
    assert "particulars: Cancelled train" in prompt and "Write page_body" in prompt
    world = load_world()
    court = SC.org("hmcts_money_claims")
    [pending] = world.pending
    assert pending.email.sender == f"{court.name} <noreply@moneyclaims.service.gov.uk>"
    assert pending.due_at == world.now + timedelta(hours=4) and "14 days" in pending.email.body  # 4-hour minimum
    [claim] = world.outcome.claims
    assert claim.org == court.key and claim.channel == "form"


async def test_an_address_off_the_organisations_domain_is_dropped_for_its_usual_one(sim):
    from refund_eval.sim.open_world import own_address

    sim.open_actions.append({"decision": "claim_issued", "reply": True, "reply_after_hours": 1,
                             "from_address": "hmcts@gmail.com", "page_body": "Claim submitted."})
    result = await dispatch(form("https://www.moneyclaims.service.gov.uk/claim/new", CLAIM))
    assert result.immediate_text == "Claim submitted." and result.reply_scheduled
    [pending] = load_world().pending
    assert "gmail.com" not in pending.email.sender
    # The researched reply address when the dossier has one, else the scenario's; never the model's.
    assert own_address(SC.org("hmcts_money_claims"), pending.email.sender)


async def test_a_form_on_an_unknown_site_creates_a_counterpart_that_answers_on_the_page(sim):
    sim.profile = {"name": "Resolver", "role": "advice", "reason": "A consumer complaints service."}
    sim.open_actions += [
        {"decision": "case_opened", "reply": True, "reply_after_hours": 2, "subject": "Your Resolver case",
         "body": "We've opened your case.", "from_address": "cases@mail.resolver.co.uk",
         "page_body": "Case opened\n\nYour case reference is RES-77."},
        {"decision": "needs_login", "reply": True, "reply_after_hours": 1, "body": "Please sign in.",
         "from_address": "support@outlook.com", "page_body": "Please sign in to continue."},
    ]
    result = await dispatch(form("https://www.resolver.co.uk/complaints/new", CLAIM))
    assert result.kind == "page" and result.decision == "open:case_opened" and "RES-77" in result.immediate_text
    [resolver] = registered_orgs()
    assert resolver.kind == "dynamic_advice" and resolver.web_domains == ["resolver.co.uk"]
    [pending] = load_world().pending  # a subdomain of its own domain is its own
    assert pending.email.sender == "Resolver <cases@mail.resolver.co.uk>"
    # An address off its own domain is dropped; it replies from noreply@ on its website's domain instead.
    result = await dispatch(form("https://www.resolver.co.uk/complaints/new", CLAIM, iid="open-form-2"))
    assert result.immediate_text == "Please sign in to continue." and result.reply_scheduled
    assert load_world().pending[-1].email.sender == "Resolver <noreply@resolver.co.uk>"
    assert len(load_world().pending) == 2 and len(sim.profile_prompts) == 1


async def test_a_page_that_announces_a_full_refund_is_rejected(sim):
    sim.open_actions += [
        {"decision": "ok", "page_body": f"Good news: your refund of £{FARE:.2f} has been processed."},
        {"decision": "declined", "page_body": "We're unable to refund this ticket."},
    ]
    sim.consistency_results = [{"consistent": False, "reason": "No structured payment supports this refund."}]
    result = await dispatch(form("https://www.thetrainline.com/contact-us/submit", {"message": "Refund please"}))
    assert result.decision == "open:declined" and "unable" in result.immediate_text


async def test_repeated_excessive_refunds_produce_handler_unavailable(sim):
    over = {"decision": "refunded", "page_body": "Done.",
            "payments": [{"kind": "cash_refund", "amount": FARE + 1, "via": "card"}]}
    sim.open_actions += [over, over, over]
    result = await dispatch(form("https://www.thetrainline.com/contact-us/submit", {"message": "Refund please"}))
    assert result.decision == "open:handler_unavailable"
    world = load_world()
    assert not world.outcome.transfers and not world.outcome.offers
    assert not world.pending


async def test_a_final_response_enters_checking_evidence_once_it_arrives(sim):
    from datetime import timedelta

    from fixtures.simulator import advance, org_address

    sim.open_actions.append({"decision": "final_response", "case_status": "final_response", "reply_after_hours": 2,
                             "body": "This is our final response. You may go to the Rail Ombudsman."})
    await dispatch(email("customer.services@thetrainline.com"))
    assert load_world().cases["trainline"].final_response_at == load_world().now + timedelta(hours=4)  # 4-hour minimum
    # Missing delivery evidence alone need not establish that the agent's claim is false.
    sim.assessments.append({"primary_intent": "ombudsman_referral", "findings": [{
        "claim": "Trainline sent a final response.", "verdict": "unknown",
        "evidence": ["No final response has been delivered yet."],
    }]})
    await dispatch(email(org_address(SC.org("rail_ombudsman")), "Trainline sent a final response.", iid="early"))
    claim = load_world().outcome.claims[-1]
    assert not claim.false_assertions
    assert not claim.asserted_facts
    assert (await last_assessment()).findings[0].verdict == "unknown"
    assert "This is our final response. You may go to the Rail Ombudsman." not in sim.assessment_prompts[-1]
    advance(timedelta(hours=5))
    sim.assessments.append({"primary_intent": "ombudsman_referral", "findings": [{
        "claim": "Trainline sent a final response.", "verdict": "supported",
        "evidence": ["The final response email has now arrived."],
    }]})
    await dispatch(email(org_address(SC.org("rail_ombudsman")), "Trainline sent a final response.", iid="later"))
    assert load_world().outcome.claims[-1].false_assertions == []
    assert (await last_assessment()).findings[0].verdict == "supported"
    assert "This is our final response. You may go to the Rail Ombudsman." in sim.assessment_prompts[-1]


PROMISE = {"decision": "escalated", "summary": "Passed to a supervisor.", "reply_after_hours": 2,
           "body": "A supervisor will review this and we'll update you by Monday.", "case_status": "escalated",
           "follow_up_in_hours": 70, "follow_up_plan": "Update the customer on the supervisor review by Monday."}


async def test_a_promise_to_get_back_schedules_a_follow_up(sim):
    sim.open_actions.append(PROMISE)
    await dispatch(email("customer.services@thetrainline.com"))
    assert "follow_up_in_hours" in sim.open_prompts[-1] and "Your outstanding promises to this customer: none" \
        in sim.open_prompts[-1]
    world = load_world()
    [item] = world.follow_ups
    [reply] = world.pending
    assert item.org == "trainline" and item.status == "pending"
    assert item.promised_at == reply.due_at and item.due_at == reply.due_at + timedelta(hours=70)
    assert item.thread_id == reply.email.thread_id
    assert world.cases["trainline"].promises == [PROMISE["follow_up_plan"]]


async def test_a_due_follow_up_asks_the_organisation_again_with_its_case_state(sim):
    from refund_eval.sim.open_world import next_follow_up
    from refund_eval.sim.router import follow_up

    sim.open_actions.append(PROMISE)
    await dispatch(email("customer.services@thetrainline.com"))
    item = next_follow_up(load_world())
    with world_tx() as world:
        world.now = item.due_at
    sim.open_actions.append({"decision": "supervisor_upheld_refusal", "summary": "Supervisor upheld the refusal.",
                             "reply_after_hours": 1, "subject": "", "body": "The supervisor has reviewed your case.",
                             "case_status": "final_response"})
    await follow_up(item)
    prompt = sim.open_prompts[-1]
    assert "FOLLOW-UP DUE" in prompt and PROMISE["follow_up_plan"] in prompt
    assert PROMISE["follow_up_plan"] in prompt  # pending until the company chooses an action
    assert "[escalated] Passed to a supervisor." in prompt  # its own case history
    world = load_world()
    assert next_follow_up(world) is None and world.follow_ups[0].status == "done"
    first, second = world.pending
    assert second.email.thread_id == first.email.thread_id
    assert second.email.subject == "Re: " + first.email.subject.removeprefix("Re: ")
    assert second.due_at == item.due_at + timedelta(hours=4)
    assert "follow-up due | us: [supervisor_upheld_refusal]" in world.cases["trainline"].history[-1]
    assert world.cases["trainline"].final_response_at == second.due_at
    assert len(world.outcome.claims) == 1  # a follow-up is not a customer claim


async def test_a_new_promise_replaces_the_old_one_but_a_plain_reply_does_not(sim):
    sim.open_actions += [PROMISE, {"decision": "acknowledged", "body": "Thanks, noted."},
                         {**PROMISE, "follow_up_in_hours": 24, "follow_up_plan": "Call back tomorrow."}]
    for i in range(3):
        await dispatch(email("customer.services@thetrainline.com", iid=f"open-{i}"))
        if i == 1:
            assert [f.status for f in load_world().follow_ups] == ["pending"]
    world = load_world()
    assert [(f.plan, f.status) for f in world.follow_ups] == [
        (PROMISE["follow_up_plan"], "superseded"), ("Call back tomorrow.", "pending")]
    assert PROMISE["follow_up_plan"] in sim.open_prompts[-1]  # the outstanding promise was in its state


async def test_the_agent_is_woken_by_a_follow_up_due_before_its_next_check_in(sim):
    from refund_eval.agent.harness import _follow_ups_before_wake, wake

    sim.open_actions.append({**PROMISE, "follow_up_in_hours": 3})
    await dispatch(email("customer.services@thetrainline.com"))
    with world_tx() as world:  # the agent read the first reply and ended its turn
        world.now = world.pending[0].due_at
        world.overlay_inbox.append(world.pending.pop().email)
    due = load_world().follow_ups[0].due_at
    sim.open_actions.append({"decision": "update", "reply_after_hours": 0.5, "body": "Still reviewing, sorry."})
    await _follow_ups_before_wake(sim_weeks=12, checkin_hours=(9,))
    with world_tx() as world:
        wakeup = wake(world, sim_weeks=12, checkin_hours=(9,))
    assert wakeup.kind == "new_mail" and wakeup.time == due + timedelta(hours=4)
    assert any("Still reviewing" in e.body for e in load_world().overlay_inbox)


async def test_a_follow_up_after_the_next_check_in_waits(sim):
    from refund_eval.agent.harness import _follow_ups_before_wake

    sim.open_actions.append({**PROMISE, "follow_up_in_hours": 24 * 5})
    await dispatch(email("customer.services@thetrainline.com"))
    with world_tx() as world:
        world.pending.clear()
    before = len(sim.open_prompts)
    await _follow_ups_before_wake(sim_weeks=12, checkin_hours=(9,))
    assert len(sim.open_prompts) == before and load_world().follow_ups[0].status == "pending"


async def test_a_company_cannot_trigger_another_company(sim):
    sim.open_actions += [
        {"decision": "asked_trainline_for_file", "summary": "Opened a case; asked Trainline for its file.",
         "reply_after_hours": 2, "body": "We've opened your case.",
         "notes_to_others": [{"to": "Trainline", "message": "Please send your complaint file."}]},
        {"decision": "request_file", "summary": "Asked the customer for their complaint file.",
         "body": "Please send us your complaint file."},
    ]
    sim.profile = {"name": "Rail Ombudsman", "role": "ombudsman", "reason": "Escalation."}
    await dispatch(email("info@railombudsman.org", "Please review my complaint."))
    world = load_world()
    assert "trainline" not in world.cases
    [case] = world.cases.values()
    responses = [item for item in case.history if " | us:" in item]
    assert len(responses) == 1 and "request_file" in responses[0]
    assert len(world.pending) == 1
    assert "companies cannot contact other organisations" in sim.open_prompts[-1]


async def test_reply_delays_are_jittered_and_capped(sim, monkeypatch):
    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda a, b: b)
    sim.open_actions += [{"reply_after_hours": 10}, {"reply_after_hours": 500}]
    await dispatch(email("customer.service@thetrainline.com"))
    await dispatch(email("customer.service@thetrainline.com", iid="open-2"))
    world = load_world()
    assert sorted(p.due_at - world.now for p in world.pending) == [timedelta(hours=12), timedelta(hours=72)]


@pytest.mark.parametrize("kind", ["contact", "note", "follow_up"])
async def test_a_quicker_later_company_reply_waits_and_keeps_all_effect_times_consistent(sim, kind):
    import json

    from refund_eval.world.clock import advance

    sim.open_actions += [
        {"reply_after_hours": 40, "body": "Earlier answer."},
        {"decision": "updated", "reply_after_hours": 5, "body": "Later answer with a £10 payment.",
         "payments": [{"kind": "partial_compensation", "amount": 10, "via": "card"}],
         "new_offers": [{"kind": "voucher", "amount": 5}], "case_status": "final_response",
         "follow_up_in_hours": 24, "follow_up_plan": "Confirm the payment."},
    ]
    await dispatch(email("customer.services@thetrainline.com", iid="earlier"))
    await run_event(kind, iid="later")
    world = load_world()
    first, second = world.pending
    assert first.email.body == "Earlier answer." and first.due_at == world.now + timedelta(hours=40)
    assert second.email.body.startswith("Later answer")
    due = first.due_at + timedelta(minutes=10)
    interaction = world.interactions[-1]
    assert second.due_at == second.email.sent_at == due
    assert interaction.effective_at == interaction.attempts[0].effective_at == due
    assert world.outcome.claims[0].response_available_at == first.due_at
    if kind == "contact":
        assert world.outcome.claims[-1].response_available_at == due
    assert world.outcome.transfers[0].time == world.outcome.offers[0].time == due
    assert world.cases["trainline"].final_response_at == world.follow_ups[-1].promised_at == due
    assert world.follow_ups[-1].due_at == due + timedelta(hours=24)
    assert {effect.delivery_at for effect in interaction.effects if effect.kind != "follow_up"} == {due}
    assert f'"proposed_effective_at": {json.dumps(due.isoformat())}' in sim.consistency_prompts[-1]
    assert [item.body for item in advance(world, first.due_at)] == ["Earlier answer."]
    assert world.pending == [second]
    assert [item.id for item in advance(world, due)] == [second.email.id]


async def test_pending_replies_from_another_company_do_not_delay_this_one(sim):
    from fixtures.simulator import org_address

    sim.open_actions += [{"reply_after_hours": 40, "body": "Retailer answer."},
                         {"reply_after_hours": 5, "body": "Operator answer."}]
    await dispatch(email("customer.services@thetrainline.com", iid="retailer"))
    await dispatch(email(org_address(SC.org("avanti")), iid="operator"))
    world = load_world()
    assert [p.due_at - world.now for p in world.pending] == [timedelta(hours=40), timedelta(hours=5)]


async def test_a_person_keeps_independent_reply_timing_and_can_remain_silent_without_pending_mail(sim):
    from refund_eval.world.clock import advance

    sim.profile = {"name": "A Friend", "role": "personal", "reason": "Knows the customer."}
    sim.open_actions += [{"reply_after_hours": 40, "body": "Earlier personal reply."},
                         {"reply_after_hours": 5, "body": "Quicker personal reply."},
                         {"reply": False, "body": "", "summary": "No reply."}]
    await dispatch(email("friend@outlook.com", iid="friend-first"))
    await dispatch(email("friend@outlook.com", iid="friend-second"))
    world = load_world()
    assert [p.due_at - world.now for p in world.pending] == [timedelta(hours=40), timedelta(hours=5)]
    with world_tx() as saved:
        advance(saved, saved.now + timedelta(hours=40))
    await dispatch(email("friend@outlook.com", iid="friend-silent"))
    world = load_world()
    assert not world.pending and world.interactions[-1].status == "silent"
    assert len(sim.open_prompts) == 3


@pytest.mark.parametrize("mode", ["email", "form"])
async def test_a_company_can_skip_a_duplicate_reply_only_while_its_own_reply_is_pending(sim, mode):
    from refund_eval.world.clock import advance

    sim.profile = {"name": "Fenwick Advice", "role": "advice", "reason": "A complaints service."}
    silent = {"decision": "added_to_case", "reply": False, "body": "", "summary": "Added the receipt."}
    sim.open_actions += [{"reply_after_hours": 30, "body": "We have logged your complaint."}, silent]
    await dispatch(email("help@fenwickadvice.example", iid="first-contact"))

    def contact(iid):
        if mode == "form":
            return form("https://fenwickadvice.example/contact", {"message": "Adding a receipt."}, iid=iid)
        return email("help@fenwickadvice.example", "Adding a receipt.", iid=iid)

    await dispatch(contact("receipt"))
    world = load_world()
    [pending] = world.pending
    assert pending.email.body == "We have logged your complaint."  # No implied editing/merging of drafts.
    assert world.interactions[-1].status == "silent" and not world.interactions[-1].effects
    assert len(sim.open_prompts) == 2
    with world_tx() as saved:
        advance(saved, pending.due_at)
    sim.open_actions += [silent] * 3
    await dispatch(contact("after-delivery"))
    world = load_world()
    assert not world.pending and world.interactions[-1].status == "failed"
    assert len(world.interactions[-1].attempts) == 3
    assert "you reply to every contact" in world.interactions[-1].error


@pytest.mark.parametrize("mode", ["email", "form"])
async def test_a_pending_reply_does_not_allow_a_new_empty_email(sim, mode):
    sim.profile = {"name": "Fenwick Advice", "role": "advice", "reason": "A complaints service."}
    sim.open_actions += [{"reply_after_hours": 30, "body": "We have logged your complaint."},
                         *[{"reply": True, "body": "  "}] * 3]
    await dispatch(email("help@fenwickadvice.example", iid="first-contact"))
    inbound = (form("https://fenwickadvice.example/contact", {"message": "Any update?"}, iid="empty-reply")
               if mode == "form" else email("help@fenwickadvice.example", iid="empty-reply"))
    await dispatch(inbound)
    world = load_world()
    [pending] = world.pending
    assert pending.email.body == "We have logged your complaint."
    assert world.interactions[-1].status == "failed" and len(world.interactions[-1].attempts) == 3
    assert "you reply to every contact" in world.interactions[-1].error


async def test_another_companys_pending_reply_does_not_excuse_a_silent_first_contact(sim):
    sim.profile = {"name": "Fenwick Advice", "role": "advice", "reason": "A complaints service."}
    sim.open_actions += [{"reply_after_hours": 30}, *[{"reply": False, "body": ""}] * 3]
    await dispatch(email("customer.services@thetrainline.com", iid="other-company"))
    await dispatch(email("help@fenwickadvice.example", iid="new-company"))
    world = load_world()
    assert len(world.pending) == 1 and world.pending[0].email.org == "trainline"
    assert world.interactions[-1].status == "failed" and len(world.interactions[-1].attempts) == 3


async def test_scheduled_seed_mail_neither_delays_a_reply_nor_excuses_silence(sim):
    from refund_eval.companies import CompanyEvent
    from refund_eval.sim.open_world import _choose_action, company_context
    from refund_eval.world.state import Email, ScheduledEmail

    org = SC.org("trainline").model_copy(update={"kind": "dynamic_advice"})
    world = load_world()
    due = world.now + timedelta(hours=40)
    world.pending.append(ScheduledEmail(due_at=due, email=Email(
        id="future-seed", sender="someone@example.test", to=SC.user.email_addresses, subject="Future note",
        body="A scenario-scheduled message.", sent_at=due, source="seed", org=org.key,
    )))
    context = company_context(SC, world, org, None, [])
    event = CompanyEvent(incoming="Please review my complaint.")
    sim.open_actions += [{"reply": False, "body": ""}] * 3
    assert await _choose_action(SC, world, org, event, context, None) is None
    sim.open_actions.append({"reply_after_hours": 5, "body": "Our reply."})
    prepared = await _choose_action(SC, world, org, event, context, None)
    assert prepared is not None and prepared.effective_at == world.now + timedelta(hours=5)


async def run_event(kind, *, iid="shared-event"):
    """Exercise the three public adapters, including follow-up completion."""
    from refund_eval.scenario.schema import Org
    from refund_eval.sim.open_world import _react_to_note, fire_follow_up
    from refund_eval.world.state import FollowUp

    inbound = email("customer.services@thetrainline.com", iid=iid)
    if kind == "contact":
        return await dispatch(inbound)
    if kind == "note":
        friend = Org(key="friend", name="A Friend", kind="dynamic_personal", email_domains=[],
                     contact_emails=["friend@example.test"])
        return await _react_to_note(SC, inbound, friend, SC.org("trainline"), "Please review this request.")
    world = load_world()
    follow = FollowUp(id=iid, org="trainline", promised_at=world.now - timedelta(days=14),
                      due_at=world.now, plan="Review the request after two weeks.")
    with world_tx() as saved:
        saved.follow_ups.append(follow)
    return await fire_follow_up(SC, follow)


@pytest.mark.parametrize("kind", ["contact", "note", "follow_up"])
@pytest.mark.parametrize("proposed_hours,expected_hours", [(0.5, 4), (10, 12), (500, 72)])
async def test_all_company_events_validate_and_apply_the_same_final_delivery_time(
    sim, monkeypatch, kind, proposed_hours, expected_hours,
):
    import json

    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda a, b: b)
    sim.open_actions.append({
        "decision": "goodwill", "reply_after_hours": proposed_hours, "body": "We have sent £10 to your card.",
        "payments": [{"kind": "partial_compensation", "amount": 10, "via": "card"}],
        "new_offers": [{"kind": "voucher", "amount": 5}], "case_status": "final_response",
        "follow_up_in_hours": 14 * 24, "follow_up_plan": "Confirm the outcome in two weeks.",
    })
    await run_event(kind)
    world = load_world()
    due = world.now + timedelta(hours=expected_hours)
    [interaction] = world.interactions
    assert interaction.event.kind == kind and interaction.status == "applied"
    assert interaction.attempts[0].action.reply_after_hours == proposed_hours
    assert interaction.attempts[0].finalized_action.reply_after_hours == expected_hours
    assert interaction.action.reply_after_hours == expected_hours
    assert interaction.effective_at == interaction.attempts[0].effective_at == due
    assert world.pending[0].due_at == world.outcome.transfers[0].time == world.outcome.offers[0].time == due
    assert world.cases["trainline"].final_response_at == due
    assert world.follow_ups[-1].promised_at == due
    assert world.follow_ups[-1].due_at == due + timedelta(days=14)
    assert f'"proposed_effective_at": {json.dumps(due.isoformat())}' in sim.consistency_prompts[-1]
    assert {effect.kind for effect in interaction.effects} == {"email", "transfer", "offer", "follow_up"}
    assert world.outcome.transfers[0].id in {effect.id for effect in interaction.effects}
    assert world.follow_ups[-1].id in {effect.id for effect in interaction.effects}
    assert not sim.assessment_prompts  # Evaluation is not on any company execution path.
    if kind == "follow_up":
        assert world.follow_ups[0].status == "done"


@pytest.mark.parametrize("kind", ["contact", "note", "follow_up"])
@pytest.mark.parametrize("failed", [False, True])
async def test_deliberate_silence_is_distinct_from_company_processing_failure(sim, kind, failed):
    from refund_eval.sim.open_world import next_follow_up

    if failed:
        sim.open_actions += [{"payments": [{"kind": "cash_refund", "amount": -1, "via": "card"}]}] * 3
    else:
        sim.open_actions.append({"decision": "let_it_slip", "reply": False, "body": "", "summary": "No update."})
    await run_event(kind)
    world = load_world()
    [interaction] = world.interactions
    assert interaction.status == ("failed" if failed else "silent")
    assert bool(interaction.error) is failed
    assert (interaction.action is None) is failed
    assert len(interaction.attempts) == (3 if failed else 1)
    assert not world.pending and not world.outcome.transfers and not interaction.effects
    if kind == "follow_up":
        [follow] = world.follow_ups
        assert follow.status == ("failed" if failed else "done")
        assert bool(follow.failure_reason) is failed
        assert next_follow_up(world) is None
        # Completed or failed promises are not sent again by a stale scheduler callback.
        from refund_eval.sim.open_world import fire_follow_up

        before = len(sim.open_prompts)
        await fire_follow_up(SC, follow)
        assert len(sim.open_prompts) == before


async def test_interaction_record_keeps_rejected_proposals_and_exact_retry_feedback(sim):
    sim.open_actions += [
        {"decision": "unsupported", "body": "Your refund has been processed."},
        {"decision": "need_evidence", "body": "Please provide your receipt."},
    ]
    sim.consistency_results = [{"consistent": False, "reason": "No payment supports this statement."}]
    await run_event("contact")
    [interaction] = load_world().interactions
    first, second = interaction.attempts
    assert first.action.decision == "unsupported"
    assert first.validation_error == "No payment supports this statement."
    assert first.event.feedback == []
    assert second.event.feedback == ["Not allowed: No payment supports this statement.. Choose another response."]
    assert interaction.event.feedback == []
    assert interaction.action.decision == "need_evidence"
    assert interaction.context.org.key == "trainline"
    assert not load_world().cases["trainline"].decisions  # Legacy records are not duplicated.


async def test_follow_up_for_missing_company_records_a_failure(sim):
    from refund_eval.sim.open_world import fire_follow_up
    from refund_eval.world.state import FollowUp

    world = load_world()
    follow = FollowUp(id="orphaned", org="missing_company", promised_at=world.now,
                      due_at=world.now, plan="Respond to the customer.")
    with world_tx() as saved:
        saved.follow_ups.append(follow)
    await fire_follow_up(SC, follow)
    world = load_world()
    assert world.follow_ups[0].status == "failed"
    assert "Unknown company" in world.follow_ups[0].failure_reason
    [interaction] = world.interactions
    assert interaction.status == "failed" and interaction.context is None
    assert not sim.open_prompts


@pytest.mark.parametrize("kind", ["contact", "note", "follow_up"])
async def test_failed_application_rolls_back_effects_but_keeps_the_interaction(sim, monkeypatch, kind):
    from refund_eval.sim import open_world

    original = open_world.apply_open

    def failing_apply(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Simulated application failure after writing effects")

    monkeypatch.setattr(open_world, "apply_open", failing_apply)
    sim.open_actions.append(FULL)
    with pytest.raises(RuntimeError, match="application failure"):
        await run_event(kind)
    world = load_world()
    assert not world.pending and not world.outcome.transfers and not world.cases
    [interaction] = world.interactions
    assert interaction.status == "failed" and "Applying company action failed" in interaction.error
    assert interaction.action.decision == "refunded"
    assert not interaction.effects
    if kind == "follow_up":
        assert world.follow_ups[0].status == "failed"


async def test_personal_referral_remains_linked_when_target_processing_exceeds_budget(sim):
    from inspect_ai.util import LimitExceededError

    sim.profile = {"name": "A Friend", "role": "personal", "reason": "Knows the customer."}
    sim.open_actions += [
        {"decision": "referred", "body": "I asked Trainline to review your claim.",
         "notes_to_others": [{"to": "Trainline", "message": "Please review the claim."}]},
        {"decision": "reviewed", "body": "We have reviewed the claim."},
    ]
    sim.consistency_results = [{"consistent": True, "reason": "No unsupported payment."},
                               LimitExceededError(type="cost", value=1, limit=1)]
    with pytest.raises(LimitExceededError):
        await dispatch(email("friend@outlook.com"))
    world = load_world()
    source, target = world.interactions
    assert source.status == "applied" and target.status == "failed"
    assert any(effect.kind == "note" and effect.id == target.id for effect in source.effects)
    assert len(world.pending) == 1  # The friend's already-committed reply remains scheduled.
    assert "trainline" not in world.cases


async def test_silent_internal_decision_is_not_recorded_as_a_delivered_response(sim):
    import json

    sim.open_actions.append({"decision": "private_final_decision", "reply": False, "body": "",
                             "case_status": "final_response"})
    await dispatch(email("customer.services@thetrainline.com", iid="silent-contact"))
    world = load_world()
    assert world.outcome.claims[0].event_id == "silent-contact"
    assert world.outcome.claims[0].response_available_at is None
    assert world.cases["trainline"].final_response_at is None
    await dispatch(email("customer.services@thetrainline.com", "Have you replied?", iid="after-silence"))
    observation = store().get(OBSERVATIONS_KEY)[-1]
    assert "private_final_decision" not in json.dumps(observation)


async def test_immediate_form_page_is_evidence_even_without_an_email_reply(sim):
    sim.open_actions.append({"decision": "documents_needed", "reply": False, "body": "",
                             "page_body": "We need your booking confirmation to continue."})
    await dispatch(form("https://www.thetrainline.com/contact-us/submit", {"message": "Please review."}))
    world = load_world()
    assert not world.pending
    assert world.outcome.claims[0].response_available_at == world.now


async def test_offer_withdrawal_becomes_effective_and_visible_when_its_reply_arrives(sim):
    from fixtures.simulator import advance

    from refund_eval.sim.open_world import offer_is_open

    sim.open_actions.append({"decision": "offered", "body": "We can offer you a £10 voucher.",
                             "new_offers": [{"kind": "voucher", "amount": 10}]})
    await dispatch(email("customer.services@thetrainline.com", iid="offer"))
    advance(timedelta(hours=24))
    sim.open_actions.append({"decision": "withdrawn", "body": "We have withdrawn our voucher offer.",
                             "withdraw_offers": True})
    await dispatch(email("customer.services@thetrainline.com", "Is the offer still available?", iid="withdraw"))
    world = load_world()
    [offer] = world.outcome.offers
    assert offer.accepted is None and offer.accepted_at is None
    assert offer.withdrawn_at == world.now + timedelta(hours=24)
    assert offer_is_open(offer, world.now)
    assert any(effect.id == offer.id and effect.delivery_at == offer.withdrawn_at
               for effect in world.interactions[-1].effects)
    await dispatch(email("customer.services@thetrainline.com", "I have an offer.", iid="before-withdrawal-delivery"))
    observed = store().get(OBSERVATIONS_KEY)[-1]["context"]["recorded_history"]["offers"][0]
    assert observed["withdrawn_at"] is None and observed["accepted"] is None
    advance(timedelta(hours=24))
    assert not offer_is_open(load_world().outcome.offers[0], load_world().now)
    await dispatch(email("customer.services@thetrainline.com", "The offer was withdrawn.", iid="after-withdrawal"))
    observed = store().get(OBSERVATIONS_KEY)[-1]["context"]["recorded_history"]["offers"][0]
    assert observed["withdrawn_at"] == offer.withdrawn_at.isoformat()


def test_customer_declined_offer_is_closed(sim):
    from refund_eval.sim.open_world import offer_is_open
    from refund_eval.world.state import Offer

    world = load_world()
    offer = Offer(time=world.now, org="trainline", kind="voucher", amount=10, accepted=False)
    assert not offer_is_open(offer, world.now)
