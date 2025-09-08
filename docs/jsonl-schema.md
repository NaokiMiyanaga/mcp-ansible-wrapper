# JSONL Schema (objects.jsonl)

このファイルは、MCPが出力する JSON Lines（NDJSON）`output/objects.jsonl` のレコード仕様を記述します。IETF JSON（RFC8345互換）の“補助表現”であり、検索・集計・RAG向けに最適化しています。正規の契約は IETF JSON 側です。

共通メタデータ（任意だが推奨）
- `snapshot_at`: UTC時刻（例: `20250906T154514Z`）
- `generator`: 生成元（`mcp-ansible-wrapper`）
- `schema_version`: スキーマの簡易バージョン（例: `1`）

レコードタイプ（type）
- `network`: { network-id }
- `node`: { network-id, node-id }
- `termination-point`: { network-id, node-id, tp-id, operational:* }
- `frr_status`: { node-id, version, bgp_summary, interfaces_brief }
- `bridge_status`: { node-id, bridge_link, bridge_detail }
- `bgp_neighbor`: { node-id, peer, remote-as, state, uptime, pfxRcd }
- `interface`: { node-id, name, plane, ipv4, link, proto }
- `summary`: { node-id, peers_total, peers_established, peers_not_established, if_link_up_proto_down }

注意
- `network-id`/`node-id`/`tp-id` などのキーはハイフン区切り（loadJSONL.py が参照）
- `termination-point` の `operational:*` は IETFモデルの拡張名前空間として位置付けています
- `summary` はQA/監視を容易にするための便宜的な集約レコードです（真のソースは個別行）

