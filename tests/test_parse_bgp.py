import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from mcp_ingest_state import parse_bgp_objects, _iso_now

def test_bgp_parse_minimal_peer_dict():
    objs = [{
        "host": "r1",
        "bgp": {
            "peers": {
                "10.0.0.1": {
                    "state": "Established",
                    "remoteAs": 65001,
                    "pfxRcd": 42
                }
            }
        }
    }]
    collected_at = _iso_now()
    rows, summary = parse_bgp_objects(objs, collected_at, strict=False, aliases={
        "bgp_peer": {"peer_ip": ["peer_ip","peerIp","neighbor","id"], "state": ["state","peerState","sessionState"], "remoteAs": ["remoteAs","asn","remote_as"], "pfxRcd": ["pfxRcd","prefixes_received","prefixReceived"]},
        "ospf_neighbor": {}
    })
    assert len(rows) == 1
    assert ("r1", "10.0.0.1", 65001, "Established", 0, 42, collected_at, "ansible-mcp") in rows
    assert "r1" in summary
    # peers_total=1, peers_established=1
    assert summary["r1"][2] == 1
    assert summary["r1"][3] == 1

def test_bgp_parse_empty_peers_is_ok_and_counts_zero():
    objs = [{"host": "r2", "bgp": {"peers": {}}}]
    rows, summary = parse_bgp_objects(objs, _iso_now(), strict=False, aliases={"bgp_peer": {}, "ospf_neighbor": {}})
    assert rows == []
    assert summary["r2"][2] == 0
    assert summary["r2"][3] == 0
