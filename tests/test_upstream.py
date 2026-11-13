import os
import shutil
import subprocess

import pytest

from . import conftest


@pytest.fixture(scope="session")
def regression_test_sources(temporary_session_directory, postgresql_version, clean_up):
    path = temporary_session_directory / "postgresql_upstream_sparse"
    path.mkdir()
    subprocess.run(("git", "init"), cwd=path, check=True)
    subprocess.run(
        ("git", "sparse-checkout", "set", "--sparse-index", "src/test/regress"),
        cwd=path,
        check=True,
    )
    subprocess.run(
        ("git", "remote", "add", "--fetch", "origin", "https://github.com/postgres/postgres.git"),
        cwd=path,
        check=True,
    )
    major, minor = postgresql_version.split(".")
    subprocess.run(("git", "checkout", f"REL_{major}_{minor}"), cwd=path, check=True)

    yield path / "src/test/regress"

    if not clean_up:
        return
    shutil.rmtree(path)


def test_installcheck(container, container_engine, regression_test_sources, workspace):
    subprocess.run(
        (
            *container_engine.sudo_prefix,
            container_engine.command_prefix,
            "exec",
            "mypostgres",
            "sh",
            "-c",
            'echo "host all all all scram-sha-256" >> "$PGDATA/pg_hba.conf"',
        ),
        check=True,
    )
    subprocess.run(
        (*container_engine.sudo_prefix, container_engine.command_prefix, "restart", "mypostgres"),
        check=True,
    )
    conftest.wait_until_postgresql_ready(container_engine=container_engine)
    env = os.environ
    env["PGPASSWORD"] = container.password  # Insecure, only acceptable in test environment
    subprocess.run(
        (
            "/usr/lib/postgresql/18/lib/pgxs/src/test/regress/pg_regress",
            "--host",
            "localhost",
            "--user",
            "postgres",
            "--schedule",
            "./parallel_schedule",
            "--outputdir",
            workspace,
        ),
        cwd=regression_test_sources,
        check=True,
        env=env,
    )
    assert False
