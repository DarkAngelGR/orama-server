"""The CI workflow: least privilege, and users' <version> tag moves only after integration."""

from pathlib import Path

import yaml

CI = yaml.safe_load((Path(__file__).resolve().parent.parent / ".github/workflows/ci.yml")
                    .read_text())
JOBS = CI["jobs"]
MAIN_PUSH = "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def _steps_using(job, action):
    return [s for s in JOBS[job]["steps"] if str(s.get("uses", "")).startswith(action)]


def test_default_permissions_are_read_only():
    assert CI["permissions"] == {"contents": "read"}
    for name, job in JOBS.items():
        if name not in ("images", "publish"):
            assert "permissions" not in job or job["permissions"] == {"contents": "read"}


def test_checkouts_do_not_keep_credentials():
    for name in JOBS:
        for step in _steps_using(name, "actions/checkout"):
            assert step.get("with", {}).get("persist-credentials") is False, name


def test_images_job_pushes_only_the_sha_tag():
    builds = _steps_using("images", "docker/build-push-action")
    assert len(builds) == 2
    for step in builds:
        tags = step["with"]["tags"]
        assert "outputs.sha" in tags and "version" not in tags


def test_version_tag_published_only_after_integration_on_main():
    publish = JOBS["publish"]
    assert set(publish["needs"]) == {"unit", "images", "browser", "integration"}
    assert publish["if"] == MAIN_PUSH
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    script = "\n".join(s.get("run", "") for s in publish["steps"])
    assert "for image in orama orama-agent" in script
    assert 'imagetools create' in script
    assert '-t "ghcr.io/darkangelgr/$image:$VERSION" "ghcr.io/darkangelgr/$image:$SHA"' in script
    assert set(JOBS["integration"]["needs"]) >= {"unit", "images"}


def test_browser_job_is_separate_pinned_and_gates_publish():
    browser = JOBS["browser"]
    assert "needs" not in browser  # unit results stay visible whatever the browser does
    assert "browser" in JOBS["publish"]["needs"]
    script = "\n".join(s.get("run", "") for s in browser["steps"])
    assert "playwright@$PLAYWRIGHT_VERSION" in script and "install --with-deps chromium" in script
    assert browser["env"]["PLAYWRIGHT_VERSION"].count(".") == 2  # an exact version
    assert "pytest tests/web/test_browser.py -m integration" in script
    assert _steps_using("browser", "actions/setup-node")
