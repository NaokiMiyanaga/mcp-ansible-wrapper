Apply these files to your mcp-ansible-wrapper project:

1) Replace `docker-compose.yml` (MCP_ALLOW wildcard enabled)
2) Add/replace `mcp_http.py` (adds /health and glob allow-list)
3) Replace `playbooks/network_overview.yml` (natural language output)

Rebuild & test:
  docker compose down && docker compose up -d --build
  curl -sS http://localhost:9000/health | jq .
  curl -sS -H "Authorization: Bearer secret123" -H "Content-Type: application/json"     -d '{"playbook":"playbooks/network_overview.yml","limit":"all"}'     http://localhost:9000/mcp/run | jq -r '.stdout'
