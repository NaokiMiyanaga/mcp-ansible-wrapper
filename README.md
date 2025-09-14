
# MCP meta/plan 拡張（追加API）

追加されるエンドポイント
- `GET /health` … 健全性
- `GET /tools/list` … ツールカタログ（id/title/tags/inputs_schema/examples）
- `GET /tools/tags` … タグのタクソノミ一覧
- 既存 `POST /mcp/run` … そのまま（将来 `/run` に統一）

## 置き方
```
mcp-ansible-wrapper/
  playbooks/
    bgp/add_neighbor.yml
    ...
    meta/
      r1_bgp_neighbor.yml   # ← あなたの定義
  mcp_http.py               # ← 本ファイルに差し替え
  requirements.txt          # ← 追加依存
  Dockerfile.mcp            # ← 参考（既存に合わせて調整）
```

## build/run
```bash
docker compose down
docker compose up -d --build

## RAG Overview
- Knowledge: `knowledge/playbook_map.yaml` (feature→prefer/fallback)
- Logs: 6/7/8/9/10/11 (-1 for health)
- Honors Chainlit `payload.candidates[0]` (if provided) as tentative playbook
- `intent=propose_create`: returns `debug.propose_new_playbook` with {feature,suggested_path,vars_suggest,template_hint}

### Feature→Playbook (defaults)
- inventory: network_overview.yml
- bgp: show_bgp.yml / show_bgp_deep.yml
- ospf: show_ospf.yml
- interface: if_addr.yml → host_ip.yml (fallback)
- vlan: bridge_summary.yml → bridge_check.yml (fallback)
- isis: show_isis.yml → frr_check.yml (fallback)
- snmp: show_snmp.yml → frr_check.yml (fallback)
- logs: ops_export.yml → router_summary.yml (fallback)
- ospf_deep: show_ospf_deep.yml → frr_check.yml (fallback)

## Dev/Prod
- Dev: mount `./knowledge:/app/knowledge:ro`, `./playbooks:/app/playbooks:ro` for instant reflection
- Prod: bake knowledge/playbooks into the image

# 動作確認
curl -sS http://localhost:9000/health | jq .
curl -sS http://localhost:9000/tools/list | jq .
curl -sS http://localhost:9000/tools/tags | jq .

# 実行
curl -sS -H "Authorization: Bearer secret123" -H "Content-Type: application/json"   -d '{"playbook":"playbooks/bgp/add_neighbor.yml","limit":"r1","extra_vars":{"local_asn":65001,"neighbor_ip":"10.0.0.2","neighbor_asn":65002}}'   http://localhost:9000/mcp/run | jq .
```

## 環境変数
- `MCP_ALLOW` = `playbooks/*.yml` 推奨（評価環境）
- `OPENAI_API_KEY` / `OPENAI_MODEL`（任意）
- `RULES_DB`（任意、将来の高度な前段ルータに）
- `MCP_TOOLS_ENUM_MODE`（既定=auto）: `embed|hint|auto`。`/tools/list` の `host` に enum を埋め込むかの制御。
- `MCP_TOOLS_ENUM_TTL`（既定=60秒）: routers_list の再探索TTL。
 - `MCP_TOOLS_ENUM_FALLBACK`（任意）: 非execモード時に enum として埋め込む候補（例: `r1,r2`）。未設定なら埋め込みなし。

## Tools catalog の検証（任意）
```
pip install jsonschema
python scripts/validate_tools_list.py --url http://localhost:9000/tools/list
```

## 備考
- OpenAI未設定でも `/plan` は「メタの軽いスコアリング→Top-1選択」で動きます。
- `inputs_schema` がある場合は `jsonschema` でバリデーション（無ければ best-effort）。
- 既存の `/mcp/run` は互換維持のままです。
