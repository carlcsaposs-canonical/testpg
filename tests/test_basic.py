import subprocess


def test_psql_in_container(container, container_engine):
    subprocess.run(
        (
            *container_engine.sudo_prefix,
            container_engine.command_prefix,
            "exec",
            "-i",
            "mypostgres",
            "psql",
            "--command",
            "SELECT 1;",
        ),
        check=True,
        encoding="utf-8",
        input=container.password,
    )
