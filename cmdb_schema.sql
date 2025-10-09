CREATE TABLE IF NOT EXISTS routing_bgp_peer(
  host TEXT, peer_ip TEXT, peer_as INTEGER, state TEXT, uptime_sec INTEGER,
  prefixes_received INTEGER, collected_at TEXT, source TEXT,
  PRIMARY KEY(host,peer_ip,collected_at)
);
CREATE TABLE IF NOT EXISTS routing_ospf_neighbor(
  host TEXT, neighbor_id TEXT, iface TEXT, state TEXT, dead_time_raw TEXT,
  address TEXT, collected_at TEXT,
  PRIMARY KEY(host,neighbor_id,collected_at)
);
CREATE TABLE IF NOT EXISTS routing_summary(
  host TEXT PRIMARY KEY,
  last_collected_at TEXT,
  peers_total INTEGER DEFAULT 0,
  peers_established INTEGER DEFAULT 0,
  ospf_neighbors INTEGER DEFAULT 0,
  status TEXT,
  last_error TEXT
);
