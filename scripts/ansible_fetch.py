#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ansible_fetch.py
-------------------
Ansible-MCP APIからBGP/OSPFなどのネットワーク情報を取得し、JSONとして出力する専用スクリプト。
"""
import os
import json
import urllib.request
import argparse




def fetch_ansible(mcp_base, token, playbook):
    # インベントリ情報取得
    payload_inv = {
        "id": "fetch-inventory",
        "name": "ansible.inventory",
        "arguments": {}
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = mcp_base.rstrip("/") + "/tools/call"
    req_inv = urllib.request.Request(url, data=json.dumps(payload_inv).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req_inv, timeout=60) as resp_inv:
        inventory = json.loads(resp_inv.read().decode("utf-8"))

    # 状態情報取得
    payload_state = {
        "id": f"fetch-{playbook}",
        "name": "ansible.playbook",
        "arguments": {"playbook": playbook}
    }
    req_state = urllib.request.Request(url, data=json.dumps(payload_state).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req_state, timeout=60) as resp_state:
        state = json.loads(resp_state.read().decode("utf-8"))

    return {"inventory": inventory, "state": state}
if __name__ == "__main__":
    main()
