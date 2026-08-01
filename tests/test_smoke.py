"""Runtime smoke test (app/agent/smoke.py + AgentCore._smoke_test_backend).

Offline: these start real, tiny local servers as subprocesses (no Ollama). Each
binds a port chosen to be free at test time, gets probed, and is killed. Short
timeouts keep the suite fast.
"""

import socket
import textwrap
from pathlib import Path

import pytest

from app.agent.blueprint import (ApiContract, Blueprint, Endpoint, PlannedFile)
from app.agent.core import AgentCore
from app.agent.smoke import SmokeResult, detect_ports, run_smoke_test
from config.settings import settings


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_good_server(dirpath: Path, port: int, name: str = "server.py") -> Path:
    src = textwrap.dedent(
        f"""
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')
            def log_message(self, *a):
                pass
        HTTPServer(('127.0.0.1', {port}), H).serve_forever()
        """
    ).strip()
    p = dirpath / name
    p.write_text(src, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# detect_ports — pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src,expected_first",
    [
        ("HTTPServer(('', 8000), H).serve_forever()", 8000),
        ("app.run(host='0.0.0.0', port=5000)", 5000),
        ("server.listen(3000)", 3000),
        ("PORT = 8081\n", 8081),
        ("uvicorn.run(app, port=9000)", 9000),
    ],
)
def test_detect_ports_finds_declared(src, expected_first):
    assert detect_ports(src)[0] == expected_first


def test_detect_ports_appends_common_fallbacks():
    ports = detect_ports("no port here")
    assert 8000 in ports and 5000 in ports


# ---------------------------------------------------------------------------
# run_smoke_test — real subprocesses
# ---------------------------------------------------------------------------


def test_smoke_good_server_responds(tmp_path):
    port = _free_port()
    server = _write_good_server(tmp_path, port)
    res = run_smoke_test(server, tmp_path, endpoint_paths=["/"], timeout=6, warmup=1.0)
    assert res.started is True
    assert res.responded is True
    assert res.status == 200
    assert res.port == port


def test_smoke_broken_server_reports_crash(tmp_path):
    (tmp_path / "server.py").write_text(
        "import definitely_not_a_real_module_xyz\n", encoding="utf-8"
    )
    res = run_smoke_test(
        tmp_path / "server.py", tmp_path, timeout=6, warmup=1.5
    )
    assert res.started is False
    assert "ModuleNotFoundError" in res.stderr or "No module" in res.stderr
    assert "crashed on startup" in res.detail
    assert res.note().startswith("may not meet:")


def test_smoke_alive_but_no_http(tmp_path):
    (tmp_path / "server.py").write_text(
        "import time\ntime.sleep(30)\n", encoding="utf-8"
    )
    res = run_smoke_test(tmp_path / "server.py", tmp_path, timeout=3, warmup=1.0)
    assert res.started is True
    assert res.responded is False


def test_smoke_unknown_runtime(tmp_path):
    (tmp_path / "server.rb").write_text("puts 'hi'", encoding="utf-8")
    res = run_smoke_test(tmp_path / "server.rb", tmp_path)
    assert res.started is False
    assert "cannot run" in res.detail


def test_smoke_result_note_phrasing():
    up = SmokeResult(True, True, 200, 8000, "server.py started; GET / -> 200 on :8000")
    assert up.note().startswith("Smoke test:")
    down = SmokeResult(False, False, None, None, "server.py crashed on startup (X)")
    assert down.note().startswith("may not meet:")


# ---------------------------------------------------------------------------
# AgentCore integration — pick the entry, run it, report
# ---------------------------------------------------------------------------


def test_pick_backend_entry_prefers_server_over_frontend(tmp_path):
    a = AgentCore(session_id="pytest_pick")
    port = _free_port()
    _write_good_server(tmp_path, port)
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    bp = Blueprint(
        files=(
            PlannedFile("index.html", "create", "", "frontend"),
            PlannedFile("server.py", "create", "", "backend"),
        ),
    )
    entry = a._pick_backend_entry(bp, tmp_path)
    assert entry is not None and entry.name == "server.py"


def test_pick_backend_entry_none_for_static(tmp_path):
    a = AgentCore(session_id="pytest_pick_none")
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    bp = Blueprint(files=(PlannedFile("index.html", "create", "", "frontend"),))
    assert a._pick_backend_entry(bp, tmp_path) is None


async def test_smoke_test_backend_good(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "smoke_test_timeout", 6.0)
    a = AgentCore(session_id="pytest_smoke_good")
    port = _free_port()
    _write_good_server(tmp_path, port)
    bp = Blueprint(
        files=(PlannedFile("server.py", "create", "", "backend"),),
        contract=ApiContract(endpoints=(Endpoint("GET", "/"),)),
    )
    note, trace = await a._smoke_test_backend(bp)
    assert "started" in note
    assert "200" in note


async def test_smoke_test_backend_broken_no_repair(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "smoke_test_timeout", 6.0)
    monkeypatch.setattr(settings, "max_smoke_repairs", 0)  # no LLM repair in test
    a = AgentCore(session_id="pytest_smoke_broken")
    (tmp_path / "server.py").write_text("import nope_xyz_missing\n", encoding="utf-8")
    bp = Blueprint(files=(PlannedFile("server.py", "create", "", "backend"),))
    note, trace = await a._smoke_test_backend(bp)
    assert note.strip().startswith("may not meet:")
    assert trace == []  # no repair attempted
