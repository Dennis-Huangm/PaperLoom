from __future__ import annotations

import argparse
import json
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
from .comparison import ComparisonService
from .version_tracker import VersionTracker
from .version_sync import PaperVersionSync
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


def build_parser(prog: str = "paperloom") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description="PaperLoom · 知织 — 把论文织成知识")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="执行一次每日检索、推荐日报生成和投递；深度报告使用 report")
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
    weekly = sub.add_parser("weekly", help="生成当前研究方向的研究活动周报")
    weekly.add_argument("--include-notes", action="store_true", help="将本期个人笔记附入周报，不发送给模型；启用自动同步时也会写入 Obsidian")
    compare = sub.add_parser("compare", help="比较 2–5 篇不同论文的本地指定版本")
    compare.add_argument("papers", nargs="+", help="本地已收藏、推荐或生成报告的 arXiv ID，必须带 v 后缀")
    compare.add_argument("--question", default="", help="比较关注点，最多 1000 字")
    sub.add_parser("versions", help="检查已追踪论文的 arXiv 新版本")
    sync = sub.add_parser("sync", help="同步单篇论文的最新或指定修订版，保留旧版")
    sync.add_argument("arxiv_id", help="不带版本后缀的 arXiv ID")
    sync.add_argument("--target-version", type=int, help="固定目标修订版；省略时查询最新")
    sync.add_argument("--retry", action="store_true", help="继续指定版本上次选择但未完成的步骤")
    sync.add_argument("--report", action="store_true", help="生成该版本的阅读报告")
    sync.add_argument("--zotero", action="store_true", help="同步该版本的 Zotero 附件")
    sync.add_argument("--obsidian", action="store_true", help="同步该版本的 Obsidian 笔记")
    citation = sub.add_parser("citation", help="为指定论文生成相关工作地图")
    citation.add_argument("arxiv_id")
    sub.add_parser("obsidian-sync", help="同步推荐、报告和周报到 Obsidian vault")
    sub.add_parser("profiles", help="列出研究方向档案")
    activate = sub.add_parser("activate", help="切换当前研究方向")
    activate.add_argument("profile_id")
    backup = sub.add_parser("backup", help="本地备份、校验预览及独立目录恢复")
    operations = backup.add_subparsers(dest="backup_action", required=True)
    create = operations.add_parser("create", help="创建并校验备份")
    create.add_argument("--reports", action="store_true", help="包含报告及配图")
    create.add_argument("--pdfs", action="store_true", help="包含本地 PDF")
    create.add_argument("--secrets", action="store_true", help="包含明文 .env 凭据")
    for action in ("preview", "restore"):
        command = operations.add_parser(action)
        command.add_argument("archive", type=Path)
        command.add_argument("--name", required=True, help="restored 下的恢复目录名称")
        command.add_argument("--mode", choices=["keep", "replace"], default="keep")
        if action == "restore":
            command.add_argument("--token", required=True, help="预览返回的 token；目标或备份变化后失效")
    operations.add_parser("reconcile", help="整理中断的恢复操作，保留所有资料")
    schedule = sub.add_parser("schedule", help="按已保存计划运行多研究方向调度")
    mode = schedule.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="检查一次到期计划，等待本次任务完成后退出")
    mode.add_argument("--status", action="store_true", help="只读查看计划和上次登记状态，不执行任务")
    return parser


def doctor(config_path: Path) -> int:
    print(f"Python: {sys.executable}")
    print(f"Config: {config_path.resolve()}")
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
    # Initialization exercises httpx's effective proxy environment without any
    # network request. Do not print exception text: proxy URLs may contain keys.
    import httpx

    transport_hint = ""
    try:
        client = httpx.Client()
        client.close()
        checks["HTTP 客户端与代理配置"] = True
    except ImportError:
        checks["HTTP 客户端与代理配置"] = False
        transport_hint = (
            '代理依赖缺失；使用 SOCKS 代理时，请在当前 Python 环境安装 '
            '"httpx[socks]>=0.27,<1"。保留需要的代理出口配置。'
        )
    except (ValueError, OSError) as exc:
        checks["HTTP 客户端与代理配置"] = False
        transport_hint = (
            f"HTTP 客户端初始化失败（{type(exc).__name__}）；"
            "请检查当前进程的代理 URL 和 SSL_CERT_FILE/SSL_CERT_DIR 配置。"
        )
    for label, okay in checks.items():
        print(f"[{'OK' if okay else '--'}] {label}")
    if transport_hint:
        print(f"[--] {transport_hint}")
    print("[说明] 此检查不联网；通过不代表 arXiv、模型服务或定时触发已验证。")
    if config:
        if "example.com" in config.metadata.openalex_email:
            print("[--] 请在 config.yaml 中填写 OpenAlex 联系邮箱")
        if config.delivery.email_enabled:
            print(f"[{'OK' if os.getenv(config.delivery.email_address_env) else '--'}] {config.delivery.email_address_env}")
            print(f"[{'OK' if os.getenv(config.delivery.password_env) else '--'}] {config.delivery.password_env}")
    return 0 if all(checks[key] for key in ("配置文件", "PyMuPDF", "HTTP 客户端与代理配置")) else 1


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def main(argv: list[str] | None = None) -> None:
    command = Path(sys.argv[0]).stem
    args = build_parser("arxiv-ra" if command == "arxiv-ra" else "paperloom").parse_args(argv)
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
    if args.command == "backup":
        from .backup import BackupService
        try:
            service = BackupService(config_path)
            if args.backup_action == "create":
                result = {"archive": str(service.create(reports=args.reports, pdfs=args.pdfs, secrets=args.secrets))}
            elif args.backup_action == "preview":
                result = service.preview(args.archive, args.name, args.mode)
            elif args.backup_action == "restore":
                result = service.restore(args.archive, args.name, token=args.token, mode=args.mode)
            else:
                result = {"reconciled": service.recover_interrupted()}
        except (OSError, ValueError, RuntimeError) as exc:
            raise SystemExit(str(exc)) from exc
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    profiles = ProfileManager(config_path.parent)
    if args.command != "schedule":
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
    if args.command == "schedule":
        from .scheduler import ProfileScheduler
        from .web_jobs import JobManager
        output = Path(config.output_dir)
        output = (output if output.is_absolute() else config_path.parent / output).resolve()
        if args.status:
            print(json.dumps(ProfileScheduler(config_path, output, None).view(), ensure_ascii=False, indent=2))
            return
        try:
            jobs = JobManager(output, config.jobs.max_parallel)
        except RuntimeError as exc:
            print(f"本次未启动调度：{exc}")
            return
        scheduler = ProfileScheduler(config_path, output, jobs)
        try:
            if args.once:
                submitted = scheduler.tick()
                while jobs.active_count or jobs.pending:
                    threading.Event().wait(.1)
                scheduler.sync_state()
                print(json.dumps({"submitted": submitted, "schedule": scheduler.view()}, ensure_ascii=False, indent=2))
                if any(jobs.get(job_id).status in {"failed", "cancelled", "interrupted"} for job_id in submitted):
                    raise SystemExit(1)
            else:
                scheduler.start()
                print("多方向调度已启动；每 30 秒检查到期计划，按 Ctrl+C 停止。")
                while True:
                    threading.Event().wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            scheduler.close()
            jobs.close()
        return
    if args.command == "gui":
        try:
            import uvicorn

            from .web import create_app
        except ImportError as exc:
            raise SystemExit('GUI 依赖未安装，请执行：python -m pip install -e ".[gui]"') from exc
        url = f"http://127.0.0.1:{args.port}"
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        print(f"PaperLoom GUI: {url}")
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
        with WeeklySynthesizer(config, config_path.parent) as synthesizer:
            path = synthesizer.generate(include_notes=args.include_notes)
        print(path.resolve())
        return
    if args.command == "compare":
        with ComparisonService(config, config_path.parent) as service:
            try:
                snapshot = service.prepare(args.papers, args.question)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            path = service.generate(snapshot)
        print(path.resolve())
        return
    if args.command == "versions":
        with VersionTracker(config, config_path.parent) as tracker:
            path = tracker.check()
        print(path.resolve())
        return
    if args.command == "citation":
        with CitationExplorer(config, config_path.parent) as explorer:
            path = explorer.generate(args.arxiv_id)
        print(path.resolve())
        return
    if args.command == "sync":
        with PaperVersionSync(config, config_path.parent) as synchronizer:
            path = synchronizer.sync(args.arxiv_id, target_version=args.target_version, retry=args.retry,
                                     report=args.report, zotero=args.zotero, obsidian=args.obsidian)
        print(path.resolve())
        return
    if args.command == "obsidian-sync":
        with ObsidianExporter(config, config_path.parent) as exporter:
            path = exporter.sync_all()
        print(path.resolve())
        return
    with DailyPipeline(config, config_path.parent) as pipeline:
        if args.command == "demo":
            path = pipeline.run(force=True, demo=True)
        elif args.command == "run":
            path = pipeline.run(force=args.force, demo=False, deliver=not args.no_email)
        elif args.command == "report":
            path = pipeline.report_arxiv_id(args.arxiv_id)
        else:
            raise SystemExit(f"未知命令：{args.command}")
    print(path.resolve())
