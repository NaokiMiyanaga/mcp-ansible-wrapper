import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from mcp_ingest_state import parse_ospf_objects, _iso_now

def test_ospf_parse_minimal_neighbors_list():
    objs = [{
        "host": "r1",
        "ospf": {
            "neighbors": [
                {"neighbor_id": "1.1.1.1", "iface": "eth0", "state": "Full", "dead_time_raw": "00:38:00", "address": "10.0.0.1"}
            ]
        }
    }]
    rows, summary = parse_ospf_objects(objs, _iso_now(), strict=False, aliases={
        "bgp_peer": {}, "ospf_neighbor": {}
    })
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "r1" and r[1] == "1.1.1.1" and r[2] == "eth0" and r[3] == "Full"
    assert summary["r1"][4] == 1  # ospf_neighbors

def test_ospf_parse_empty_neighbors_is_ok_and_counts_zero():
    objs = [{"host": "r2", "ospf": {"neighbors": []}}]
    rows, summary = parse_ospf_objects(objs, _iso_now(), strict=False, aliases={"bgp_peer": {}, "ospf_neighbor": {}})
    assert rows == []
    assert summary["r2"][4] == 0
