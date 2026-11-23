from refund_eval import task as task_module
from refund_eval.agent.prompt import task_message
from refund_eval.scenario.schema import load_scenario


def test_task_text_override_keeps_scenario_and_default_prompt(monkeypatch):
    scenario = load_scenario("bills_price_rise")
    monkeypatch.setattr(task_module, "load_agent_scenario", lambda _: scenario)
    monkeypatch.setattr(task_module, "_eval_model_name", lambda: "mockllm/model")
    monkeypatch.setattr(task_module, "register_default_costs", lambda *_args, **_kwargs: None)
    instruction = "Review only the actual bills and terms."
    task = task_module.impossible_refund(
        scenario="bills_price_rise", task_text=instruction, task_id="bills_energy_tariff", world_url="http://world",
        cost_limit=None, sim_weeks=12, research_mode="saved", published_data="fixture",
    )
    sample = task.dataset[0]
    assert sample.input == instruction
    assert sample.id == "bills_price_rise"
    assert sample.metadata["scenario"] == "bills_price_rise"
    assert sample.metadata["task_id"] == "bills_energy_tariff"

    default = task_module.impossible_refund(
        scenario="bills_price_rise", world_url="http://world", cost_limit=None,
        sim_weeks=12, research_mode="saved", published_data="fixture",
    )
    assert default.dataset[0].input == task_message(scenario, 12)
    assert "task_id" not in default.dataset[0].metadata
