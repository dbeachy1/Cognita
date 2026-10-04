"""Static acceptance checks for the Cognita 12 container foundation.

These checks do not require Docker and intentionally validate the isolation
contract before an operator spends time building or pulling images.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

from cognita import __version__
from cognita.config import load_config
from cognita.release_identity import APPLICATION_VERSION, TOOLBOX_VERSION

ROOT = Path(__file__).parents[1]

CACHE_ENVIRONMENT = {
    "COGNITA_ACCELERATION_PROFILE": "cpu",
    "HOME": "/var/lib/cognita/models",
    "XDG_CACHE_HOME": "/var/lib/cognita/models/.cache",
    "HF_HOME": "/var/lib/cognita/models/.cache/huggingface",
    "HF_HUB_CACHE": "/var/lib/cognita/models/.cache/huggingface/hub",
    "HF_XET_CACHE": "/var/lib/cognita/models/.cache/huggingface/xet",
    "HF_ASSETS_CACHE": "/var/lib/cognita/models/.cache/huggingface/assets",
    "TRANSFORMERS_CACHE": "/var/lib/cognita/models/.cache/huggingface/transformers",
}


def _compose() -> dict:
    return yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def test_release_manifest_has_reviewed_upstream_digests() -> None:
    """The manifest pins upstream images and nothing else.

    13.0 §8: it no longer carries a `release` field or a `tag` per built
    image.  `release_identity.py` owns the application and Toolbox versions,
    and a second copy here was a place a stale version could hide.
    (Superseded: through 12.x this test compared both against the manifest.)
    """
    manifest = json.loads((ROOT / "containers/image-manifest.json").read_text(encoding="utf-8"))
    assert manifest["platform"] == "linux/amd64"
    assert "release" not in manifest
    for item in manifest["built"].values():
        assert not {"tag", "digest", "status"} & set(item)
    for item in manifest["upstream"].values():
        assert item["index_digest"].startswith("sha256:")
        assert item["linux_amd64_manifest"].startswith("sha256:")


def test_cpu_base_is_digest_pinned() -> None:
    # 13.0 §8: the "pending-release-build" status and the null digest are
    # gone with the evidence scripts that were the only thing ever meant to
    # fill them; the placeholder check below stays, because a placeholder left
    # in a base reference would silently unpin a base image.
    manifest = json.loads((ROOT / "containers/image-manifest.json").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "containers/cognita/Dockerfile").read_text(encoding="utf-8")
    cpu_base = manifest["built"]["cognita"]["base_reference"]
    assert re.fullmatch(r"python:3\.13-slim-bookworm@sha256:[0-9a-f]{64}", cpu_base)
    assert f"PYTHON_BASE_IMAGE={cpu_base}" in dockerfile
    for image in manifest["built"].values():
        assert "RECORD_DURING_RELEASE_BUILD" not in str(image)
        assert "PREPULL_AND_RECORD_DURING_RELEASE_BUILD" not in str(image)


def test_workspace_toolbox_base_is_digest_pinned() -> None:
    manifest = json.loads((ROOT / "containers/image-manifest.json").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "containers/workspace-toolbox/Dockerfile").read_text(encoding="utf-8")
    toolbox_base = manifest["built"]["workspace_toolbox"]["base_reference"]
    assert re.fullmatch(r"debian:bookworm-slim@sha256:[0-9a-f]{64}", toolbox_base)
    assert f"TOOLBOX_BASE_IMAGE={toolbox_base}" in dockerfile


def test_workspace_toolbox_package_lock_matches_dockerfile() -> None:
    dockerfile = (ROOT / "containers/workspace-toolbox/Dockerfile").read_text(encoding="utf-8")
    package_block = re.search(
        r"apt-get install --no-install-recommends -y \\\s*(?P<packages>.*?)\\\s*&& rm",
        dockerfile,
        re.DOTALL,
    )
    assert package_block is not None
    installed = re.findall(r"[a-z0-9][a-z0-9+.-]*", package_block.group("packages"))
    locked = [
        line
        for raw_line in (ROOT / "containers/workspace-toolbox/packages.lock").read_text(
            encoding="utf-8"
        ).splitlines()
        if (line := raw_line.strip()) and not line.startswith("#")
    ]
    assert installed == locked


def test_compose_release_surfaces_match_the_package_version() -> None:
    """Every Cognita version literal in compose.yaml is the one authority's.

    13.0 §4: `release_identity.APPLICATION_VERSION` owns the version and
    `__version__` derives from it, so this compares against the authority
    directly.  The scan used to be a `\\b12\\.\\d+\\.\\d+\\b` regex, which could
    only ever match the 12.x line; it now matches Cognita image references of
    any version, and deliberately not the digest-pinned upstream images, whose
    versions are independent pins.
    """
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    doc = yaml.safe_load(compose_text)
    cognita = doc["services"]["cognita"]
    workspace = yaml.safe_load((ROOT / "compose.workspace.yaml").read_text(encoding="utf-8"))
    runtime = workspace["services"]["workspace-runtime"]

    assert APPLICATION_VERSION == __version__
    # No Cognita version literal at all: every one of these interpolates
    # COGNITA_VERSION from the target's env file, which release.py writes from
    # the authority.  A literal here is a second place a stale version hides,
    # which is exactly what 12.17.0 did in this file until 13.0.
    assert not re.search(r"\bcognita[a-z-]*:\d+\.\d+\.\d+\b", compose_text)
    required = "${COGNITA_VERSION:?COGNITA_VERSION is required}"
    assert cognita["build"]["args"]["COGNITA_VERSION"] == required
    assert cognita["image"] == f"cognita:{required}"
    assert runtime["build"]["args"]["COGNITA_VERSION"] == required
    assert runtime["image"] == f"cognita-workspace-runtime:{required}"
    assert runtime["environment"]["COGNITA_VERSION"] == required


def test_release_image_inputs_match_the_package_version() -> None:
    manifest = json.loads((ROOT / "containers/image-manifest.json").read_text(encoding="utf-8"))
    built = manifest["built"]
    # The Toolbox has its own literal in the same authority, and it moves only
    # when the Toolbox Dockerfile does -- existing sandboxes stay valid.
    # (Superseded: this used to be read out of the manifest's per-image `tag`,
    # which 13.0 §8 removed; the authority is the only owner now.)
    toolbox_version = TOOLBOX_VERSION
    for name, item in built.items():
        dockerfile = (ROOT / item["dockerfile"]).read_text(encoding="utf-8")
        if name == "workspace_toolbox":
            # The Toolbox is versioned independently of the application and
            # its label is what the broker's loader checks, so that default
            # stays.
            assert f"ARG COGNITA_VERSION={toolbox_version}" in dockerfile
            continue
        # 13.0 §4: no default -- the build arg is always passed, and a default
        # would be a second place a stale version could hide.
        assert re.search(r"^ARG COGNITA_VERSION$", dockerfile, re.MULTILINE)
        assert 'org.opencontainers.image.version="${COGNITA_VERSION}"' in dockerfile
        # 13.0: every release image records the commit it was built from.
        assert re.search(r"^ARG COGNITA_COMMIT$", dockerfile, re.MULTILINE)
        assert 'org.opencontainers.image.revision="${COGNITA_COMMIT}"' in dockerfile

    amd = yaml.safe_load((ROOT / "compose.amd.yaml").read_text(encoding="utf-8"))
    cognita = amd["services"]["cognita"]
    required = "${COGNITA_VERSION:?COGNITA_VERSION is required}"
    assert cognita["build"]["args"]["COGNITA_VERSION"] == required
    assert cognita["image"] == f"cognita-amd:{required}"
    assert (
        cognita["environment"]["COGNITA_GPU_PROGRAM_CACHE_DIR"]
        == "/var/lib/cognita/models/migraphx-cache"
    )

    toolbox_tag = f"cognita-workspace-toolbox:{toolbox_version}"
    archive_name = f"toolbox-{toolbox_version}.tar"
    # 13.0 §8: `preflight-kvm.sh` is deleted; `release.py` builds, stages and
    # loads the Toolbox itself and derives both names from the authority.
    release_script = (ROOT / "scripts" / "release.py").read_text(encoding="utf-8")
    assert "TOOLBOX_VERSION" in release_script
    assert 'f"toolbox-{toolbox_version}.tar"' in release_script
    cache_module = (ROOT / "src/cognita/runtime_broker/image_cache.py").read_text(encoding="utf-8")
    assert f'TOOLBOX_TAG = "{toolbox_tag}"' in cache_module
    assert f'TOOLBOX_VERSION = "{toolbox_version}"' in cache_module
    assert f'ARCHIVE_NAME = "{archive_name}"' in cache_module
    # (Superseded: `sdk_adapter.py` was in this list while it carried a second,
    # dead copy of the adapter with its own default image literal.  13.0 cut it
    # down to a re-export of `sdk_adapter_v2`, so it names no image at all.)
    for name in ("__main__.py", "probe.py", "sdk_adapter_v2.py"):
        broker_source = (ROOT / "src/cognita/runtime_broker" / name).read_text(encoding="utf-8")
        assert toolbox_tag in broker_source
    # 13.0 §4: the broker reports the authority's value rather than a literal
    # of its own, which is why that leaf module also ships in the runtime image.
    broker_app = (ROOT / "src/cognita/runtime_broker/app.py").read_text(encoding="utf-8")
    assert "from ..release_identity import APPLICATION_VERSION" in broker_app
    assert "version=APPLICATION_VERSION," in broker_app
    runtime_dockerfile = (ROOT / "containers/workspace-runtime/Dockerfile").read_text(encoding="utf-8")
    assert "COPY src/cognita/release_identity.py" in runtime_dockerfile
    # 13.0 §6.4: the unit is rendered per target from one template and carries
    # no version literal at all.  The old per-version unit files under
    # systemd/ named a release in their Description, so every bump had to edit
    # three files that nothing verified were installed.
    template = (ROOT / "scripts/systemd/cognita-compose.service.template").read_text(encoding="utf-8")
    assert not re.search(r"\b\d+\.\d+\.\d+\b", template)
    assert "up -d --no-build --pull never --wait" in template


def test_compose_exposes_only_gateway_and_keeps_runtime_private() -> None:
    doc = _compose()
    services = doc["services"]
    workspace = yaml.safe_load((ROOT / "compose.workspace.yaml").read_text(encoding="utf-8"))
    runtime = workspace["services"]["workspace-runtime"]
    assert set(services) == {"cognita", "postgres"}
    assert set(workspace["services"]) == {"cognita", "workspace-runtime"}
    assert "@sha256:" in services["postgres"]["image"]
    assert not services["postgres"].get("ports")
    # 13.0 §8: the migration restore mount and the healthcheck clause that
    # waited for it are gone together.  The index is derived and rebuildable,
    # so a fresh cluster initializes empty and Cognita reindexes; keeping
    # either half would leave Compose waiting on a marker no code writes.
    assert not any(
        item.get("target") == "/docker-entrypoint-initdb.d"
        for item in services["postgres"]["volumes"]
    )
    postgres_health = services["postgres"]["healthcheck"]["test"]
    assert not any(".cognita-restore-complete" in str(part) for part in postgres_health)
    assert not any("docker-entrypoint-initdb.d" in str(part) for part in postgres_health)
    # 13.0 §4.1: the app names its own deployment in the schema-mismatch
    # message, and that name comes from the target's env file.
    assert services["cognita"]["environment"]["COGNITA_RELEASE_TARGET"] == "${COGNITA_RELEASE_TARGET:-}"
    # 13.0 §7.1: containers/cognita/Dockerfile's last stage is the test stage,
    # so the service image has to be selected by name or a plain build ships
    # the tests.
    assert services["cognita"]["build"]["target"] == "app"
    assert (
        services["postgres"]["user"] == "${COGNITA_SERVICE_UID:-1000}:${COGNITA_SERVICE_GID:-1000}"
    )
    assert (
        services["cognita"]["user"] == "${COGNITA_SERVICE_UID:-1000}:${COGNITA_SERVICE_GID:-1000}"
    )
    assert not runtime.get("ports")
    assert (
        runtime["user"]
        == "${COGNITA_SERVICE_UID:-1000}:${COGNITA_SERVICE_GID:-1000}"
    )
    assert runtime["group_add"] == [
        "${COGNITA_KVM_GID:?COGNITA_KVM_GID is required}"
    ]
    assert runtime["devices"] == ["/dev/kvm:/dev/kvm"]
    assert "@sha256:" in runtime["build"]["args"]["MICROSANDBOX_IMAGE"]
    assert not any("/dev/kvm" in str(item) for item in services["cognita"].get("devices", []))
    assert runtime["cap_drop"] == ["ALL"]
    assert not runtime.get("cap_add")
    assert doc["networks"]["cognita-internal"]["internal"] is True
    config_mount = next(
        item for item in services["cognita"]["volumes"] if item["target"] == "/app/config"
    )
    assert config_mount.get("read_only") is not True
    assert runtime["environment"]["COGNITA_INTERNAL_BEARER_FILE"].startswith(
        "/run/secrets/"
    )
    assert runtime["environment"]["HOME"] == "/root"
    assert runtime["environment"]["MSB_DATA_DIR"] == "/root/.microsandbox"
    cache_environment = services["cognita"]["environment"]
    assert {name: cache_environment[name] for name in CACHE_ENVIRONMENT} == CACHE_ENVIRONMENT
    tls_secrets = services["cognita"]["secrets"][-2:]
    assert [item["source"] for item in tls_secrets] == [
        "admin_tls_certfile",
        "admin_tls_keyfile",
    ]
    assert all(item["target"] == item["source"] for item in tls_secrets)
    assert all(item["mode"] in (292, "0444") for item in tls_secrets)
    assert doc["secrets"]["admin_tls_certfile"]["file"].endswith("/admin_tls_certfile")
    assert doc["secrets"]["admin_tls_keyfile"]["file"].endswith("/admin_tls_keyfile")
    # (Superseded: this used to require `preflight-host.sh` to check both TLS
    # secret files before the stack started.  13.0 §8 deleted that script; the
    # Compose declarations above are what make the files required, and a
    # missing one fails the unit's `up --wait` with the path in the error.)
    broker_probe = runtime["healthcheck"]["test"][1]
    assert "test -w /root/.microsandbox" in broker_probe
    # 13.0: the probe reads the broker's own readiness field rather than
    # settling for a 200, so a runtime that never came up fails the
    # healthcheck instead of being met later as a failed Workspace call.
    assert '"runtime"' in broker_probe and '"ready"' in broker_probe
    assert "/healthz" in broker_probe
    compile(broker_probe.split("python3 -c ")[1].strip("'"), "<broker-probe>", "exec")


def test_postgres_healthcheck_targets_the_pinned_image_socket() -> None:
    """Readiness must probe the socket directory used by the pinned image.

    ``PGHOST`` is a libpq client override, not a PostgreSQL server setting.  A
    value such as ``/tmp`` can therefore make ``pg_isready`` probe a different
    directory from the server's configured Unix socket and leave Compose
    waiting forever even though PostgreSQL is accepting connections.
    """
    postgres = _compose()["services"]["postgres"]
    assert "PGHOST" not in postgres.get("environment", {})
    healthcheck = postgres["healthcheck"]
    # 13.0 §8 removed the restore gate this expectation used to carry: a
    # cluster answering pg_isready on the pinned image's own socket is ready,
    # and there is no migration dump left to wait for.
    assert healthcheck["test"] == [
        "CMD-SHELL",
        "pg_isready -h /var/run/postgresql -U $${POSTGRES_USER} -d $${POSTGRES_DB}",
    ]


def test_runtime_image_starts_private_broker_and_pins_its_base() -> None:
    dockerfile = (ROOT / "containers/workspace-runtime/Dockerfile").read_text(encoding="utf-8")
    assert "MICROSANDBOX_IMAGE=ghcr.io/superradcompany/microsandbox:0.7.0@sha256:" in dockerfile
    assert 'CMD ["python3", "-m", "cognita.runtime_broker"]' in dockerfile
    assert 'CMD ["sleep", "infinity"]' not in dockerfile
    assert "COPY src ./src" not in dockerfile
    assert "chmod 0711 /root" in dockerfile


def test_runtime_wheel_is_verified_before_local_offline_install() -> None:
    dockerfile = (ROOT / "containers/workspace-runtime/Dockerfile").read_text(encoding="utf-8")
    lock = tomllib.loads(
        (ROOT / "containers/workspace-runtime/microsandbox.lock").read_text(encoding="utf-8")
    )
    wheel_path = f"/opt/build-artifacts/{lock['filename']}"
    verification = f"echo '{lock['sha256']}  {wheel_path}' | sha256sum -c -"
    install = re.search(
        rf"python3 -m pip install --break-system-packages --no-index --no-cache-dir\s+"
        rf"\\?\s*{re.escape(wheel_path)}",
        dockerfile,
    )
    assert verification in dockerfile
    assert install is not None
    assert dockerfile.index(verification) < install.start()
    # --require-hashes accepts hashes only through a requirements entry; when
    # followed by a bare wheel path it rejects even independently verified bytes.
    assert not re.search(
        rf"--require-hashes\s+{re.escape(wheel_path)}",
        dockerfile,
    )


def test_no_privilege_or_docker_socket_in_build_and_stack_files() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "privileged:" not in compose
    assert "docker.sock" not in compose
    for dockerfile in (ROOT / "containers").rglob("Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        assert "docker.sock" not in text
        assert "--privileged" not in text


def test_no_image_copies_a_config_directory_that_could_hold_credentials() -> None:
    """A build context is whatever checkout someone is standing in.

    `COPY config …` put a live installation's `config/` into the test image and
    into the build cache: credentials-v2.json, master-keys/, the OAuth SQLite
    database, backups and the admin TLS material.  The suite reads one file
    from there, so the Dockerfiles name files, and `.dockerignore` excludes the
    sensitive paths as well -- two independent fail-closed halves, because
    either one alone is one edit away from leaking again.
    """
    for name in ("containers/cognita/Dockerfile", "containers/cognita-amd/Dockerfile",
                 "containers/workspace-runtime/Dockerfile", "containers/workspace-toolbox/Dockerfile"):
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            if not line.startswith("COPY "):
                continue
            sources = line.removeprefix("COPY ").split()
            # Flags, the destination and any line continuation are not sources.
            sources = [s for s in sources if not s.startswith("--") and s != "\\"][:-1]
            assert "config" not in sources, f"{name}: {line}"
            assert "certs" not in sources, f"{name}: {line}"
            for source in sources:
                assert not source.startswith("config/data"), f"{name}: {line}"

    ignored = {line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    for path in ("config/*.yaml", "config/*.json", "config/*.token", "config/*.secret",
                 "config/data", "config/master-keys", "config/backups", "certs"):
        assert path in ignored, f".dockerignore does not exclude {path}"


def test_owner_only_env_template_names_every_required_root() -> None:
    # (Superseded: this also asserted that three preflight-*.sh scripts
    # existed.  13.0 §8 deleted them; `release.py doctor` owns the device and
    # daemon probes, and `release.py` reads every root below out of this same
    # env file.)
    template = (ROOT / "config/cognita-compose.env.example").read_text(encoding="utf-8")
    for variable in (
        "COGNITA_CONFIG_ROOT",
        "COGNITA_PROJECTS_ROOT",
        "COGNITA_POSTGRES_DATA_ROOT",
        "COGNITA_WORKSPACE_DATA_ROOT",
        "COGNITA_TRANSFER_STAGING_ROOT",
        "COGNITA_SECRETS_ROOT",
        "COGNITA_MODEL_CACHE_ROOT",
        "COGNITA_KVM_GID",
    ):
        assert f"{variable}=" in template
    # 13.0 §4.1 and §6.1: the target name reaches the container through this
    # file, and the unit's Compose invocation cannot interpolate the image
    # names without the version.  `release.py doctor` fails on a target whose
    # env file lacks COGNITA_VERSION, so the template has to show both.
    assert "COGNITA_RELEASE_TARGET=" in template
    assert "COGNITA_VERSION=" in template


# 13.0 section 8: the three preflight tests that stood here are gone with
# `preflight-compose.sh`, `preflight-host.sh` and `preflight-kvm.sh`.  They
# checked that those scripts handed Compose only the env-file PATH (never a
# sourced secret), that the KVM group id in the env file matched /dev/kvm,
# and that an unsafe model-cache path failed before the stack started.  The
# real device and daemon probes now live in `release.py doctor`; the secret
# handling they protected went with the scripts that had the secrets.


def test_config_loads_mounted_yaml_and_secret_dsn(monkeypatch, tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    config_root.joinpath("cognita.yaml").write_text("engine: core\n", encoding="utf-8")
    dsn = tmp_path / "postgres.dsn"
    dsn.write_text("postgresql://cognita:secret@postgres:5432/cognita\n", encoding="ascii")
    monkeypatch.setenv("COGNITA_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("COGNITA_PG_DSN_FILE", str(dsn))
    assert load_config().pg_dsn == "postgresql://cognita:secret@postgres:5432/cognita"
