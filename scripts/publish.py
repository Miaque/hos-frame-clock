"""使用当前用户 Maven 中的 nexus 服务配置构建并发布到 Nexus。"""

import os
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    project = Path(__file__).resolve().parents[1]
    settings = Path.home() / ".m2" / "settings.xml"
    root = ET.parse(settings).getroot()
    for node in root.iter():
        node.tag = node.tag.split("}")[-1]
    server = next(
        (s for s in root.findall("./servers/server") if s.findtext("id") == "nexus"),
        None,
    )
    if server is None:
        raise SystemExit("Maven settings.xml 缺少 nexus server")
    env = os.environ.copy()
    for field, variable in (
        ("username", "UV_PUBLISH_USERNAME"),
        ("password", "UV_PUBLISH_PASSWORD"),
    ):
        value = server.findtext(field) or ""
        if not value or value.startswith(("{", "${")):
            raise SystemExit(f"Maven nexus {field} 需要可直接使用的配置值")
        env[variable] = value
    # 避免终端环境中的 PyPI API Token 覆盖基本认证凭据。
    env.pop("UV_PUBLISH_TOKEN", None)
    with (project / "pyproject.toml").open("rb") as file:
        version = tomllib.load(file)["project"]["version"]
    subprocess.run(["uv", "build"], cwd=project, check=True)
    subprocess.run(
        [
            "uv",
            "publish",
            "--publish-url",
            "http://172.18.6.206:8081/repository/pypi-hosted/",
            "--trusted-publishing",
            "never",
            f"dist/hos_frame_clock-{version}-py3-none-any.whl",
            f"dist/hos_frame_clock-{version}.tar.gz",
        ],
        cwd=project,
        env=env,
        check=True,
    )


if __name__ == "__main__":
    main()
