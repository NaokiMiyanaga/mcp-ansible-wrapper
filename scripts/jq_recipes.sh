#!/usr/bin/env bash
set -euo pipefail
TOKEN="${MCP_TOKEN:-secret123}"
BASE="${MCP_BASE:-http://127.0.0.1:9000}"

echo "# BGP peers summary per host"
curl -s -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -X POST "${BASE}/tools/call" \
  -d '{"id":"sum","name":"ansible.playbook","arguments":{"playbook":"show_bgp"}}' \
| jq -c '
  .. | .msg? // empty | fromjson? | select(type=="object") as $o
  | ($o.bgp.peers // $o.ipv4Unicast.peers // {} ) as $peers
  | {
      host: ($o.host // "unknown"),
      peers_total: ($peers|length),
      peers_established: (
        [ $peers[]? | (.state // .peerState) | select(.=="Established" or .=="OK") ] | length
      )
    }
'

echo "# OSPF neighbor count per host"
curl -s -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -X POST "${BASE}/tools/call" \
  -d '{"id":"ospf","name":"ansible.playbook","arguments":{"playbook":"show_ospf"}}' \
| jq -c '
  .. | .msg? // empty | fromjson? | select(type=="object")
  | { host: (.host // "unknown"),
      neighbors: ((.ospf.neighbors // .neighbors // []) | length) }
'
