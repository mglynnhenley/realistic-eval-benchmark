"""Free parity gate: scripted fixture-mode trials on the native Inspect path and on the
native Harbor task's path (in-process and in local Docker), compared world, event log and grades.

    uv run python -m refund_eval.parity --out .context/parity [--scripts NAME ...] [--skip-harbor]

Test only: a `mockllm/model` target driven by a fixed script (`scripts`), the world's fake models
(`harbor.world_entrypoint.FakeWorldModels`), fixture data and a fixed seed. No API keys, no Modal.
"""
