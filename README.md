
# MCP meta/plan 拡張（追加API）

追加されるエンドポイント
- `GET /health` … `{"status":"ok","allow":"...","meta_count":N}`
- `GET /meta` … メタ一覧（id/title/tags/capabilities/path）
- `GET /meta/{id}` … 個別メタ（inputs_schema/例など含む）
- `POST /plan` … 意図から playbook 候補→決定（decision/candidates/vars/validation）
- 既存 `POST /mcp/run` … そのまま

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

# 動作確認
curl -sS http://localhost:9000/health | jq .
curl -sS http://localhost:9000/meta | jq .
curl -sS http://localhost:9000/meta/pb.bgp.neighbor.add@ansible | jq .

# プランニング（OpenAI未設定でも動作。未設定時はスコア上位を選択）
curl -sS -H "Content-Type: application/json"   -d '{"intent":"R1に10.0.0.2のBGPピア(AS65002)追加して"}'   http://localhost:9000/plan | jq .

# 実行（/planの結果を使う例）
curl -sS -H "Authorization: Bearer secret123" -H "Content-Type: application/json"   -d '{"playbook":"playbooks/bgp/add_neighbor.yml","limit":"r1","extra_vars":{"local_asn":65001,"neighbor_ip":"10.0.0.2","neighbor_asn":65002}}'   http://localhost:9000/mcp/run | jq .
```

## 環境変数
- `MCP_ALLOW` = `playbooks/*.yml` 推奨（評価環境）
- `OPENAI_API_KEY` / `OPENAI_MODEL`（任意）
- `RULES_DB`（任意、将来の高度な前段ルータに）

## 備考
- OpenAI未設定でも `/plan` は「メタの軽いスコアリング→Top-1選択」で動きます。
- `inputs_schema` がある場合は `jsonschema` でバリデーション（無ければ best-effort）。
- 既存の `/mcp/run` は互換維持のままです。
