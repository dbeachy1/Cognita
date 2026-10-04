"""Static checks on the Windows installer's Linux image recipe (containers/wsl/).

DESIGN-WINDOWS-INSTALLER §6 and §14.2 ("Recipe"). Pure file reading: no Docker, no network,
no shell. The image itself is proven by building it (the proof run in the design's §14.3),
not here; these tests keep the recipe's promises from being edited away unnoticed.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

WSL_DIR = Path(__file__).resolve().parent.parent / "containers" / "wsl"


def _text(name: str) -> str:
    return (WSL_DIR / name).read_text(encoding="utf-8")


def _code_lines(name: str) -> list[str]:
    """The script's lines with full-line comments dropped, so a comment cannot satisfy or
    trip a check meant for code."""
    return [
        line for line in _text(name).splitlines() if not line.lstrip().startswith("#")
    ]


def test_recipe_files_exist_and_use_lf() -> None:
    for name in (
        "build-wsl-image.sh",
        "wsl.conf",
        "cognita-keepalive",
        "51cognita-docker",
    ):
        data = (WSL_DIR / name).read_bytes()
        assert data, name
        assert b"\r" not in data, f"{name} must use LF line endings"


def test_base_rootfs_is_pinned_by_url_and_sha256() -> None:
    text = _text("build-wsl-image.sh")
    url = re.search(r'^BASE_URL="(https://[^"]+)"$', text, re.MULTILINE)
    sha = re.search(r'^BASE_SHA256="([0-9a-f]{64})"$', text, re.MULTILINE)
    assert url, "BASE_URL must be pinned"
    assert sha, "BASE_SHA256 must be a 64-hex SHA-256"
    assert "ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz" in url.group(1)
    # The download is verified against the pin, and a mismatch stops the build.
    assert '[ "$ACTUAL_SHA" != "$BASE_SHA256" ]' in text
    assert "mismatch" in text


def test_wsl_conf_has_exactly_the_design_keys() -> None:
    parser = configparser.ConfigParser()
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(_text("wsl.conf"))
    actual = {section: dict(parser[section]) for section in parser.sections()}
    assert actual == {
        "boot": {"systemd": "true"},
        "user": {"default": "cognita"},
        "automount": {"enabled": "true", "mountFsTab": "true"},
        "interop": {"appendWindowsPath": "false"},
        "network": {"generateResolvConf": "true"},
    }


def test_apt_origin_file_is_the_single_docker_line() -> None:
    lines = [ln for ln in _text("51cognita-docker").splitlines() if ln.strip()]
    assert lines == [
        'Unattended-Upgrade::Origins-Pattern { "origin=Docker,label=Docker CE"; };'
    ]


def test_keepalive_only_sleeps() -> None:
    code = [
        ln
        for ln in _text("cognita-keepalive").splitlines()
        if ln.strip() and (ln.startswith("#!") or not ln.lstrip().startswith("#"))
    ]
    assert code == ["#!/bin/sh", "exec sleep infinity"]


def test_script_never_pulls_cognita_images() -> None:
    code = "\n".join(_code_lines("build-wsl-image.sh"))
    assert not re.search(r"\bdocker\s+(image\s+)?pull\b", code)
    assert not re.search(r"\bdocker\s+run\b", code)  # the build uses create/cp/start
    assert "ghcr.io" not in code
    assert "compose pull" not in code


def test_apt_caches_lists_and_logs_are_removed() -> None:
    code = "\n".join(_code_lines("build-wsl-image.sh"))
    assert "apt-get clean" in code
    assert "rm -rf /var/lib/apt/lists/* /var/log/apt/* /var/cache/apt/*" in code


def test_resolv_conf_is_removed_so_wsl_generates_its_own() -> None:
    code = "\n".join(_code_lines("build-wsl-image.sh"))
    assert "/etc/resolv.conf" in code
    # Docker bind-mounts it, so the build container unmounts it before removing it, and the
    # export is checked for it afterwards.
    assert "umount /etc/resolv.conf" in code
    assert "rm -f /etc/resolv.conf" in code
    assert "--cap-add SYS_ADMIN" in code
    assert "for member in .dockerenv etc/resolv.conf" in code
    # `tar --delete` leaves an archive Windows bsdtar calls damaged; never edit the export.
    assert "tar --delete" not in code


def test_no_sudoers_change() -> None:
    code = "\n".join(_code_lines("build-wsl-image.sh"))
    for forbidden in ("sudoers", "visudo", "NOPASSWD", "usermod -aG sudo", "adduser cognita sudo"):
        assert forbidden not in code, forbidden
    assert " sudo " not in f" {code} "


def test_cognita_user_is_uid_1000_in_the_docker_group_with_linger() -> None:
    code = "\n".join(_code_lines("build-wsl-image.sh"))
    assert "useradd -m -u 1000 -s /bin/bash cognita" in code
    assert "usermod -aG docker cognita" in code
    assert "touch /var/lib/systemd/linger/cognita" in code
    assert "systemctl enable docker" in code


def test_tree_stamp_format_and_layout() -> None:
    text = _text("build-wsl-image.sh")
    # The stamp the Linux CLI compares with published-release.txt (Linux design §19.9).
    assert "printf 'commit: %s\\n' \"$FULL_COMMIT\" > \"$INPUTS/tree/.cognita-tree\"" in text
    assert 'TREE_NAME="$REL_VERSION-$COMMIT12"' in text
    assert "/opt/cognita/trees/$TREE_NAME" in text
    assert "ln -s /opt/cognita/src/cognita /usr/local/bin/cognita" in text
    assert 'cp "$RELEASE_FILE" "$INPUTS/tree/containers/published-release.txt"' in text
    # The published commit must be the one archived, and the version comes from the file.
    assert 'git -C "$REPO" archive --format=tar "$FULL_COMMIT"' in text
    assert "s/^version:" in text


def test_outputs_and_stdout_contract() -> None:
    text = _text("build-wsl-image.sh")
    assert 'WSL_NAME="cognita-wsl-$REL_VERSION.tar.gz"' in text
    assert 'SRC_NAME="cognita-src-$REL_VERSION.tar.gz"' in text
    assert "printf 'file=%s sha256=%s bytes=%s\\n'" in text
    assert ".sha256" in text


def test_trap_cleans_temporary_docker_objects_and_work_dir() -> None:
    text = _text("build-wsl-image.sh")
    assert "trap cleanup EXIT" in text
    match = re.search(r"^cleanup\(\) \{$(.*?)^\}$", text, re.MULTILINE | re.DOTALL)
    assert match, "cleanup function missing"
    body = match.group(1)
    assert 'docker rm -f "$BUILD_CTR"' in body
    assert 'docker image rm -f "$BASE_IMG"' in body
    assert 'rm -rf "$WORK"' in body
    # Signals go through the EXIT trap too.
    assert "trap 'exit 130' INT" in text
    assert "trap 'exit 143' TERM" in text


def test_script_uses_strict_mode() -> None:
    assert "\nset -eu\n" in _text("build-wsl-image.sh")


# --- 15.1.0: the NVIDIA Container Toolkit (DESIGN-WINDOWS-INSTALLER §22.5, §22.12 items 4 and 7) --------

NVIDIA_TOOLKIT_VERSION = "1.20.1-1"
NVIDIA_KEY_URL = "https://nvidia.github.io/libnvidia-container/gpgkey"
NVIDIA_LIST_URL = "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list"
NVIDIA_PACKAGES = (
    "nvidia-container-toolkit",
    "nvidia-container-toolkit-base",
    "libnvidia-container1",
    "libnvidia-container-tools",
)
WINDOWS_HELPER = Path(__file__).resolve().parent.parent / "windows" / "CognitaWin.ps1"


def _in_container() -> str:
    """The body of the single-quoted IN_CONTAINER string (the script that runs inside the build container)."""
    text = _text("build-wsl-image.sh")
    match = re.search(r"^IN_CONTAINER='\n(.*?)^'$", text, re.MULTILINE | re.DOTALL)
    assert match, "IN_CONTAINER string not found"
    return match.group(1)


def _in_container_code() -> list[str]:
    return [ln for ln in _in_container().splitlines() if not ln.lstrip().startswith("#")]


def test_image_pins_the_toolkit_and_installs_the_four_packages_at_the_pin() -> None:
    body = _in_container()
    assert f"NV_VER={NVIDIA_TOOLKIT_VERSION}\n" in body
    install = next(ln for ln in _in_container_code() if ln.startswith("apt-get install") and "nvidia" in ln)
    for package in NVIDIA_PACKAGES:
        assert f"{package}=$NV_VER" in install, package
    # Exactly four package names, each pinned: nothing else rides along unpinned.
    assert len(re.findall(r"\S+=\$NV_VER", install)) == 4


def test_image_holds_all_four_toolkit_packages_and_checks_the_holds() -> None:
    code = _in_container_code()
    hold = next(ln for ln in code if ln.startswith("apt-mark hold"))
    assert hold.split()[2:] == list(NVIDIA_PACKAGES)
    joined = "\n".join(code)
    assert "apt-mark showhold" in joined
    for package in NVIDIA_PACKAGES:
        assert package in joined.split("for pkg in", 1)[1].split("; do", 1)[0], package


def test_image_registers_the_docker_runtime_and_never_generates_a_cdi_spec() -> None:
    text = _text("build-wsl-image.sh")
    assert "nvidia-ctk runtime configure --runtime=docker" in "\n".join(_in_container_code())
    assert "cdi generate" not in text            # not even in a comment
    assert "nvidia-ctk cdi" not in text


def test_image_toolkit_urls_and_the_signed_by_rewrite() -> None:
    code = "\n".join(_in_container_code())
    assert f"-o /tmp/cognita-nv.gpg {NVIDIA_KEY_URL}" in code
    assert f"-o /tmp/cognita-nv.list {NVIDIA_LIST_URL}" in code
    assert "/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg" in code
    assert "> /etc/apt/sources.list.d/nvidia-container-toolkit.list" in code
    # `|` is the sed delimiter and the expression is double-quoted (the string is single-quoted).
    assert 's|deb https://|deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://|' in code


def test_image_toolkit_key_is_written_through_temp_files_with_no_curl_pipes() -> None:
    code = _in_container_code()
    assert any(ln.startswith("gpg --batch --yes --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg "
                             "/tmp/cognita-nv.gpg") for ln in code)
    assert any(ln.startswith("rm -f /tmp/cognita-nv.gpg /tmp/cognita-nv.list") for ln in code)
    for ln in code:
        if "curl" in ln:
            assert "|" not in ln, f"curl must not be piped (sh has no pipefail): {ln}"


def test_in_container_script_has_no_single_quote_so_it_stays_one_single_quoted_string() -> None:
    assert "'" not in _in_container()


def test_image_checks_the_toolkit_and_reports_its_version_on_the_done_line() -> None:
    code = "\n".join(_in_container_code())
    assert 'nvidia-ctk --version | grep -qF "${NV_VER%-*}"' in code
    assert 'grep -q "\\"nvidia\\"" /etc/docker/daemon.json' in code
    done = next(ln for ln in _in_container_code() if ln.startswith('echo "in-container: done,'))
    assert "dpkg-query -W nvidia-container-toolkit" in done


def test_image_and_windows_helper_agree_on_the_toolkit_pin_urls_and_neither_uses_cdi_generate() -> None:
    """The one pinned toolkit is written in two places (§22.5). The helper's copy is another coder's file; this
    test fails until both exist and agree."""
    image = _text("build-wsl-image.sh")
    helper = WINDOWS_HELPER.read_text(encoding="utf-8")
    assert f"$script:NvidiaToolkitVersion = '{NVIDIA_TOOLKIT_VERSION}'" in helper
    assert f"NV_VER={NVIDIA_TOOLKIT_VERSION}\n" in image
    for url in (NVIDIA_KEY_URL, NVIDIA_LIST_URL):
        assert url in image, url
        assert url in helper, url
    assert "cdi generate" not in image
    assert "cdi generate" not in helper
