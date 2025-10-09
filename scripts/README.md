# MCP → CMDB State Update (BGP/OSPF)

このパッケージは、Ansible-MCP の `/tools/call` から BGP/OSPF の状態を取得し、
SQLite の CMDB に `routing_bgp_peer / routing_ospf_neighbor / routing_summary` を更新します。

## 同梱物
- `scripts/mcp_ingest_state.py` … 取得・解析・書き込み本体（Python 3.9+）
- `scripts/mcp_ingest_state.sh` … 便利シェル（環境変数で設定）
- `scripts/jq_recipes.sh` … 動作確認用の curl + jq ワンライナー

## 使い方
1. **DB スキーマを用意**（未作成なら）  
   既存の `rag.db` に以下のテーブルがなければ自動作成します:
   - routing_bgp_peer
   - routing_ospf_neighbor
   - routing_summary

2. **環境変数**
```bash
export MCP_TOKEN=secret123
export MCP_BASE=http://127.0.0.1:9000
export DB=/path/to/rag.db
```

3. **取り込み**
```bash
./scripts/mcp_ingest_state.sh
# もしくは
python3 scripts/mcp_ingest_state.py --db "$DB" --token "$MCP_TOKEN" --mcp-base "$MCP_BASE" --verbose
```

4. **検証**
```bash
sqlite3 "$DB" "SELECT host, COUNT(*) FROM routing_bgp_peer GROUP BY host;"
sqlite3 "$DB" "SELECT * FROM routing_summary ORDER BY host;"
```

## 備考
- MCP 側の playbook 名は `show_bgp` / `show_ospf` を前提（拡張子不要）
- JSON 抽出は `.result.msg` と `.result.ansible.stdout` のどちらにも対応
- Python の再帰正規表現に依存せず、波括弧バランサで JSON ブロックを抽出します
