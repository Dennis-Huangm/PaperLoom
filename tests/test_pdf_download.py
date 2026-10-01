from pathlib import Path

import pytest

from arxiv_ra.pdf_download import download_with_curl
from arxiv_ra.task_runtime import TaskCancelled, TaskHooks, bind_task_hooks


@pytest.mark.parametrize("bad_response", [None, "offset", "length", "modified", "html", "retry"])
def test_curl_validates_ranges_and_preserves_destination(tmp_path, monkeypatch, bad_response):
    from contextlib import nullcontext
    monkeypatch.setattr("arxiv_ra.pdf_download.shared_rate_limit", lambda *_, **kwargs: nullcontext())
    monkeypatch.setattr("arxiv_ra.pdf_download.time.sleep", lambda _: None)
    payload = b"%PDF-1.7\n" + b"x" * (1024 * 1024 + 16384)
    destination = tmp_path / "paper.pdf"
    destination.write_bytes(b"existing complete PDF")
    calls = []

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            calls.append(command)
            assert command[1] == "--disable"
            assert command[command.index("--noproxy") + 1] == "*"
            start, end = map(int, command[command.index("--range") + 1].split("-"))
            if bad_response == "retry" and len(calls) == 2:
                self.returncode = 22
                return
            end = min(end, len(payload) - 1)
            body = payload[start:end + 1]
            modified = "Thu, 19 Mar 2026 00:24:48 GMT"
            range_start = start
            if start:
                if bad_response == "retry" and len(calls) == 3:
                    assert not any(item.startswith("If-Range:") for item in command)
                else:
                    assert command[command.index("--header", command.index("--header") + 1) + 1] == f"If-Range: {modified}"
                if bad_response == "offset":
                    range_start += 1
                elif bad_response == "length":
                    body = body[:-1]
                elif bad_response == "modified":
                    modified = "Fri, 20 Mar 2026 00:24:48 GMT"
            if bad_response == "html":
                body = b"<html>" + body[6:]
            Path(command[command.index("--output") + 1]).write_bytes(body)
            Path(command[command.index("--dump-header") + 1]).write_text(
                f"HTTP/1.1 206 Partial Content\nContent-Range: bytes {range_start}-{end}/{len(payload)}\n"
                f"Last-Modified: {modified}\n\n", encoding="ascii")

        def poll(self):
            return self.returncode

    monkeypatch.setattr("arxiv_ra.pdf_download.subprocess.Popen", Process)
    if bad_response and bad_response != "retry":
        with pytest.raises(RuntimeError, match="校验失败"):
            download_with_curl("https://arxiv.org/pdf/testv1", destination, "testv1", 0)
        assert destination.read_bytes() == b"existing complete PDF"
    else:
        download_with_curl("https://arxiv.org/pdf/testv1", destination, "testv1", 1)
        assert destination.read_bytes() == payload
        assert len(calls) == (3 if bad_response == "retry" else 2)
        if bad_response == "retry":
            assert calls[1][calls[1].index("--range") + 1] == calls[2][calls[2].index("--range") + 1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["paper.pdf"]


def test_curl_cancellation_terminates_process_and_cleans_files(tmp_path, monkeypatch):
    processes = []
    cancelled = [False]

    class Process:
        returncode = None
        terminated = False

        def __init__(self, command, **kwargs):
            processes.append(self)
            cancelled[0] = True

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 1

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr("arxiv_ra.pdf_download.subprocess.Popen", Process)
    hooks = TaskHooks(progress=lambda *_: None, warning=lambda *_: None, is_cancelled=lambda: cancelled[0])
    with bind_task_hooks(hooks), pytest.raises(TaskCancelled):
        download_with_curl("https://arxiv.org/pdf/testv1", tmp_path / "paper.pdf", "testv1", 0)
    assert processes[0].terminated
    assert list(tmp_path.iterdir()) == []


def test_arxiv_routes_only_pdf_to_optional_backend(tmp_path, monkeypatch):
    from arxiv_ra.arxiv_client import ArxivClient
    calls = []
    monkeypatch.setenv("ARXIV_PDF_BACKEND", "curl-direct")
    monkeypatch.setattr("arxiv_ra.pdf_download.download_with_curl", lambda *args: calls.append(args))
    client = ArxivClient(max_retries=2)
    try:
        client._download_pdf("https://arxiv.org/pdf/testv1", tmp_path / "paper.pdf", "testv1")
        assert calls == [("https://arxiv.org/pdf/testv1", tmp_path / "paper.pdf", "testv1", 2)]
        assert client.client is not None
    finally:
        client.client.close()
