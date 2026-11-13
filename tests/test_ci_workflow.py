"""The CI workflow: least privilege, and users' <version> tag moves only after integration."""

from pathlib import Path

import yaml

CI_ROOT = Path(__file__).resolve().parent.parent
CI = yaml.safe_load((CI_ROOT / ".github/workflows/ci.yml").read_text())
E2E = yaml.safe_load((CI_ROOT / ".github/workflows/e2e.yml").read_text())
JOBS = CI["jobs"]
MAIN_PUSH = "github.event_name == 'push' && github.ref == 'refs/heads/main'"


def _steps_using(job, action, jobs=JOBS):
    return [s for s in jobs[job].get("steps", []) if str(s.get("uses", "")).startswith(action)]


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


def test_every_push_runs_ci_except_the_scratch_branch():
    # `e2e-dev` is the throw-away branch the end-to-end jobs are tried on (Plan 5 Task 3);
    # pushes to any other branch, `main` and `test` included, must still run all of CI.
    triggers = CI[True]  # PyYAML reads the bare key `on` as the boolean True
    assert triggers["push"] == {"branches-ignore": ["e2e-dev"]}
    assert "pull_request" in triggers


def test_e2e_workflow_runs_after_unit_and_images_with_least_privilege():
    e2e_caller = JOBS["e2e"]
    assert e2e_caller["uses"] == "./.github/workflows/e2e.yml"
    assert set(e2e_caller["needs"]) >= {"unit", "images"}
    assert E2E["permissions"] == {"contents": "read"}
    triggers = E2E[True]
    assert "workflow_call" in triggers
    assert triggers["push"] == {"branches": ["e2e-dev"]}

    ubuntu = E2E["jobs"]["e2e-ubuntu"]
    assert ubuntu["runs-on"] == "ubuntu-24.04"
    assert ubuntu["timeout-minutes"] == 60
    assert ubuntu["env"]["PLAYWRIGHT_VERSION"].count(".") == 2
    for step in _steps_using("e2e-ubuntu", "actions/checkout", jobs=E2E["jobs"]):
        assert step.get("with", {}).get("persist-credentials") is False
    script = "\n".join(s.get("run", "") for s in ubuntu["steps"])
    assert "playwright@$PLAYWRIGHT_VERSION" in script and "install --with-deps chromium" in script
    assert "ORAMA_E2E_TARGET=ubuntu" in script
    assert "tests/e2e/test_e2e.py" in script
    uploads = _steps_using("e2e-ubuntu", "actions/upload-artifact", jobs=E2E["jobs"])
    assert len(uploads) == 1
    assert uploads[0]["if"] == "failure()"
    assert uploads[0]["with"]["name"] == "e2e-logs-ubuntu"



def test_integration_pulls_docker_hub_images_through_a_mirror():
    """Docker Hub's anonymous pull limit made the real-app job fail at random (twice)."""
    steps = JOBS["integration"]["steps"]
    names = [s.get("name", "") for s in steps]
    mirror = names.index("Docker Hub images through a mirror")
    assert mirror < names.index("Integration test")
    assert steps[mirror]["run"] == ".github/scripts/docker-hub-mirror.sh"
    script = (Path(__file__).resolve().parents[1] / ".github/scripts/docker-hub-mirror.sh").read_text()
    assert 'MIRROR="https://mirror.gcr.io"' in script and "registry-mirrors" in script
