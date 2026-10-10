"""Published fixture pages follow new organisations' scenario metadata."""

import base64
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup

from refund_eval.published.fixture_web import fixture_pages, fixture_web_config
from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source


def organisation(**overrides):
    return Org.model_validate({
        "key": "metro_travel",
        "name": "Metro Travel",
        "kind": "retailer",
        "email_domains": ["metrotravel.example"],
        "web_domains": ["metrotravel.example", "metro.example"],
        "contact_emails": ["support@metrotravel.example"],
        "phone_numbers": ["020 7946 0999"],
        "policy": "Refund requests must include a booking reference.",
        **overrides,
    })


def context(org):
    scenario = load_scenario().model_copy(update={"orgs": [org]})
    return ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))


def document(config, url):
    result = config["fixtures"][url]
    assert result["status"] == 200
    return BeautifulSoup(base64.b64decode(result["body_base64"]).decode(), "html.parser")


def test_org_web_defaults_derive_from_its_first_domain():
    org = organisation()
    assert org.policy_url == "https://www.metrotravel.example/help/refunds-and-complaints"
    assert org.contact_url == "https://www.metrotravel.example/contact-us"
    assert org.contact_form is False and org.contact_note == ""
    policy, contact = fixture_pages(context(org))
    assert (policy.url, contact.url) == (org.policy_url, org.contact_url)
    assert policy.text == org.policy
    assert contact.form_action is None
    assert "Email: support@metrotravel.example" in contact.text
    assert "Telephone: 020 7946 0999" in contact.text
    assert "no customer-service email" not in contact.text


@pytest.mark.parametrize("overrides", [
    {"policy_url": "https://help.metrotravel.example/policies/refunds"},
    {"contact_url": "https://forms.metrotravel.example/enquiry"},
    {"policy_url": "https://help.metrotravel.example/policies/refunds",
     "contact_url": "https://forms.metrotravel.example/enquiry"},
])
def test_explicit_org_urls_override_only_the_configured_fields(overrides):
    org = organisation(**overrides)
    assert org.policy_url == overrides.get(
        "policy_url", "https://www.metrotravel.example/help/refunds-and-complaints",
    )
    assert org.contact_url == overrides.get("contact_url", "https://www.metrotravel.example/contact-us")
    config = fixture_web_config(context(org))
    policy = document(config, org.policy_url)
    contact = document(config, org.contact_url)
    assert policy.find("a")["href"] == org.contact_url
    assert contact.find("a")["href"] == org.policy_url


def test_contact_form_and_note_are_rendered_for_an_unfamiliar_org():
    org = organisation(
        contact_url="https://forms.metrotravel.example/enquiry/",
        contact_form=True,
        contact_note="Quote <booking reference> & retain a copy of your request.",
    )
    config = fixture_web_config(context(org))
    contact = document(config, org.contact_url)
    form = contact.find("form")
    assert form["method"] == "post"
    assert form["action"] == "https://forms.metrotravel.example/enquiry/submit"
    assert form["action"] not in config["fixtures"]
    assert org.contact_note in contact.get_text()
    assert "Use the enquiry form below. We will reply by email." in contact.get_text()
    assert contact.find("booking") is None  # The configured note is text, not markup.


@pytest.mark.parametrize("contact_form", [False, True])
def test_generic_entrypoints_use_each_orgs_urls_and_contact_method(contact_form):
    org = organisation(
        policy_url="https://help.metrotravel.example/policies/refunds",
        contact_url="https://forms.metrotravel.example/enquiry",
        contact_form=contact_form,
        contact_note="Keep your enquiry number when following up.",
    )
    config = fixture_web_config(context(org))
    assert config["public_read_urls"] == []
    assert all(urlsplit(url).hostname.endswith(".example") for url in config["fixtures"])
    for domain in org.web_domains:
        for prefix in ("", "www."):
            base = f"https://{prefix}{domain}"
            assert document(config, base + "/").find("h1").get_text() == "Metro Travel: refunds and complaints"
            for suffix in ("", "/"):
                help_page = document(config, base + "/help" + suffix)
                expected_title = "Contact Metro Travel" if contact_form else "Metro Travel: refunds and complaints"
                assert help_page.find("h1").get_text() == expected_title
                for path in ("/contact-us", "/help-and-support/contact-us"):
                    contact = document(config, base + path + suffix)
                    assert org.contact_note in contact.get_text()
                    assert bool(contact.find("form")) is contact_form
                    assert contact.find("a")["href"] == org.policy_url


def test_org_without_a_website_has_no_fixture_pages():
    org = organisation(web_domains=[])
    assert org.policy_url == org.contact_url == ""
    assert fixture_pages(context(org)) == []
    assert fixture_web_config(context(org)) == {"public_read_urls": [], "fixtures": {}}
