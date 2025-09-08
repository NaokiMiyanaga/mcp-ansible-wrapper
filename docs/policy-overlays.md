# ポリシー・オーバーレイ運用ガイド

- 目的: ベースのSoT（`policy/master.ietf.yaml`）に対して、変更点だけをオーバーレイYAMLとして管理し、合成した“有効ポリシー”を生成します。
- 原則: オーバーレイは `ietf-network:networks.operational` 配下の変更に限定するのが安全です（`network[]` リストのマージは非対応）。

## ディレクトリ構成（例）

- `policy/master.ietf.yaml` … ベースSoT
- `policy/overlays/*.yaml` … 差分（小さく分割、用途ごと）
- `output/policies/effective.yaml` … マージ結果

## サンプル

- BGP Router-ID の変更（r1のみ）
  - `policy/overlays/demo-bgp-routerid.yaml`
- VLAN10 の SVIアドレスを `.254/24` に変更
  - `policy/overlays/demo-vlan10-svi.yaml`

## 使い方

- マージして有効ポリシーを生成

```bash
# 1つのオーバーレイ
docker compose -f mcp-ansible-wrapper/compose.yaml run --rm ansible \
  python scripts/mcp.py policy.render \
  --overlay /work/policy/overlays/demo-bgp-routerid.yaml \
  --out /work/output/policies/effective.yaml

# 複数オーバーレイ（順に適用）
docker compose -f mcp-ansible-wrapper/compose.yaml run --rm ansible \
  python scripts/mcp.py policy.render \
  --overlay /work/policy/overlays/demo-bgp-routerid.yaml \
  --overlay /work/policy/overlays/demo-vlan10-svi.yaml \
  --out /work/output/policies/effective.yaml
```

- 適用

```bash
# 有効ポリシーを使ってFRR/Bridgeを適用
docker compose -f mcp-ansible-wrapper/compose.yaml run --rm ansible \
  python scripts/mcp.py apply --component all --policy /work/output/policies/effective.yaml
```

## マージ仕様

- deep-merge: dictは再帰的にマージ、スカラ/リストは置換
- 特別扱い: `operational.vlans` は `vlan-id` で突き合わせて要素をマージ（既存は更新、なければ追加）
- 非推奨: `network[]` 配下のリスト要素の差し替えは未対応のため、ここを触るオーバーレイは避けてください

## 戻す（ロールバック）

- オーバーレイの適用順を変える、あるいは“元に戻す値”を書いた別オーバーレイを適用して再度 `policy.render` してください。
- 変更履歴を残したい場合は、`policy/overlays/YYYYMMDD-*` のように日付で管理するのがおすすめです。

---

将来的に JSON Patch (RFC6902) 対応や `policy.diff` の可視化も追加可能です。今は運用で多い `operational` 配下の更新にフォーカスした軽量な仕組みにしてあります。

