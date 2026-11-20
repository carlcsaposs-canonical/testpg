import pathlib
import re
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


# TODO: remove when adopt-info used in rockcraft.yaml
def test_rockcraft_yaml_version(postgresql_version):
    for line in (pathlib.Path(__file__).parent.parent / "rockcraft.yaml").read_text().splitlines():
        if line.startswith("version:"):
            match = re.fullmatch(r"version: +['\"](?P<version>[0-9]+\.[0-9]+)['\"]", line)
            if not match:
                raise ValueError
            rockcraft_yaml_version = match.group("version")
            assert rockcraft_yaml_version == postgresql_version
            break
    else:
        assert False, "`version` not found in rockcraft.yaml"
