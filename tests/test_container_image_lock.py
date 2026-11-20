import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def _dockerfiles():
    legacy = (ROOT / "refund_eval/harbor/__main__.py").read_text()
    legacy = ast.literal_eval(re.search(r'DOCKERFILE = (""".*?""")', legacy, re.DOTALL).group(1))
    return {
        "legacy Harbor": legacy,
        **{
            path: (ROOT / path).read_text()
            for path in (
                "refund_eval/harbor/task_template/environment/Dockerfile",
                "refund_eval/harbor/task_template/environment/world/Dockerfile",
                "refund_eval/harbor/task_template/tests/Dockerfile",
                "refund_eval/published/browser_runtime/Dockerfile",
            )
        },
    }


def test_external_docker_images_match_immutable_lock():
    lock = json.loads((ROOT / "refund_eval/image-lock.json").read_text())
    locked_images = set(lock["images"].values())
    for source, dockerfile in _dockerfiles().items():
        stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", dockerfile, re.MULTILINE))
        references = re.findall(r"^FROM\s+(\S+)|^COPY\s+--from=(\S+)", dockerfile, re.MULTILINE)
        for from_image, copy_image in references:
            image = from_image or copy_image
            if image in stages:
                continue
            assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", image), f"{source}: {image}"
            assert image in locked_images, f"{source}: {image} is absent from image-lock.json"


def test_apt_build_inputs_use_the_locked_debian_snapshot():
    lock = json.loads((ROOT / "refund_eval/image-lock.json").read_text())
    snapshot = lock["debian_snapshot"]
    dockerfiles = _dockerfiles()
    for source, dockerfile in dockerfiles.items():
        if "apt-get update" in dockerfile or "install --with-deps chromium" in dockerfile:
            assert f"snapshot.debian.org/archive/debian/{snapshot}/" in dockerfile, source
            assert f"snapshot.debian.org/archive/debian-security/{snapshot}/" in dockerfile, source
            assert "Check-Valid-Until: no" in dockerfile, source
