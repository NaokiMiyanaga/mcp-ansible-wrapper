import sys, pathlib

# scripts/ を import path に追加
HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mcp_ingest_state as mod

EXIT_OK = mod.EXIT_OK
EXIT_SCHEMA_MISSING = mod.EXIT_SCHEMA_MISSING
EXIT_NO_JSON = mod.EXIT_NO_JSON

def test_exit_no_json_when_mcp_returns_empty(monkeypatch, tmp_path):
    # preflight を通す
    monkeypatch.setattr(mod, "_check_mcp_health", lambda *a, **k: True)
    # MCP が空を返す → JSON 0 件
    monkeypatch.setattr(mod, "_call_playbook", lambda *a, **k: {})
    db = tmp_path / "test.db"
    rc = mod.main([
        "--db", str(db),
        "--json-log",
        "--dry-run",
    ])
    assert rc == EXIT_NO_JSON

def test_ok_flow_with_minimal_objects_and_dry_run(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_check_mcp_health", lambda *a, **k: True)

    # パーサが理解する最小オブジェクトを返す
    def fake_call(playbook, token, port, verbose=False):
        if "bgp" in playbook:
            return {"msg": '{"host":"r1","bgp":{"peers":{"10.0.0.1":{"state":"Established","remoteAs":65001,"pfxRcd":42}}}}'}
        else:
            return {"msg": '{"host":"r1","ospf":{"neighbors":[{"neighbor_id":"1.1.1.1","iface":"eth0","state":"Full","dead_time_raw":"00:38:00","address":"10.0.0.1"}]}}'}
    monkeypatch.setattr(mod, "_call_playbook", fake_call)

    # DB 書き込みはスキップ（dry-run）しつつ呼び出し内容を検証
    calls = {}
    def fake_write(db_path, bgp_rows, ospf_rows, summaries, **kw):
        calls["bgp_rows"] = len(bgp_rows)
        calls["ospf_rows"] = len(ospf_rows)
        calls["hosts"] = len(summaries)
        return None
    monkeypatch.setattr(mod, "write_sqlite", fake_write)

    db = tmp_path / "ok.db"
    rc = mod.main([
        "--db", str(db),
        "--json-log",
        "--dry-run",
    ])
    assert rc == EXIT_OK
    assert calls.get("bgp_rows") == 1
    assert calls.get("ospf_rows") == 1
    assert calls.get("hosts") == 1

def test_ensure_schema_failure_returns_exit_2(monkeypatch, tmp_path):
    # preflight OK に通す
    monkeypatch.setattr(mod, "_check_mcp_health", lambda *a, **k: True)

    # 空でも存在する .sql を作る（sqlite3 側失敗をシミュレート）
    sql = tmp_path / "bad.sql"
    sql.write_text("CREATE TABLE t(x INT);")

    class Proc:
        def __init__(self, returncode=1, stderr="boom"):
            self.returncode = returncode
            self.stderr = stderr
            self.stdout = ""

    # sqlite3 の実行結果を失敗に見せる
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: Proc(returncode=1, stderr="bad read"))

    db = tmp_path / "fail.db"
    rc = mod.main([
        "--db", str(db),
        "--ensure-schema",
        "--schema-sql", str(sql),
        "--json-log",
        "--dry-run",
    ])
    assert rc == EXIT_SCHEMA_MISSING
