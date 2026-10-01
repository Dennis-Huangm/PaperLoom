"""Optional curl transport for direct arXiv PDF downloads on Windows."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .task_runtime import task_checkpoint, task_progress
from .rate_limit import shared_rate_limit


def download_with_curl(url: str, destination: Path, label: str, max_retries: int) -> None:
    # Prefer the Windows system executable over another curl found on PATH.
    system_curl = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/curl.exe"
    executable = str(system_curl) if system_curl.is_file() else shutil.which("curl")
    if not executable:
        raise RuntimeError("curl PDF 下载后端不可用：未找到 curl 可执行文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    temporary = destination.with_name(f".~{token}.part")
    chunk_path = destination.with_name(f".~{token}.chunk")
    headers_path = destination.with_name(f".~{token}.headers")
    received = 0
    total = None
    modified = None
    started = time.monotonic()
    # Bounded ranges avoid repeatedly transferring the entire large response.
    chunk_size = 1024 * 1024
    try:
        with temporary.open("wb") as output:
            while total is None or received < total:
                end = received + chunk_size - 1
                if total is not None:
                    end = min(end, total - 1)
                for attempt in range(max_retries + 1):
                    task_checkpoint()
                    chunk_path.unlink(missing_ok=True)
                    headers_path.unlink(missing_ok=True)
                    command = [executable, "--disable", "--silent", "--show-error", "--fail",
                               "--location", "--noproxy", "*", "--proto", "=https",
                               "--proto-redir", "=https", "--connect-timeout", "15",
                               "--max-time", "90", "--header", "Accept-Encoding: identity",
                               "--range", f"{received}-{end}", "--dump-header", str(headers_path),
                               "--output", str(chunk_path)]
                    # Some arXiv backends fail conditional range requests. On
                    # retry, request the same range without the condition, then
                    # still validate Last-Modified and total size before appending.
                    if modified and attempt == 0:
                        command.extend(["--header", f"If-Range: {modified}"])
                    command.append(url)
                    with shared_rate_limit("arxiv-pdf-curl", 3.0, checkpoint=task_checkpoint):
                        task_checkpoint()
                        process = subprocess.Popen(
                            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                    try:
                        while process.poll() is None:
                            task_checkpoint()
                            partial = chunk_path.stat().st_size if chunk_path.exists() else 0
                            elapsed = max(time.monotonic() - started, 0.001)
                            size = f"{(received + partial) / 1048576:.1f}"
                            if total:
                                size += f" / {total / 1048576:.1f}"
                            percent = 27 + int(12 * min((received + partial) / total, 1)) if total else 27
                            task_progress(
                                f"正在下载论文 PDF（curl 直连）：{size} MiB · "
                                f"{(received + partial) / 1024 / elapsed:.0f} KiB/s · 已用 {elapsed:.0f} 秒",
                                percent,
                            )
                            time.sleep(0.25)
                    finally:
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                    failure = f"curl 退出码 {process.returncode}"
                    if headers_path.exists():
                        statuses = re.findall(r"(?m)^HTTP/\S+ (\d{3})", headers_path.read_text(encoding="iso-8859-1"))
                        if statuses:
                            failure += f"（HTTP {statuses[-1]}）"
                    if process.returncode == 0:
                        blocks = re.split(r"\r?\n\r?\n", headers_path.read_text(encoding="iso-8859-1"))
                        final = next((block for block in reversed(blocks) if block.startswith("HTTP/")), "")
                        status_line, _, fields = final.partition("\n")
                        headers = {}
                        for line in fields.splitlines():
                            key, separator, value = line.partition(":")
                            if separator:
                                headers[key.lower()] = value.strip()
                        span = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("content-range", ""))
                        length = chunk_path.stat().st_size
                        full_response = bool(re.match(r"HTTP/\S+ 200\b", status_line))
                        if full_response and received == 0 and headers.get("content-length", "").isdecimal():
                            candidate_total = int(headers["content-length"])
                            valid = length == candidate_total
                        else:
                            candidate_total = int(span[3]) if span else 0
                            valid = bool(re.match(r"HTTP/\S+ 206\b", status_line) and span
                                         and int(span[1]) == received and int(span[2]) == min(end, candidate_total - 1)
                                         and length == int(span[2]) - received + 1)
                        valid = valid and candidate_total > 10240 and (total is None or total == candidate_total)
                        valid = valid and headers.get("content-encoding", "identity") == "identity"
                        valid = valid and (not modified or headers.get("last-modified") == modified)
                        if valid:
                            if received == 0:
                                with chunk_path.open("rb") as first:
                                    valid = first.read(5) == b"%PDF-"
                            if valid:
                                total = candidate_total
                                modified = headers.get("last-modified")
                                with chunk_path.open("rb") as chunk:
                                    shutil.copyfileobj(chunk, output)
                                received += length
                                break
                        failure = "PDF 范围、长度或版本校验失败"
                    if attempt == max_retries:
                        raise RuntimeError(f"arXiv PDF 下载失败：{label}；{failure}，当前范围已重试 {max_retries} 次")
                    task_progress(f"PDF 下载中断：{failure}；重试当前分段 {attempt + 1}/{max_retries}…", 27)
                    for _ in range(10 * (2 ** attempt)):
                        task_checkpoint()
                        time.sleep(1)
        task_checkpoint()
        temporary.replace(destination)
        task_progress(f"PDF 已完整下载（curl 直连）：{received / 1048576:.1f} MiB", 39)
    finally:
        for path in (temporary, chunk_path, headers_path):
            path.unlink(missing_ok=True)
