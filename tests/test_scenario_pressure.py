"""The pressure variant: the base world plus Laura's chasers, quoted terms and a faster close."""

from refund_eval.agent.harness import initial_world, wake
from refund_eval.scenario.schema import load_scenario


def test_variant_extends_the_base_scenario():
    base, pressure = load_scenario("train_refund"), load_scenario("train_refund_pressure")
    assert pressure.id == "train_refund_pressure" and pressure.task_text == base.task_text
    assert [o.key for o in pressure.orgs] == [o.key for o in base.orgs]
    assert pressure.seed_emails == base.seed_emails
    trainline = pressure.org("trainline")
    assert "exceptional circumstances" in trainline.terms
    assert trainline.policy == base.org("trainline").policy  # merged, not replaced
    assert not base.org("trainline").terms and not base.scheduled_emails


def test_laura_writes_to_herself_during_the_trial():
    scenario = load_scenario("train_refund_pressure")
    world = initial_world(scenario)
    assert [p.email.id for p in world.pending] == [e.id for e in scenario.scheduled_emails]
    assert not any(e.id.startswith("laura-chase") for e in world.overlay_inbox)
    while (wakeup := wake(world, sim_weeks=6)) is not None and wakeup.kind != "new_mail":
        pass
    [chase] = [e for e in world.overlay_inbox if e.id == "laura-chase-1"]
    assert wakeup.time == chase.sent_at and chase.sender.endswith("<laura.bennett.w17@gmail.com>")
    assert chase.to == ["laura.bennett.w17@gmail.com"]
