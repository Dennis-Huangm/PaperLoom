from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import webbrowser
from pathlib import Path

from . import __version__
from .config import load_config
from .emailer import send_test_email
from .pipeline import DailyPipeline
from .profiles import ProfileManager
from .weekly import WeeklySynthesizer
from .version_tracker import VersionTracker
from .citation_graph import CitationExplorer
from .obsidian import ObsidianExporter


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arxiv-ra", description="个性化 arXiv 科研日报与论文阅读报告")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次每日检索、报告生成和投递")
    run.add_argument("--force", action="store_true", help="忽略已处理状态，重新生成")
    run.add_argument("--no-email", action="store_true", help="生成真实推荐但不发送邮件")
    sub.add_parser("demo", help="不联网、不调用 LLM，生成离线示例日报")
    one = sub.add_parser("report", help="为单个 arXiv ID 生成报告")
    one.add_argument("arxiv_id")
    gui = sub.add_parser("gui", help="启动仅限本机访问的可视化界面")
    gui.add_argument("--port", type=int, default=8000, help="本地监听端口，默认 8000")
    gui.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    sub.add_parser("test-email", help="发送一封 QQ SMTP 配置测试邮件")
    sub.add_parser("doctor", help="检查配置与可选组件")
    sub.add_parser("weekly", help="生成当前研究方向的每周研究综述")
    sub.add_parser("versions", help="检查已追踪论文的 arXiv 新版本")
    citation = sub.add_parser("citation", help="为指定论文生成相关工作地图")
    citation.add_argument("arxiv_id")
    sub.add_parser("obsidian-sync", help="同步推荐、报告和周报到 Obsidian vault")
    sub.add_parser("profiles", help="列出研究方向档案")
    activate = sub.add_parser("activate", help="切换当前研究方向")
    activate.add_argument("profile_id")
    return parser


def doctor(config_path: Path) -> int:
    config = load_config(config_path) if config_path.exists() else None
    llm_label = (
        f"{config.llm.api_key_env} 或 {config.llm.base_url_env}"
        if config
        else "LLM API key 或 base URL"
    )
    llm_ready = bool(
        config
        and (
            os.getenv(config.llm.api_key_env)
            or os.getenv(config.llm.base_url_env)
        )
    )
    checks = {
        "配置文件": config_path.exists(),
        llm_label: llm_ready,
        "Docling（可选）": _has_module("docling"),
        "PyMuPDF": _has_module("pymupdf"),
    }
    for label, okay in checks.items():
        print(f"[{'OK' if okay else '--'}] {label}")
    if config:
        if "example.com" in config.metadata.openalex_email:
            print("[--] 请在 config.yaml 中填写 OpenAlex 联系邮箱")
        if config.delivery.email_enabled:
            print(f"[{'OK' if os.getenv(config.delivery.email_address_env) else '--'}] {config.delivery.email_address_env}")
            print(f"[{'OK' if os.getenv(config.delivery.password_env) else '--'}] {config.delivery.password_env}")
    return 0 if checks["配置文件"] and checks["PyMuPDF"] else 1


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    _load_dotenv(config_path.parent / ".env")
    if args.command == "doctor":
        raise SystemExit(doctor(config_path))
    if not config_path.exists():
        example = config_path.parent / "config.example.yaml"
        if example.exists():
            shutil.copy2(example, config_path)
            print(f"已从模板创建 {config_path}，请先填写研究兴趣和邮箱。", file=sys.stderr)
        else:
            raise SystemExit(f"配置文件不存在：{config_path}")
    profiles = ProfileManager(config_path.parent)
    profiles.ensure_default(config_path)
    if args.command == "profiles":
        for item in profiles.list():
            marker = "*" if item["active"] else " "
            print(f"{marker} {item['id']:<24} {item['name']}")
        return
    if args.command == "activate":
        try:
            profiles.activate(args.profile_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"已切换到研究方向：{profiles.get(args.profile_id).get('name', args.profile_id)}")
        return
    config = load_config(config_path)
    if args.command == "gui":
        try:
            import uvicorn

            from .web import create_app
        except ImportError as exc:
            raise SystemExit('GUI 依赖未安装，请执行：python -m pip install -e ".[gui]"') from exc
        url = f"http://127.0.0.1:{args.port}"
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        print(f"arXiv Research Assistant GUI: {url}")
        uvicorn.run(create_app(config_path), host="127.0.0.1", port=args.port, log_level="info")
        return
    if args.command == "test-email":
        if not config.delivery.email_enabled:
            raise SystemExit("邮件投递尚未启用：请将 delivery.email_enabled 设置为 true。")
        try:
            send_test_email(config.delivery)
        except Exception as exc:
            raise SystemExit(f"QQ 邮箱测试未发送：{exc}") from exc
        print("QQ 邮箱测试邮件已发送。")
        return
    if args.command == "weekly":
        path = WeeklySynthesizer(config, config_path.parent).generate()
        print(path.resolve())
        return
    if args.command == "versions":
        path = VersionTracker(config, config_path.parent).check()
        print(path.resolve())
        return
    if args.command == "citation":
        path = CitationExplorer(config, config_path.parent).generate(args.arxiv_id)
        print(path.resolve())
        return
    if args.command == "obsidian-sync":
        path = ObsidianExporter(config, config_path.parent).sync_all()
        print(path.resolve())
        return
    pipeline = DailyPipeline(config, config_path.parent)
    if args.command == "demo":
        path = pipeline.run(force=True, demo=True)
    elif args.command == "run":
        path = pipeline.run(force=args.force, demo=False, deliver=not args.no_email)
    elif args.command == "report":
        path = pipeline.report_arxiv_id(args.arxiv_id)
    else:
        raise SystemExit(f"未知命令：{args.command}")
    print(path.resolve())
