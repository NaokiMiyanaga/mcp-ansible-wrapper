import sys, os, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from mcp_ingest_state import write_sqlite, ensure_schema, _iso_now

def test_write_sqlite_upserts_and_no_unknown():
    # temp DB
    with tempfile.NamedTemporaryFile(suffix=".db") as tf:
        db = tf.name
        # Prepare rows (1 bgp, 1 ospf) + summaries for host r1
        ts = _iso_now()
        bgp_rows = [("r1","10.0.0.1",65001,"Established",0,42,ts,"ansible-mcp")]
        ospf_rows = [("r1","1.1.1.1","eth0","Full","00:38:00","10.0.0.1",ts)]
        summaries = {"r1": ("r1", ts, 1, 1, 1, "ok", "")}
        # Write
        write_sqlite(db, bgp_rows, ospf_rows, summaries, verbose=False, dry_run=False)
        # Check
        con = sqlite3.connect(db)
        cur = con.cursor()
        # tables exist (ensure_schema called inside)
        cur.execute("SELECT COUNT(*) FROM routing_bgp_peer"); assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM routing_ospf_neighbor"); assert cur.fetchone()[0] == 1
        cur.execute("SELECT host, peers_total, peers_established, ospf_neighbors FROM routing_summary"); row = cur.fetchone()
        assert row[0] == "r1" and row[1] == 1 and row[2] == 1 and row[3] == 1
        # unknown rows should be cleaned up
        cur.execute("SELECT COUNT(*) FROM routing_summary WHERE host='unknown'"); assert cur.fetchone()[0] == 0
        con.close()
