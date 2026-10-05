from pathlib import Path

import pytest

from refund_eval.scenario.schema import load_scenario
from refund_eval.world.orgs import (
    is_user_address,
    is_user_phone,
    parse_address,
    resolve_org,
)

SCENARIO = load_scenario(str(Path(__file__).parent / "fixtures" / "scenario_tools.yaml"))


@pytest.mark.parametrize(
    "target,org",
    [
        ("customerservices@trainline.com", "trainline"),
        ("Trainline Support <help@trainline.com>", "trainline"),
        ("refunds@info.thetrainline.com", "trainline"),
        ("customers@LNER.co.uk", "lner"),
        ("complaints@mail.lner.co.uk", "lner"),
        ("https://www.thetrainline.com/help/refunds", "trainline"),
        ("thetrainline.com", "trainline"),
        ("www.lner.co.uk/support", "lner"),
        ("https://www.railombudsman.org/make-a-complaint/", "rail_ombudsman"),
        ("0333 202 2222", "trainline"),
        ("+44 333 202 2222", "trainline"),
        ("03457225333", "lner"),
        ("0044 3457 225 225", "lner"),
        ("Monzo", "monzo"),
        ("rail_ombudsman", "rail_ombudsman"),
        ("someone@gmail.com", None),
        ("https://www.nationalrail.co.uk/", None),
        ("0113 496 0000", None),
        ("", None),
        ("notlner.co.uk.evil.com", None),
        ("x@faketrainline.com", None),
    ],
)
def test_resolve_org(target, org):
    assert resolve_org(SCENARIO, target) == org


def test_user_addresses_and_phone():
    assert is_user_address(SCENARIO, "Sam Carter <SAM.CARTER@gmail.com>")
    assert is_user_address(SCENARIO, "sam@carter.me.uk")
    assert not is_user_address(SCENARIO, "sam.carter@outlook.com")
    assert is_user_phone(SCENARIO, "+44 7700 900123")
    assert not is_user_phone(SCENARIO, "07700 900124")


def test_parse_address_rejects_malformed():
    assert parse_address("not an address") is None
    assert parse_address("a@b") is None
    assert parse_address("LNER <customers@lner.co.uk>") == "customers@lner.co.uk"
