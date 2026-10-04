import asyncio
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def test_toolbox_cache_load_uses_pinned_local_sdk_import() -> None:
    """The release script stages and imports the Toolbox the same way.

    13.0 §8: `scripts/bootstrap-toolbox-cache.sh` is deleted.  It did exactly
    what `release.py`'s `build_toolbox`/`load_toolbox` now do -- `docker save`
    into the target's toolbox-cache root, then the broker-owned
    `image_cache materialize-binding`/`load` inside the pinned
    workspace-runtime image -- and carried its own `12.6.0` literal, a second
    place a stale Toolbox version could hide.
    """
    script = (ROOT / "scripts/release.py").read_text(encoding="utf-8")
    module = (ROOT / "src/cognita/runtime_broker/image_cache.py").read_text(encoding="utf-8")
    assert '"docker", "save"' in script
    # Compose 5.3 requires --pull to stay among the run options before the
    # service name. Keep the executable pinned to the broker image's local
    # SDK loader; this operation must not acquire the Docker socket.
    assert '"run", "--pull", "never", "--rm", "--no-deps", "workspace-runtime"' in script
    assert '"python3", "-m",' in script
    assert '"cognita.runtime_broker.image_cache"' in script
    assert "image_cache" in script
    assert '"load"' in script
    assert "/var/run/docker.sock:" not in script
    assert "Image.load" in module
    assert "toolbox-12.6.0.tar" in module
    assert "Image.inspect" in module
    assert "manifest.json" in module
    assert "archive_digest" in module
    assert "Docker's image ``Id``" in module
    assert "materialize-binding" in script
    assert "PullPolicy" not in module


def test_toolbox_cache_identity_is_fail_closed() -> None:
    module = (ROOT / "src/cognita/runtime_broker/image_cache.py").read_text(encoding="utf-8")
    assert "org.opencontainers.image.title" in module
    assert "org.opencontainers.image.version" in module
    assert 'if not await _inspect(tag, binding.config_digest):' in module
    assert 'raise SystemExit("toolbox cache: imported image failed identity verification")' in module


# 13.0 §8: `test_systemd_and_kvm_preflight_require_explicit_bootstrap` is gone
# with its three subjects.  The per-profile unit files and preflight-kvm.sh are
# deleted, and the unit no longer bootstraps anything: it runs
# `docker compose ... up` against `<releases>/<target>/current/` and nothing
# else (§6.4).  `release.py` loads the Toolbox after the unit starts, which the
# test above covers.


def _load_module(monkeypatch, image):
    monkeypatch.setitem(sys.modules, "microsandbox", SimpleNamespace(Image=image))
    spec = importlib.util.spec_from_file_location(
        "cognita_toolbox_cache_test", ROOT / "src/cognita/runtime_broker/image_cache.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _detail(digest: str):
    return SimpleNamespace(
        handle=SimpleNamespace(reference="cognita-workspace-toolbox:12.6.0"),
        config=SimpleNamespace(
            digest=digest,
            labels={
                "org.opencontainers.image.title": "Cognita Workspace toolbox",
                "org.opencontainers.image.description": "Credential-free CPU toolbox for isolated Workspaces",
                "org.opencontainers.image.version": "12.6.0",
            },
            user="workspace", working_dir="/workspace", cmd=["bash"],
        ),
    )


def _write_archive(root: Path, tag: str, duplicate: str | None = None) -> Path:
    config = b'{"architecture":"amd64","os":"linux"}'
    config_digest = hashlib.sha256(config).hexdigest()
    manifest = [{
        "Config": f"blobs/sha256/{config_digest}",
        "RepoTags": [tag],
        "Layers": [],
    }]
    archive = root / "toolbox-12.6.0.tar"
    with tarfile.open(archive, "w") as saved:
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        saved.addfile(manifest_info, io.BytesIO(manifest_bytes))
        config_info = tarfile.TarInfo(f"blobs/sha256/{config_digest}")
        config_info.size = len(config)
        saved.addfile(config_info, io.BytesIO(config))
        if duplicate == "manifest":
            saved.addfile(manifest_info, io.BytesIO(manifest_bytes))
        elif duplicate == "config":
            saved.addfile(config_info, io.BytesIO(config))
    return archive


def test_archive_binding_uses_docker_save_config_blob_digest(monkeypatch):
    module = _load_module(monkeypatch, SimpleNamespace())
    with tempfile.TemporaryDirectory(prefix="cognita-toolbox-review-") as root:
        archive_root = Path(root)
        module.ARCHIVE_ROOT = archive_root
        archive = _write_archive(archive_root, module.TOOLBOX_TAG)
        binding = module._archive_binding(archive, module.TOOLBOX_TAG)
    assert binding.config_digest.startswith("sha256:")
    assert binding.archive_digest.startswith("sha256:")
    assert binding.config_digest != "sha256:" + "a" * 64


def test_archive_binding_rejects_tampered_config(monkeypatch):
    module = _load_module(monkeypatch, SimpleNamespace())
    with tempfile.TemporaryDirectory(prefix="cognita-toolbox-review-") as root:
        archive_root = Path(root)
        module.ARCHIVE_ROOT = archive_root
        archive = _write_archive(archive_root, module.TOOLBOX_TAG)
        module._write_binding(archive, module.TOOLBOX_TAG)
        with archive.open("ab") as stream:
            stream.write(b"tampered")
        try:
            module._verified_binding(archive, module.TOOLBOX_TAG)
        except SystemExit as exc:
            assert "release archive binding" in str(exc)
        else:
            raise AssertionError("tampered archive unexpectedly accepted")


def test_archive_binding_rejects_duplicate_identity_entries(monkeypatch):
    module = _load_module(monkeypatch, SimpleNamespace())
    with tempfile.TemporaryDirectory(prefix="cognita-toolbox-review-") as root:
        archive_root = Path(root)
        module.ARCHIVE_ROOT = archive_root
        for duplicate in ("manifest", "config"):
            archive = _write_archive(archive_root, module.TOOLBOX_TAG, duplicate)
            try:
                module._archive_binding(archive, module.TOOLBOX_TAG)
            except SystemExit as exc:
                assert "duplicate paths" in str(exc)
            else:
                raise AssertionError(f"duplicate {duplicate} unexpectedly accepted")


def test_binding_write_does_not_follow_existing_fixed_temp_path(monkeypatch):
    module = _load_module(monkeypatch, SimpleNamespace())
    with tempfile.TemporaryDirectory(prefix="cognita-toolbox-review-") as root:
        archive_root = Path(root)
        module.ARCHIVE_ROOT = archive_root
        archive = _write_archive(archive_root, module.TOOLBOX_TAG)
        old_temp = archive_root / ".toolbox-12.6.0.binding.json.tmp"
        old_temp.write_text("preserve", encoding="utf-8")
        module._write_binding(archive, module.TOOLBOX_TAG)
        assert old_temp.read_text(encoding="utf-8") == "preserve"
        assert module._verified_binding(archive, module.TOOLBOX_TAG).config_digest.startswith("sha256:")


def test_cached_label_match_does_not_accept_different_docker_image(monkeypatch):
    expected = "sha256:" + "a" * 64
    other = "sha256:" + "b" * 64

    class FakeImage:
        @staticmethod
        async def inspect(_tag):
            return _detail(other)

    module = _load_module(monkeypatch, FakeImage)
    assert not asyncio.run(module._inspect(module.TOOLBOX_TAG, expected))
    assert asyncio.run(module._inspect(module.TOOLBOX_TAG, other))


def test_matching_cache_is_not_removed_or_reimported(monkeypatch):
    expected = "sha256:" + "a" * 64
    calls = []
    current_digest = expected

    class FakeImage:
        @staticmethod
        async def inspect(_tag):
            return _detail(current_digest)

        @staticmethod
        async def get(_tag):
            calls.append("get")

        @staticmethod
        async def load(_path, *, tag):
            calls.append(("load", tag))

    module = _load_module(monkeypatch, FakeImage)
    with tempfile.TemporaryDirectory(prefix="cognita-toolbox-review-") as root:
        archive_root = Path(root)
        module.ARCHIVE_ROOT = archive_root
        archive = _write_archive(archive_root, module.TOOLBOX_TAG)
        binding = module._archive_binding(archive, module.TOOLBOX_TAG)
        current_digest = binding.config_digest
        module._write_binding(archive, module.TOOLBOX_TAG)
        asyncio.run(module.load(archive, binding.config_digest))
    assert not archive_root.exists()
    assert calls == []


def test_changed_cache_is_reimported_without_forcing_active_users(monkeypatch):
    other = "sha256:" + "b" * 64
    calls = []
    current_digest = other
    expected_digest = None

    class Existing:
        async def remove(self, *, force=False):
            calls.append(("remove", force))

    class FakeImage:
        @staticmethod
        async def inspect(_tag):
            return _detail(current_digest)

        @staticmethod
        async def get(_tag):
            return Existing()

        @staticmethod
        async def load(_path, *, tag):
            nonlocal current_digest
            calls.append(("load", tag))
            current_digest = expected_digest

    module = _load_module(monkeypatch, FakeImage)
    with tempfile.TemporaryDirectory(prefix="cognita-toolbox-review-") as root:
        archive_root = Path(root)
        module.ARCHIVE_ROOT = archive_root
        archive = _write_archive(archive_root, module.TOOLBOX_TAG)
        binding = module._archive_binding(archive, module.TOOLBOX_TAG)
        expected_digest = binding.config_digest
        module._write_binding(archive, module.TOOLBOX_TAG)
        asyncio.run(module.load(archive, binding.config_digest))
    assert not archive_root.exists()
    assert calls == [("remove", False), ("load", module.TOOLBOX_TAG)]
