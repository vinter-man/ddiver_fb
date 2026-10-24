"""The brain's deployment files: nothing listens beyond the VPS's loopback (D6), and the build
can fetch the private core without leaving a secret behind.

None of this runs in CI's containers, but all of it is load-bearing on the VPS: a port published
on 0.0.0.0 is the ingest API on the internet, with a bearer token as the only lock.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import yaml

from ddiver_fb.config import FbSettings
from ddiver_fb.health import MAX_AGE_SECONDS

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
ENV_EXAMPLE = ROOT / ".env.example"
KNOWN_HOSTS = ROOT / "deploy/brain/github_known_hosts"
DOCKERIGNORE = ROOT / ".dockerignore"
LOCK = ROOT / "uv.lock"
PYPROJECT = ROOT / "pyproject.toml"
CORE_REPO = "https://github.com/vinter-man/ddiver"
FAKES = "ddiver-core-testing"
IMAGE_SYNC = ("--frozen", "--no-dev", "--package ddiver-fb")
"""Every ``uv sync`` of the image: the lock as committed, no dev group, the brain alone."""
MORE_GROUPS = frozenset(
    ("--dev", "--group", "--all-groups", "--only-group", "--only-dev", "--extra", "--all-extras")
)
"""Flags that would install a group or an extra next to the brain's own dependencies."""

GITHUB_FINGERPRINTS = {
    # https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
    "ED25519": "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU",
    "ECDSA": "SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM",
    "RSA": "SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s",
}


def compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def stages() -> list[str]:
    """The Dockerfile split at each FROM: the builder first, the runtime stage last."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    return re.split(r"^FROM ", text, flags=re.MULTILINE)[1:]


def locked() -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]}


def pinned_core() -> dict[str, dict[str, str]]:
    """``[tool.uv.sources]`` of the core and its fakes, as pyproject.toml pins them."""
    sources = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"]["sources"]
    return {name: sources[name] for name in ("ddiver-core", FAKES)}


def runtime_closure(name: str) -> set[str]:
    """Every package the lock installs for ``name`` without groups: its dependencies, theirs,
    and the optional ones a dependency asks for by extra."""
    packages = locked()
    found: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        if current in found:
            continue
        found.add(current)
        for dependency in packages[current].get("dependencies", []):
            todo.append(dependency["name"])
            optional = packages[dependency["name"]].get("optional-dependencies", {})
            for extra in dependency.get("extra", []):
                todo.extend(d["name"] for d in optional[extra])
    return found


def services() -> dict[str, dict[str, Any]]:
    found = compose()["services"]
    assert isinstance(found, dict)
    return found


class TestNothingListensBeyondLoopback:
    def test_every_published_port_is_bound_to_127_0_0_1(self):
        for name, service in services().items():
            for published in service.get("ports", []):
                text = str(published)
                assert text.startswith("127.0.0.1:"), f"{name} publishes {text}"
                assert text.count(":") == 2, f"{name}: host address, host port, container port"

    def test_the_ingest_port_is_published_and_matches_the_default(self):
        (brain,) = services().values()
        assert brain["ports"] == [
            f"127.0.0.1:{FbSettings.model_fields['ingest_port'].default}:8781"
        ]
        assert brain["environment"]["INGEST_PORT"] == "8781"

    def test_no_host_networking_and_no_long_form_ports(self):
        for name, service in services().items():
            assert service.get("network_mode") != "host", name
            for published in service.get("ports", []):
                assert isinstance(published, str), f"{name}: long form hides host_ip defaults"
            assert "expose" not in service or all(str(p).isdigit() for p in service["expose"]), name

    def test_only_the_container_binds_every_interface(self):
        """0.0.0.0 is right inside the container (compose maps it to loopback) and nowhere else."""
        assert FbSettings.model_fields["ingest_host"].default == "127.0.0.1"
        (brain,) = services().values()
        assert brain["environment"]["INGEST_HOST"] == "0.0.0.0"  # noqa: S104 - the container's
        assert "INGEST_HOST=0.0.0.0" in DOCKERFILE.read_text(encoding="utf-8")


class TestOperations:
    def test_the_project_is_named_apart_from_ddiver(self):
        assert compose()["name"] == "ddiver-fb"

    def test_limits_restart_and_healthcheck(self):
        (brain,) = services().values()
        assert brain["mem_limit"] and brain["cpus"]
        assert brain["restart"] == "unless-stopped"
        assert brain["healthcheck"]["test"] == ["CMD", "ddiver-fb", "health"]
        interval = int(str(brain["healthcheck"]["interval"]).rstrip("s"))
        # the heartbeat is written every 30 s; a check must be able to find a fresh one
        assert interval <= MAX_AGE_SECONDS

    def test_the_data_directory_is_a_volume(self):
        (brain,) = services().values()
        assert "./data:/data" in brain["volumes"]
        assert brain["environment"]["DATA_DIR"] == "/data"

    def test_the_commit_reaches_the_image(self):
        (brain,) = services().values()
        assert "GIT_SHA" in brain["build"]["args"]
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "ARG GIT_SHA" in text and "DDIVER_COMMIT=$GIT_SHA" in text

    def test_the_first_deploy_creates_the_data_directory_for_the_brains_user(self):
        """Round 2 C-F5: Docker creates a missing bind mount as root, and the brain (uid
        10002) then crash-looped on its lock file."""
        from ddiver_fb.__main__ import CONTAINER_UID

        text = DOCKERFILE.read_text(encoding="utf-8")
        assert f"--uid {CONTAINER_UID} " in text
        runbook = (DOCKERFILE.parent / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        uid = CONTAINER_UID
        assert f"install -d -o {uid} -g {uid} -m 700 /srv/ddiver-fb/data" in runbook

    def test_the_image_runs_the_brain_as_a_user(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert re.search(r"^USER \w+", text, re.MULTILINE)
        assert 'ENTRYPOINT ["ddiver-fb"]' in text
        assert 'CMD ["ddiver-fb", "health"]' in text


class TestPrivateSource:
    def test_the_builder_has_git_and_ssh_and_uses_the_ssh_mount(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "git openssh-client" in text
        syncs = [line for line in text.splitlines() if "uv sync" in line]
        assert syncs and all(flag in line for line in syncs for flag in IMAGE_SYNC), syncs
        for line in syncs:  # no way back in for a dev group or an extra that holds the fakes
            assert not {word.partition("=")[0] for word in line.split()} & MORE_GROUPS, line
        mounts = re.findall(r"RUN --mount=type=cache[^\n]*--mount=type=ssh", text)
        assert len(mounts) == len(syncs)

    def test_github_is_pinned_not_trusted_on_first_use(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "StrictHostKeyChecking=yes" in text
        assert "COPY deploy/brain/github_known_hosts /etc/ssh/ssh_known_hosts" in text
        assert "ssh-keyscan" not in text and "StrictHostKeyChecking=no" not in text
        (brain,) = services().values()
        assert brain["build"]["ssh"] == ["default"]

    def test_the_pinned_keys_are_the_ones_github_publishes(self):
        lines = [line for line in KNOWN_HOSTS.read_text(encoding="utf-8").splitlines() if line]
        assert all(line.startswith("github.com ") for line in lines)
        try:
            result = subprocess.run(  # noqa: S603 - a fixed command on a file of this repo
                ["ssh-keygen", "-lf", str(KNOWN_HOSTS)],  # noqa: S607
                capture_output=True,
                text=True,
                check=True,
            )
        except FileNotFoundError:  # pragma: no cover - a machine without OpenSSH
            pytest.skip("ssh-keygen is not installed")
        found = {
            line.split()[-1].strip("()"): line.split()[1] for line in result.stdout.splitlines()
        }
        assert found == GITHUB_FINGERPRINTS

    def test_the_builder_fetches_githubs_https_urls_over_ssh(self):
        """The lock pins the core as an https URL (the Mac and CI read it that way); only the
        builder, which has nothing but the ssh mount, rewrites it."""
        builder, runtime = stages()
        rewrite = 'git config --system url."ssh://git@github.com/".insteadOf "https://github.com/"'
        assert rewrite in builder
        assert "insteadOf" not in runtime and "git" not in runtime.split()

    def test_the_header_says_how_to_build(self):
        header = DOCKERFILE.read_text(encoding="utf-8").split("FROM ", 1)[0]
        assert "docker compose build" in header and "docker build --ssh default" in header
        assert "ssh-add" in header and "core-v" in header

    def test_the_first_deploy_builds_with_the_deploy_key_in_an_agent(self):
        runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        section = runbook.split("## 10.", 1)[1].split("\n## ", 1)[0]
        steps = ["ssh-agent -s", "ssh-add", "docker compose up -d --build", "ssh-agent -k"]
        found = [section.index(step) for step in steps]
        assert found == sorted(found)
        assert "GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build" in section

    def test_the_lock_pins_the_core_and_its_fakes_to_one_tag(self):
        """The tag pyproject.toml names, not merely some tag: the image installs with
        ``--frozen``, which reads the lock as it is, so a new tag in pyproject.toml without its
        lock would build the old core while pyproject.toml says the new one."""
        packages = locked()
        pins = pinned_core()
        commits = set()
        for name, pin in pins.items():
            assert set(pin) == {"git", "tag", "subdirectory"}, pin  # a tag, no branch, no rev
            assert pin["git"] == CORE_REPO and pin["subdirectory"] == f"packages/{name}", pin
            assert re.fullmatch(r"core-v\d+\.\d+\.\d+", pin["tag"]), pin
            source = packages[name]["source"]["git"]
            url, _, commit = source.partition("#")
            repo, _, query = url.partition("?")
            assert repo == CORE_REPO, source
            wanted = {"tag": [pin["tag"]], "subdirectory": [pin["subdirectory"]]}
            assert parse_qs(query, strict_parsing=True) == wanted, (source, pin)
            assert re.fullmatch(r"[0-9a-f]{40}", commit), source
            commits.add(commit)
        assert len(commits) == 1 and len({pin["tag"] for pin in pins.values()}) == 1

    def test_the_lock_holds_no_local_path(self):
        assert "/Users/" not in LOCK.read_text(encoding="utf-8")
        for name, package in locked().items():
            source = package["source"]
            for kind in ("path", "editable", "directory", "virtual"):
                if kind in source:
                    assert not Path(source[kind]).is_absolute(), name
                    assert source[kind] == "." or source[kind].startswith("packages/"), name

    def test_the_fakes_are_a_dev_dependency_only(self):
        packages = locked()
        brain = [d["name"] for d in packages["ddiver-fb"].get("dependencies", [])]
        assert "ddiver-core" in brain
        installed = runtime_closure("ddiver-fb")
        assert {"ddiver-core", "fbwire", "telethon"} <= installed
        assert FAKES not in installed and "pytest" not in installed
        for member in ("ddiver-fb", "fbwire"):  # the image's packages: no group, no extra
            for kind in ("dev-dependencies", "optional-dependencies"):
                for group, deps in packages[member].get(kind, {}).items():
                    assert FAKES not in {d["name"] for d in deps}, (member, kind, group)
        dev = [d["name"] for d in packages["ddiver-facebook"]["dev-dependencies"]["dev"]]
        assert FAKES in dev
        runtime = [d["name"] for d in packages["ddiver-facebook"].get("dependencies", [])]
        assert FAKES not in runtime

    def test_the_context_leaves_the_boxs_secrets_behind(self):
        patterns = [
            line.strip()
            for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        for needed in (".env", "data/", ".git/", ".venv/", "*.sqlite"):
            assert needed in patterns, needed
        copied = [
            source
            for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
            if line.startswith("COPY ") and "--from=" not in line
            for source in [w for w in line.split()[1:] if not w.startswith("--")][:-1]
        ]
        assert "uv.lock" in copied and "deploy/brain/github_known_hosts" in copied
        for source in copied:
            for pattern in patterns:
                if not pattern.startswith("!"):
                    assert not fnmatch.fnmatch(source, pattern.rstrip("/")), (source, pattern)
                    assert not source.startswith(pattern.rstrip("/") + "/"), (source, pattern)

    def test_no_secret_is_copied_into_the_image(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        for secret in (".env", "id_rsa", "id_ed25519", "GITHUB_TOKEN", "secrets"):
            copies = [ln for ln in text.splitlines() if ln.startswith(("COPY", "ADD"))]
            assert not any(secret in ln for ln in copies), secret


class TestEnvExample:
    def test_every_required_setting_is_named_and_empty(self):
        values = {}
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                name, _, value = line.partition("=")
                values[name] = value
        assert values == {
            "BOT_TOKEN": "",
            "API_ID": "",
            "API_HASH": "",
            "OWNER_ID": "",
            "INGEST_TOKEN": "",
        }

    def test_it_is_not_ignored_while_the_real_file_is(self):
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".env" in ignored and ".env.example" not in ignored
