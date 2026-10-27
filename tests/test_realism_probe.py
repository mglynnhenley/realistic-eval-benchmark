import json

from refund_eval.realism.probe import findings, load_probes, sandbox_facts

HEADLESS = {"userAgent": "Mozilla/5.0 HeadlessChrome/155.0", "webdriver": True, "languages": ["en-US"],
            "timezone": "UTC", "plugins": 0, "webgl": "ANGLE (SwiftShader driver)"}
REAL = {"userAgent": "Mozilla/5.0 Chrome/155.0", "webdriver": False, "languages": ["en-GB", "en"],
        "timezone": "Europe/London", "plugins": 5, "webgl": "ANGLE (Apple M3)"}


def test_headless_sandbox_fails_every_check():
    facts = {"browser": HEADLESS, "fetch_headers": {"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
             "kernel": "Linux 4.4.0", "tz": "UTC", "home": "/root"}
    found = findings(facts, source="t/probe.json")
    assert len(found) == 11
    assert {f["factor"] for f in found} == {"sandbox", "internet"}
    assert all(f["noticed"] == "possible" and f["log"] == "t/probe.json" for f in found)


def test_real_looking_machine_and_missing_facts_pass():
    facts = {"browser": REAL, "fetch_headers": {"User-Agent": "Mozilla/5.0 (X11) Chrome/155.0", "Accept": "text/html"},
             "kernel": "Linux 6.8.0", "tz": "GMT", "home": "/home/laura"}
    assert findings(facts) == []
    assert findings({"browser": {"error": "not installed"}}) == []


def test_load_probes_and_facts(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.realism.probe.browser_facts", lambda: {"webdriver": True})
    facts = sandbox_facts()
    assert {"hostname", "kernel", "tz", "fetch_headers", "browser"} <= facts.keys()
    path = tmp_path / "probe.json"
    path.write_text(json.dumps(facts))
    assert any(f["quote"] == "browser.webdriver: True" for f in load_probes([str(path)]))
