# BGP Deep Check ガイド (FRR / IOS)

## 目的
- BGPの**neighbor状態**、**Downピアの列挙**、AFI/SAFIごとの**経路受信/採用/インストール**状況を取得。

## 使うプレイブック
- 通常: `playbooks/show_bgp_deep.yml`
- 簡易: `playbooks/show_bgp.yml`（サマリのみ。neighbor/経路詳細を要する質問では非推奨）

## 代表質問と推奨プレイブック
- 「r2のBGPのneighborと受信経路の詳細見せて」→ **show_bgp_deep.yml**
- 「r1のBGPざっくり状態」→ show_bgp.yml（必要に応じて deep を提示）

## 実装メモ（FRR）
- `vtysh -c "show bgp summary json"` / `vtysh -c "show bgp ipv4 unicast summary json"` でJSONを取得して解析。
- Downピアの一覧化、AFIごとの `acceptedPaths/receivedPaths/installedPaths` を抜く。

## 実装メモ（IOS）
- `ios_command: show ip bgp summary`（TextFSM化すると精度UP）。
