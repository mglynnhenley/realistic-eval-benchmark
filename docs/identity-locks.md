# Reproducibility identity locks

An identity lock is an opt-in, reusable manifest for a local Docker-browser
run. It binds the environment version and fingerprint, resolved scenario, and
the browser image ID already present on this host:

    uv run python -m refund_eval.run_identity \
      --scenario train_refund_late \
      --output .context/identity-locks/train-refund-late.json

The default Docker runtime must already have the requested browser image in the
active local Docker daemon. The command records its immutable image ID; it does
not pull an image or change a tag. Pass the manifest to an Inspect task with
the Inspect setting identity_lock=.context/identity-locks/train-refund-late.json,
or set identity_lock in a dashboard RunConfig.

The update workflow is deliberately explicit. Recreate a manifest at a new
path, or pass --update to replace one after reviewing the change. A mismatch
in source/environment, scenario, runtime, or unavailable local Docker image
fails closed before model-cost setup; no newer tag is substituted. Docker
launches use the recorded image ID, not the mutable tag.

The Docker image ID is an identity check for the active host's local artifact,
not a distribution mechanism. A different host must already have that exact
image available; cross-host distribution is not configured or verified here.
Exact artifact locks are unsupported for Harbor subprocess/browser builds:
their resulting artifacts have no immutable identity available here. Native
Harbor records source and rules identity from the generated sidecar task; it
does not treat the installed agent wheel as the runtime identity.
The checked-in refund_eval/image-lock.json pins external OCI build inputs,
and the Dockerfiles use the fixed Debian snapshot 20261118T000000Z.

Update a build input only after verifying its full OCI digest; update the
manifest and every matching Dockerfile reference together. The offline
container-image-lock test checks that all references stay in sync. Rebuilds and
publication are intentionally outside this workflow.
