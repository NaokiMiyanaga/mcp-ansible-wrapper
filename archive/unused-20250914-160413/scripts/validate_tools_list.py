#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, json, argparse, urllib.request

def load_url(url: str, token: str | None = None, timeout: float = 10.0) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))

def load_file(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def main():
    ap = argparse.ArgumentParser(description="Validate MCP /tools/list against JSON Schema")
    ap.add_argument("--url", help="URL to GET /tools/list (e.g., http://localhost:9000/tools/list)")
    ap.add_argument("--file", help="Local JSON file with /tools/list response")
    ap.add_argument("--schema", default="docs/schema/tools-list.schema.json", help="Path to JSON Schema")
    ap.add_argument("--token", default=None, help="Bearer token")
    args = ap.parse_args()
    if not args.url and not args.file:
        print("Either --url or --file is required", file=sys.stderr)
        sys.exit(2)

    try:
        import jsonschema  # type: ignore
    except Exception:
        print("jsonschema package not available. Install with: pip install jsonschema", file=sys.stderr)
        sys.exit(2)

    # Load target JSON
    obj = load_url(args.url, token=args.token) if args.url else load_file(args.file)
    # Load schema
    schema = load_file(args.schema)
    # Validate
    try:
        jsonschema.validate(instance=obj, schema=schema)
        print("OK: /tools/list is valid")
    except jsonschema.ValidationError as e:  # type: ignore
        print(f"Invalid: {e.message}")
        sys.exit(2)

if __name__ == '__main__':
    main()

