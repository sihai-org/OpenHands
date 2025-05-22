import argparse
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

docker_cli = docker.from_env()


def docker_build(work_dir: Path, image_name: str):
    return docker_cli.images.build(path=str(work_dir), tag=image_name, rm=True)


def docker_run(image: str, container_name: str, host_port: int, container_port: int):
    container = docker_cli.containers.run(
        image,
        detach=True,
        environment={"PORT": str(container_port)},
        ports={f"{container_port}/tcp": host_port},
        stdin_open=True,
        tty=True,
        auto_remove=False,
        name=container_name,
    )
    return container


def check_port_in_use(port: int):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False  # 端口未被占用
        except socket.error:
            return True  # 端口被占用


# def get_port(dockerfile_path: Path):
#     with open(dockerfile_path, "r") as f:
#         for line in f:
#             if line.startswith("ENV") and "PORT" in line:
#                 if "=" in line:
#                     port = line.split("=")[-1]
#                 else:
#                     port = line.split()[-1]
#                 return port.strip()
#     return 8000


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
def main(base_dir: Path, articles: Path, base_url: str, port_start: int, container_port: int):
    workspace_base = base_dir / "workspace"
    article_base = base_dir / "articles"

    c2a = []
    with articles.open() as f:
        articles = json.load(f)
    for origin_article in articles:
        article_path = article_base / f"{origin_article['uuid']}.json"
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
        dockerfile_list = list((workspace_base / uuid_str).glob("**/Dockerfile"))
        dockerfile_path = dockerfile_list[0] if dockerfile_list else None
        if not dockerfile_path:
            item["deploy_failed_reason"] = "no dockerfile found"
            continue
        container_name = f"{base_dir.name}-{uuid_str}"
        image_name = f"{uuid_str}:{base_dir.name}"
        print(f"Building docker image image_name")
        try:
            try:
                image = docker_cli.images.get(name=image_name)
            except docker.errors.ImageNotFound:
                print(f"Image {image_name} not found, building...")
                image = docker_build(dockerfile_path.parent, image_name)

            print(f"got image {image}, trying to run...")

            host_port = port_start
            while check_port_in_use(host_port):
                host_port += 1

            print(f"Check if container: {container_name} exists")
            try:
                container = docker_cli.containers.get(container_name)
            except docker.errors.NotFound:
                container = None
            if container:
                print(f"Container {container_name} exists...")
            else:
                print(f"Running docker container for {container_name} on port {host_port}")
                container = docker_run(image_name, container_name, host_port, container_port)
            alive, failed_reason = check_aliveness(container, host_port)
            if alive:
                print(f"Container app {container.name} is alive and healthy. Setting auto restart on it.")
                container.update(restart_policy={"Name": "always"})
                item["host_url"] = f"{base_url}:{host_port}"
            else:
                item["deploy_failed_reason"] = failed_reason
                print(f"container app {container_name} is not alive: {failed_reason}")
                container.stop()
                container.remove(force=True)
                print(f"Container :{container_name} stopped and removed.")

            item["is_auto_deployed"] = alive
        except Exception as e:
            # traceback.print_exc()
            print(f"Failed to build or run docker image for {image_name}:{type(e)} {e}")
            item["deploy_failed_reason"] = f"Failed to build or run: {traceback.format_exc()}"

    pd.DataFrame(c2a).to_csv(base_dir / "c2a.csv", index=False)


def get_parser():
    parser = argparse.ArgumentParser(description="Batch deploy docker containers")
    parser.add_argument("base_dir", type=Path, help="Base directory for workspace and articles")
    parser.add_argument("articles", type=Path, help="Path to the articles JSON file")
    parser.add_argument("--base_url", type=str, default="http://show.datou.me", help="Base URL for apps")
    parser.add_argument("--port_start", type=int, default=9001, help="Starting port for the containers")
    parser.add_argument("--container_port", type=int, default=8000, help="Container port to expose")
    return parser


if __name__ == '__main__':
    args = get_parser().parse_args()
    main(**vars(args))
