"""Model calls made by the world side (simulator and grader roles).

`client.py` is the interface and does not import Inspect. `inspect_client.py` is the
default backend; `openrouter.py` calls OpenRouter directly with the `openai` SDK.
"""
