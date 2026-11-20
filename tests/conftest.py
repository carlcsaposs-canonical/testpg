import argparse
import dataclasses
import enum
import getpass
import pathlib
import re
import shutil
import subprocess
import time
import typing

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--image",
        required=True,
        help='Skopeo image name (e.g. "oci-archive:postgresql_18.7_amd64.rock", '
        '"docker://rocks.pkg.store/ubuntu/postgresql:18_edge"): '
        "https://github.com/podman-container-tools/skopeo/blob/main/docs/skopeo.1.md#image-names",
    )
    parser.addoption(
        "--cleanup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Clean up after test execution. Disabling this can break host environment and other "
        "tests. For debugging failed tests. When disabling this, it is recommended to select a "
        "single test item (e.g. with `-k`).",
    )


@pytest.fixture(scope="session")
def clean_up(request) -> typing.Generator[bool, None, None]:
    return request.config.getoption("cleanup")


@pytest.fixture(scope="session")
def temporary_session_directory(clean_up):
    tests_path = pathlib.Path(__file__).parent
    if not (tests_path.exists() and tests_path.name == "tests"):
        raise FileNotFoundError
    path = tests_path / "tmp"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()

    yield path.resolve(strict=True)

    if not clean_up:
        return
    shutil.rmtree(path)


@pytest.fixture(scope="function")
def workspace(temporary_session_directory, clean_up):
    path = temporary_session_directory / "workspace"
    path.mkdir()

    yield path.resolve(strict=True)

    if not clean_up:
        return
    shutil.rmtree(path)


@pytest.fixture(scope="session", autouse=True)
def packages():
    packages = ("skopeo", "postgresql-client", "postgresql-server-dev-18")
    subprocess.run(("sudo", "apt-get", "install", "--yes", *packages), check=True)


@dataclasses.dataclass(frozen=True)
class _ContainerEngine_:
    deb_package: str
    command_prefix: str
    sudo_required: bool
    transport: str

    @property
    def sudo_prefix(self) -> tuple[str, ...]:
        if self.sudo_required:
            return ("sudo",)
        else:
            return ()


# https://docs.python.org/3/howto/enum.html#enum-dataclass-support
class ContainerEngine(_ContainerEngine_, enum.Enum):
    DOCKER = ("docker.io", "docker", True, "docker-daemon")
    PODMAN = ("podman", "podman", False, "containers-storage")


# Magic constant, to differentiate lines commented by this code from user-commented lines
_ID_COMMENT_PREFIX = "#ROCK_TEST_ROOTLESS_PODMAN "


@pytest.fixture(scope="session", params=(ContainerEngine.DOCKER, ContainerEngine.PODMAN))
def container_engine(request, clean_up) -> typing.Generator[ContainerEngine, None, None]:
    engine: ContainerEngine = request.param
    user = getpass.getuser()
    id_paths = (pathlib.Path("/etc/subuid"), pathlib.Path("/etc/subgid"))
    if engine is ContainerEngine.PODMAN:
        # https://www.redhat.com/en/blog/rootless-podman
        # (rock uses UID/GID 584792: https://ubuntu.com/containers/rockcraft/docs/latest/reference/rockcraft-yaml/#run-user)
        for path in id_paths:
            lines = path.read_text().splitlines()
            for line in lines:
                if line.startswith(f"{_ID_COMMENT_PREFIX}{user}:"):
                    # Leftover from previous run that was not cleaned up; no action needed
                    break
            else:
                lines = [
                    f"{_ID_COMMENT_PREFIX}{line}" if line.startswith(f"{user}:") else line
                    for line in lines
                ]
                lines.append(f"{user}:100000:1000000")
                text = "\n".join((*lines, ""))
                subprocess.run(
                    ("sudo", "tee", path),
                    input=text,
                    stdout=subprocess.DEVNULL,
                    check=True,
                    encoding="utf-8",
                )
    subprocess.run(("sudo", "apt-get", "install", "--yes", engine.deb_package), check=True)

    yield engine

    if not clean_up:
        return
    if engine is ContainerEngine.PODMAN:
        subprocess.run(("podman", "system", "reset", "--force"), check=True)
        for path in id_paths:
            lines = path.read_text().splitlines()
            lines = (line for line in lines if line != f"{user}:100000:1000000")
            lines = (
                line.removeprefix(_ID_COMMENT_PREFIX)
                if line.startswith(f"{_ID_COMMENT_PREFIX}{user}:")
                else line
                for line in lines
            )
            text = "\n".join((*lines, ""))
            subprocess.run(
                ("sudo", "tee", path),
                input=text,
                stdout=subprocess.DEVNULL,
                check=True,
                encoding="utf-8",
            )
    subprocess.run(("sudo", "apt-get", "purge", "--yes", engine.deb_package), check=True)
    subprocess.run(("sudo", "apt-get", "autopurge", "--yes"), check=True)
    if engine is ContainerEngine.DOCKER:
        subprocess.run(("sudo", "rm", "--recursive", "--force", "/var/lib/containerd"), check=True)


@pytest.fixture(scope="session")
def image(request, container_engine):
    reference = "image-under-test:latest"
    subprocess.run(
        (
            *container_engine.sudo_prefix,
            "skopeo",
            "copy",
            request.config.getoption("image"),
            f"{container_engine.transport}:{reference}",
        ),
        check=True,
    )

    yield reference


@pytest.fixture(scope="session")
def postgresql_version(image, container_engine):
    # Example: "postgres (PostgreSQL) 18.4 (Ubuntu 18.4-0ubuntu0.26.04.1)"
    output = subprocess.run(
        (
            *container_engine.sudo_prefix,
            container_engine.command_prefix,
            "run",
            "--rm",
            "--quiet",
            "--entrypoint",
            "postgres",
            image,
            "--version",
        ),
        capture_output=True,
        check=True,
        encoding="utf-8",
    ).stdout.strip()
    match = re.match(r"postgres \(PostgreSQL\) (?P<version>[^ ]+)", output)
    if not match:
        raise ValueError
    # Example: "18.4"
    version: str = match.group("version")
    return version


@dataclasses.dataclass(frozen=True, kw_only=True)
class _Container:
    password: str


def wait_until_postgresql_ready(*, container_engine: ContainerEngine, timeout=10):
    start = time.monotonic()
    while True:
        try:
            subprocess.run(
                (
                    *container_engine.sudo_prefix,
                    container_engine.command_prefix,
                    "exec",
                    "mypostgres",
                    "pg_isready",
                ),
                check=True,
            )
        except subprocess.CalledProcessError as exception:
            if exception.returncode == 3:
                # https://www.postgresql.org/docs/current/app-pg-isready.html
                raise
            elif time.monotonic() - start < timeout:
                time.sleep(0.1)
            else:
                raise
        else:
            break


@pytest.fixture
def container(container_engine, image, workspace, clean_up):
    # Match README.md commands
    if container_engine is ContainerEngine.DOCKER:
        subprocess.run(
            "tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24 | tee postgres_password.txt",
            shell=True,
            cwd=workspace,
            check=True,
        )
        subprocess.run(
            ("sudo", "chown", "584792:584792", "postgres_password.txt"), cwd=workspace, check=True
        )
        subprocess.run(("sudo", "chmod", "400", "postgres_password.txt"), cwd=workspace, check=True)
        secret_args = (
            "--mount",
            f"type=bind,source={workspace}/postgres_password.txt,destination=/run/secrets/postgres_password,readonly",
        )
        password = subprocess.run(
            ("sudo", "cat", workspace / "postgres_password.txt"),
            capture_output=True,
            check=True,
            encoding="utf-8",
        ).stdout
    elif container_engine is ContainerEngine.PODMAN:
        assert container_engine.sudo_required is False
        subprocess.run(
            f"tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24 | {container_engine.command_prefix} secret create postgres_password -",
            shell=True,
            check=True,
        )
        secret_args = ("--secret", "postgres_password,uid=584792,gid=584792,mode=400")
        password = subprocess.run(
            (
                *container_engine.sudo_prefix,
                container_engine.command_prefix,
                "secret",
                "inspect",
                "postgres_password",
                "--showsecret",
                "--format",
                "{{.SecretData}}",
            ),
            capture_output=True,
            check=True,
            encoding="utf-8",
        ).stdout.strip()
    else:
        raise TypeError
    subprocess.run(
        (
            *container_engine.sudo_prefix,
            container_engine.command_prefix,
            "run",
            "-d",
            "--name",
            "mypostgres",
            "-p",
            "5432:5432",
            "--volume",
            "mypgdata:/var/lib/postgresql/",
            *secret_args,
            image,
        ),
        check=True,
    )
    wait_until_postgresql_ready(container_engine=container_engine)

    yield _Container(password=password)

    if not clean_up:
        return
    if container_engine is ContainerEngine.DOCKER:
        subprocess.run(("sudo", "rm", workspace / "postgres_password.txt"), check=True)
    subprocess.run(
        (
            *container_engine.sudo_prefix,
            container_engine.command_prefix,
            "container",
            "stop",
            "mypostgres",
        ),
        check=True,
    )
    subprocess.run(
        (
            *container_engine.sudo_prefix,
            container_engine.command_prefix,
            "container",
            "rm",
            "mypostgres",
        ),
        check=True,
    )
    subprocess.run(
        (
            *container_engine.sudo_prefix,
            container_engine.command_prefix,
            "volume",
            "rm",
            "mypgdata",
        ),
        check=True,
    )
    if container_engine is ContainerEngine.PODMAN:
        subprocess.run(
            (
                *container_engine.sudo_prefix,
                container_engine.command_prefix,
                "secret",
                "rm",
                "postgres_password",
            ),
            check=True,
        )
