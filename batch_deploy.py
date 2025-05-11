import json
import socket
import traceback
from pathlib import Path
from time import sleep

import docker
import docker.errors
import pandas as pd
import requests

BASE_DIR = Path("ruic_first200")
WORKSPACE_BASE = BASE_DIR / "workspace"
ARTICLE_BASE = BASE_DIR / "articles"
BASE_URL = "http://articles.datou.me"
PORT_START = 9001

docker_cli = docker.from_env()


def docker_build(work_dir: Path, uuid_str: str):
    return docker_cli.images.build(path=str(work_dir), tag=f"{uuid_str}:latest", rm=True)


def docker_run(image: str, uuid_str: str, host_port: int, container_port: int):
    container = docker_cli.containers.run(
        image,
        detach=True,
        ports={f"{container_port}/tcp": host_port},
        stdin_open=True,
        tty=True,
        auto_remove=True,
        name=uuid_str,
    )
    return container


def check_port_in_use(port: int):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False  # 端口未被占用
        except socket.error:
            return True  # 端口被占用


def get_expose_port(dockerfile_path: Path):
    with open(dockerfile_path, "r") as f:
        for line in f:
            if line.startswith("EXPOSE"):
                return int(line.split()[1])
    raise ValueError(f"No exposed port for {dockerfile_path}")


# 并发执行deploy

def main():
    c2a = []
    for article_path in ARTICLE_BASE.iterdir():
        uuid_str = article_path.stem
        dockerfile_list = list((WORKSPACE_BASE / uuid_str).glob("*/Dockerfile"))
        dockerfile_path = dockerfile_list[0] if dockerfile_list else None
        if not dockerfile_path:
            continue
        print(f"Building docker image for {uuid_str}")
        image_name = f"{uuid_str}:latest"
        try:
            try:
                image = docker_cli.images.get(name=image_name)
            except docker.errors.ImageNotFound:
                print(f"Image {image_name} not found, building...")
                image = docker_build(dockerfile_path.parent, uuid_str)

            print(f"got image {image}, trying to run...")

            host_port = PORT_START
            while check_port_in_use(host_port):
                host_port += 1

            print(f"Running docker container for {uuid_str} on port {host_port}")
            container_port = get_expose_port(dockerfile_path)
            container = docker_run(f"{uuid_str}:latest", uuid_str, host_port, container_port)
            print(f"Container {container.id} is running...")
            # TODO: 睡久一点
            sleep(5)
            print("Sleep for 5s to make sure container is alive...")
            alive = False
            try:
                resp = requests.get(f"http://localhost:{host_port}")
                resp.raise_for_status()
                print(f"http://localhost:{host_port} is alive.")
                alive = True
            except:
                traceback.print_exc()
                print(f"task {uuid_str} is not alive.")
                alive = False

            article = json.load(article_path.open())

            c2a.append({
                "uuid": article["uuid"],
                "url": article["url"],
                "title": article["title"],
                "executable": article["executable"],
                "complexity": article["complexity"],
                "confidence": article["confidence"],
                "cost": article["cost"]["cost"],
                "input_tokens": article["cost"]["input_tokens"],
                "output_tokens": article["cost"]["output_tokens"],
                "host_url": f"{BASE_URL}:{host_port}" if alive else "",
            })
        except:
            traceback.print_exc()
            print(f"Failed to build or run docker image for {uuid_str}")

    pd.DataFrame(c2a).to_csv(BASE_DIR / "c2a.csv", index=False)


if __name__ == '__main__':
    main()
