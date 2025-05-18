import json
import multiprocessing
import os
import shutil
import sys
import traceback
from pathlib import Path

NUM_PROCESSES = 8

META_BASE = Path("ruic")
CONFIG_FILE = META_BASE / "config.toml"
ARTICLES_FILE = META_BASE / "selected_articles.json"
PROMPT_FILE = META_BASE / "prompt_c2a_v2.prompt"

BASE = Path("./ruic_5articles_gpt-4-1-2025-04-14")
LLM_CONFIG = "5articles-gpt-4-1-2025-04-14"
WORKSPACE_BASE = BASE / "workspace"
ARTICLES_DIR = BASE / "articles"
LOG_BASE = BASE / "logs"
BASE.mkdir(parents=True, exist_ok=True)
WORKSPACE_BASE.mkdir(parents=True, exist_ok=True)
ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
LOG_BASE.mkdir(parents=True, exist_ok=True)


def run_datou(uuid_str: str):
    article_path = ARTICLES_DIR / f"{uuid_str}.json"
    article = json.load(article_path.open())
    workspace_dir = WORKSPACE_BASE / uuid_str
    log_dir = LOG_BASE / uuid_str

    if article["status"] == "done":
        print(f"Article {uuid_str} done, skipped.")
        return

    print(f"Article {uuid_str} not in init status, cleaning workspace and logs...")
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir, ignore_errors=True)
    if log_dir.exists():
        shutil.rmtree(log_dir, ignore_errors=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"Running datou with task: {uuid_str}, title: {article['title']}, workspace_dir: {workspace_dir}")
    sys.argv = [
        f"main.py",
        # f"--file={task_file}",
        f"--config-file={CONFIG_FILE}",
        f"--llm-config={LLM_CONFIG}",
    ]
    os.environ["WORKSPACE_BASE"] = str(workspace_dir)

    article["status"] = "running"
    json.dump(article, article_path.open("w"), ensure_ascii=False, indent=4)

    import openhands.core.main as headless_main
    import openhands.core.logger as oh_logger

    oh_logger.LOG_DIR = log_dir
    new_handler = oh_logger.get_file_handler(str(log_dir), oh_logger.current_log_level)
    new_prompt_logger = oh_logger._get_llm_file_handler("prompt", oh_logger.current_log_level)
    new_response_logger = oh_logger._get_llm_file_handler("response", oh_logger.current_log_level)

    headless_main.logger.addHandler(new_handler)
    oh_logger.llm_prompt_logger.addHandler(new_prompt_logger)
    oh_logger.llm_response_logger.addHandler(new_response_logger)

    args = headless_main.parse_arguments()
    config: headless_main.AppConfig = headless_main.setup_config_from_args(args)
    sid = headless_main.generate_sid(config, None)
    try:
        state: headless_main.State = headless_main.asyncio.run(
            headless_main.run_controller(
                config=config,
                initial_user_action=headless_main.MessageAction(content=article["article"]["prompt"]),
                sid=sid,
                fake_user_response_fn=headless_main.auto_continue_response,
            )
        )
        article["cost"] = {
            "input_tokens": state.metrics.accumulated_token_usage.prompt_tokens,
            "output_tokens": state.metrics.accumulated_token_usage.completion_tokens,
            "cost": state.metrics.accumulated_cost,
        }
        if state.agent_state is not headless_main.AgentState.FINISHED:
            raise Exception(f"Task not finished: {state.agent_state}")
        article["status"] = "done"
    except Exception:
        traceback.print_exc()
        article["status"] = "failed"
    finally:
        headless_main.logger.removeHandler(new_handler)
        oh_logger.llm_prompt_logger.removeHandler(new_prompt_logger)
        oh_logger.llm_response_logger.removeHandler(new_response_logger)
        os.system(f"docker rm -f openhands-runtime-{sid}")
        json.dump(article, article_path.open("w"), ensure_ascii=False, indent=4)


def worker(task_queue: multiprocessing.Queue, result_queue: multiprocessing.Queue):
    while not task_queue.empty():
        uuid_str = task_queue.get()
        res = ""
        try:
            run_datou(uuid_str)
            res = f"{uuid_str} run success"
        except Exception as e:
            print(f"Error processing {uuid_str}: {e}")
            res = traceback.format_exc()
        finally:
            result_queue.put(res)


# def filter_condition(article):
#     return article["executable"] and article["complexity"] >= 2


def main():
    with ARTICLES_FILE.open() as f:
        articles = json.load(f)
    print(f"Loaded {len(articles)} articles")
    filtered_articles = articles
    print(f"{len(filtered_articles)} articles to process:")
    # print(*[article["title"] for article in remained_articles], sep="\n")
    prompt = PROMPT_FILE.read_text()

    task_queue, result_queue = multiprocessing.Queue(), multiprocessing.Queue()

    for article in filtered_articles:
        task_content = prompt.replace("{{content_source}}", f"{article['title']}\n{article['article']['md']}")
        article["article"]["prompt"] = task_content
        uuid_str = article["uuid"]
        article_path = ARTICLES_DIR / f"{uuid_str}.json"
        if not article_path.exists():
            json.dump(article, article_path.open("w"), ensure_ascii=False, indent=4)
        task_queue.put(uuid_str)

    processes = []
    for _ in range(NUM_PROCESSES):
        p = multiprocessing.Process(target=worker, args=(task_queue, result_queue))
        processes.append(p)
        p.start()

    for p in processes:
        p.join()

    while not result_queue.empty():
        res = result_queue.get()
        print(res)


if __name__ == "__main__":
    main()
