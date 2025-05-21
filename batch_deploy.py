import json
import socket
import time
import traceback
from pathlib import Path

import docker
import docker.errors
import docker.models.containers
import pandas as pd
import requests

BASE_DIR = Path("ruic_385_20")
WORKSPACE_BASE = BASE_DIR / "workspace"
ARTICLE_BASE = BASE_DIR / "articles"
ARTICLES = Path("ruic") / "csdn_10k_category_385_random20.json"
BASE_URL = "http://show.datou.me"
PORT_START = 9001

docker_cli = docker.from_env()


def docker_build(work_dir: Path, uuid_str: str):
    return docker_cli.images.build(path=str(work_dir), tag=f"{BASE_DIR}-{uuid_str}:latest", rm=True)


def docker_run(image: str, uuid_str: str, host_port: int, container_port: int):
    container = docker_cli.containers.run(
        image,
        detach=True,
        environment={"PORT": str(container_port)},
        ports={f"{container_port}/tcp": host_port},
        stdin_open=True,
        tty=True,
        auto_remove=False,
        name=f"{BASE_DIR}-{uuid_str}",
    )
    return container


def check_port_in_use(port: int):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False  # 端口未被占用
        except socket.error:
            return True  # 端口被占用


def get_port(dockerfile_path: Path):
    with open(dockerfile_path, "r") as f:
        for line in f:
            if line.startswith("ENV") and "PORT" in line:
                if "=" in line:
                    port = line.split("=")[-1]
                else:
                    port = line.split()[-1]
                return port.strip()
    return 8000


def check_aliveness(
    container: docker.models.containers.Container, host_port, timeout: int = 20, interval: int = 3
) -> tuple[bool, str | None]:
    start = time.time()
    while time.time() - start < timeout:
        try:
            time.sleep(interval)
            container.reload()
            if container.status == "running":
                try:
                    resp = requests.get(f"http://localhost:{host_port}", timeout=5)
                    resp.raise_for_status()
                    if resp.status_code == 200:
                        return True, None
                except Exception as e:
                    print(f"Error checking container health: {e}, retrying...")
                    continue
            elif container.status == "exited":
                return False, f"Container exited with code {container.attrs['State']['ExitCode']}"
        except:
            return False, traceback.format_exc()
    return False, f"aliveness check timeout after {timeout} seconds, container status: {container.status}"


# TODO: 并发执行deploy
def main():
    c2a = []
    with ARTICLES.open() as f:
        articles = json.load(f)
    for origin_article in articles:
        article_path = ARTICLE_BASE / f"{origin_article['uuid']}.json"
        if not article_path.exists():
            continue
        with article_path.open() as f:
            article = json.load(f)
        item = {
            "uuid": article["uuid"],
            "url": article["url"],
            "title": article["title"],
            "executable": article["executable"],
            "complexity": article["complexity"],
            "confidence": article["confidence"],
            "cost": article["cost"]["cost"],
            "input_tokens": article["cost"]["input_tokens"],
            "output_tokens": article["cost"]["output_tokens"],
            "app_status": article["status"],
            "host_url": None,
            "is_auto_deployed": False,
            "deploy_failed_reason": None,
        }
        c2a.append(item)
        if article["status"] != "done":
            print(f"{article['uuid']} not done, skipping...")
            continue

        uuid_str = article_path.stem
        dockerfile_list = list((WORKSPACE_BASE / uuid_str).glob("**/Dockerfile"))
        dockerfile_path = dockerfile_list[0] if dockerfile_list else None
        if not dockerfile_path:
            item["deploy_failed_reason"] = "no dockerfile found"
            continue
        print(f"Building docker image for {BASE_DIR}-{uuid_str}:latest")
        image_name = f"{BASE_DIR}-{uuid_str}:latest"
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

            print(f"Check if container {uuid_str} exists")
            try:
                container = docker_cli.containers.get(uuid_str)
            except docker.errors.NotFound:
                container = None
            if container:
                print(f"Container {uuid_str} exists, removing...")
                container.remove(force=True)
            print(f"Running docker container for {uuid_str} on port {host_port}")
            container_port = 8000
            container = docker_run(f"{uuid_str}:latest", uuid_str, host_port, container_port)
            alive, failed_reason = check_aliveness(container, host_port)
            if alive:
                print(f"Container for task {container.name} is alive and healthy. Setting auto restart on it.")
                container.update(restart_policy={"Name": "always"})
                item["host_url"] = f"{BASE_URL}:{host_port}"
            else:
                item["deploy_failed_reason"] = failed_reason
                print(f"task {uuid_str} is not alive: {failed_reason}")
                container.stop()
                container.remove(force=True)
                print(f"Container {container.id} stopped and removed.")

            item["is_auto_deployed"] = alive
        except Exception as e:
            # traceback.print_exc()
            print(f"Failed to build or run docker image for {uuid_str}:{type(e)} {e}")
            item["deploy_failed_reason"] = f"Failed to build or run: {traceback.format_exc()}"

    pd.DataFrame(c2a).to_csv(BASE_DIR / "c2a.csv", index=False)


if __name__ == '__main__':
    main()
