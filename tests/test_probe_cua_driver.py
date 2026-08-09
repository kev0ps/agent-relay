from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "probe_cua_driver.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_cua_driver", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_probe_uses_exact_initialize_and_tools_list_requests() -> None:
    probe = _load_probe()

    assert probe.INITIALIZE["id"] == 1
    assert probe.INITIALIZE["method"] == "initialize"
    assert probe.INITIALIZED["method"] == "notifications/initialized"
    assert probe.TOOLS_LIST == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }


def test_probe_accepts_only_json_rpc_result_messages() -> None:
    probe = _load_probe()

    assert probe._result_message(b'{"jsonrpc":"2.0","id":1,"result":{}}\n') == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {},
    }
    assert probe._result_message(b'{"jsonrpc":"2.0","id":1,"error":{}}\n') is None
    assert probe._result_message(b'{"jsonrpc":"2.0","result":{}}\n') is None
    assert probe._result_message(
        b'{"jsonrpc":"2.0","result":{}}\n', require_id=False
    ) == {"jsonrpc": "2.0", "result": {}}
    assert probe._result_message(b"not-json\n") is None
