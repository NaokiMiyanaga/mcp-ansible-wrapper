#!/usr/bin/env python3
import argparse
import json
import os
import re
import sqlite3
import sys
from typing import List, Tuple, Optional
import unicodedata
import subprocess
from datetime import datetime, timezone



INTENT_PATTERNS = {
    # ぴあ(ひらがな)/ピア(カタカナ)/ネイバー表記も拾う
    "neighbor": re.compile(r"(peer|neighbor|ネイバ|ネイバー|ピア|ぴあ|bgp)", re.I),
    "link_down": re.compile(r"(down|ダウン|落ち|障害)", re.I),
    "link_up": re.compile(r"(up|アップ|上が|復旧)", re.I),
    "routers": re.compile(r"(router|ルータ|l3)\b", re.I),
    "l2": re.compile(r"(l2|スイッチ|bridge)\b", re.I),
    "vlan": re.compile(r"(vlan|ブイラン|svi)", re.I),
    "route": re.compile(r"(route|ルーティングテーブル|経路)", re.I),
    # change intent (設定/変更/追加/削除/上げる/下げる/落とす など)
    "change": re.compile(r"(設定|変更|変更|変えて|にして|追加|削除|落と|上げ|shutdown|no +shutdown)", re.I),
}


def _normalize_query(q: str) -> str:
    # Unicode正規化 + 小文字化 + よくある専門語彙の正規化
    s = unicodedata.normalize("NFKC", (q or "")).strip().lower()
    # 同義語の簡易正規化（順序重要）
    repl = [
        (r"(ネイバ|ネイバー)", "neighbor"),
        (r"(ぴあ|ピア)", "peer"),
        (r"(ｲﾝﾀｰﾌｪｰｽ|ｲﾝﾀﾌｪｰｽ|インターフェース|インタフェース|interface|if)\b", "interface"),
        (r"l2sw", "l2"),
        (r"l3sw", "router"),
        (r"レイヤー?2", "l2"),
        (r"レイヤー?3", "router"),
        (r"(vlan|ブイラン)", "vlan"),
        (r"(svi|sviアドレス)", "svi"),
        (r"(ルーティングテーブル|経路|route)\b", "route"),
        (r"リンク\s*アップ", "link up"),
        (r"リンク\s*ダウン", "link down"),
        (r"(上がっている|アップしている)", "up"),
        (r"(落ちている|ダウンしている)", "down"),
    ]
    for pat, rep in repl:
        s = re.sub(pat, rep, s)
    return s


def classify_intent(q: str) -> str:
    raw = (q or "").strip()
    qn = _normalize_query(raw)
    # changeを最優先で検出
    if INTENT_PATTERNS["change"].search(qn):
        return "change"
    # リンク系の共起で判定（link + up/down）
    if ("link" in qn or "interface" in qn) and "up" in qn:
        return "link_up"
    if ("link" in qn or "interface" in qn) and ("down" in qn or "障害" in qn):
        return "link_down"
    # 既存パターン
    for name, pat in INTENT_PATTERNS.items():
        if pat.search(qn):
            return name
    return "general"


def fetch_context(cur: sqlite3.Cursor, intent: str, k: int) -> List[Tuple[int, str]]:
    order = "ORDER BY json_extract(json,'$.snapshot_at') DESC, rowid DESC"
    if intent == "neighbor":
        sql = f"SELECT rowid,json FROM docs WHERE type='bgp_neighbor' {order} LIMIT ?"
        rows = cur.execute(sql, (k,)).fetchall()
        if not rows:
            # fallback to summary
            rows = cur.execute(f"SELECT rowid,json FROM docs WHERE type='summary' {order} LIMIT ?", (k,)).fetchall()
        return rows
    if intent == "link_down":
        sql = f"""
            SELECT rowid,json FROM docs
            WHERE type='interface' AND json_extract(json,'$.link')='down'
            {order} LIMIT ?
        """
        rows = cur.execute(sql, (k,)).fetchall()
        if not rows:
            rows = cur.execute(f"SELECT rowid,json FROM docs WHERE type='summary' {order} LIMIT ?", (k,)).fetchall()
        return rows
    if intent == "link_up":
        sql = f"""
            SELECT rowid,json FROM docs
            WHERE type='interface' AND json_extract(json,'$.link')='up'
            {order} LIMIT ?
        """
        rows = cur.execute(sql, (k,)).fetchall()
        if not rows:
            rows = cur.execute(f"SELECT rowid,json FROM docs WHERE type='summary' {order} LIMIT ?", (k,)).fetchall()
        return rows
    if intent == "routers":
        sql = f"SELECT rowid,json FROM docs WHERE type='frr_status' {order} LIMIT ?"
        rows = cur.execute(sql, (k,)).fetchall()
        if not rows:
            # neighbor presence also implies routers
            rows = cur.execute(f"SELECT rowid,json FROM docs WHERE type='bgp_neighbor' {order} LIMIT ?", (k,)).fetchall()
        return rows
    if intent == "l2":
        sql = f"SELECT rowid,json FROM docs WHERE type='bridge_status' {order} LIMIT ?"
        rows = cur.execute(sql, (k,)).fetchall()
        if not rows:
            # Fallback: nodes whose id looks like l2*
            rows = cur.execute(
                f"SELECT rowid,json FROM docs WHERE type='node' AND json_extract(json,'$.node-id') LIKE 'l2%' {order} LIMIT ?",
                (k,)
            ).fetchall()
        return rows
    if intent == "vlan":
        sql = f"""
          SELECT rowid,json FROM docs
          WHERE type='termination-point' AND json_extract(json,'$.network-id') LIKE 'vlan%'
          {order} LIMIT ?
        """
        return cur.execute(sql, (k,)).fetchall()
    if intent == "route":
        sql = f"SELECT rowid,json FROM docs WHERE type='route' {order} LIMIT ?"
        return cur.execute(sql, (k,)).fetchall()
    # general fallback
    sql = f"""
      SELECT rowid,json FROM docs
      WHERE type IN ('summary','bgp_neighbor','interface')
      {order} LIMIT ?
    """
    return cur.execute(sql, (k,)).fetchall()


def build_prompt(question: str, rows: List[Tuple[int, str]], intent: str = "", summary_line: Optional[str] = None) -> str:
    lines = []
    lines.append("あなたはネットワーク運用のアシスタントです。以下の「コンテキスト」だけを根拠に、")
    if intent in ("neighbor", "link_up", "link_down", "vlan", "route"):
        lines.append("日本語で簡潔・正確に回答してください。最初に件数を明記し、その後に該当項目の簡潔な一覧を示してください。推測は避け、根拠となる [n] 番号も必ず併記してください。\n")
    else:
        lines.append("日本語で簡潔・正確に回答してください。推測は避け、根拠となる [n] 番号も必ず併記してください。\n")
    if summary_line:
        lines.append(f"要約: {summary_line}")
    lines.append("コンテキスト:")
    for i, (_, js) in enumerate(rows, 1):
        try:
            obj = json.loads(js)
        except Exception:
            obj = {"json": js}
        typ = obj.get("type", "?")
        # 短い見出し
        head = []
        if typ == "bgp_neighbor":
            head = [f"bgp_neighbor node={obj.get('node-id','')}"]
        elif typ == "interface":
            head = [f"interface node={obj.get('node-id','')} name={obj.get('name','')}"]
        elif typ == "summary":
            head = [f"summary node={obj.get('node-id','')}"]
        elif typ == "frr_status":
            head = [f"frr_status node={obj.get('node-id','')}"]
        elif typ == "bridge_status":
            head = [f"bridge_status node={obj.get('node-id','')}"]
        elif typ == "node":
            head = [f"node node={obj.get('node-id','')}"]
        elif typ == "termination-point":
            head = [f"termination-point node={obj.get('node-id','')} tp={obj.get('tp-id','')}"]
        else:
            head = [typ]
        lines.append(f"[{i}] {' '.join(head)}")
        # 1行のjson（可読のため必要最小限）
        compact = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        lines.append(f"json: {compact}\n")
    lines.append("")
    lines.append(f"質問: {question}")
    lines.append("回答（根拠の [n] を明記）:")
    return "\n".join(lines)


def openai_call(prompt: str, model: str) -> str:
    # Optional: only call if OPENAI_API_KEY is present and user asked
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return "[DRY-RUN] OPENAI_API_KEY が未設定のため、プロンプトのみ出力しました。\n\n" + prompt
    try:
        # new OpenAI client (>=1.0)
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "あなたはネットワーク運用のアシスタントです。根拠[n]を併記し、与えた文脈のみで回答します。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        return f"[ERROR] OpenAI呼び出しに失敗: {e}\n\nプロンプト:\n{prompt}"


def make_summary(cur: sqlite3.Cursor, intent: str) -> Optional[str]:
    try:
        if intent == "neighbor":
            total = cur.execute(
                "SELECT COUNT(*) FROM docs WHERE type='bgp_neighbor'"
            ).fetchone()[0]
            est = cur.execute(
                "SELECT COUNT(*) FROM docs WHERE type='bgp_neighbor' AND json_extract(json,'$.state')='Established'"
            ).fetchone()[0]
            return f"BGPピア 総数={total}, Established={est}, NotEstablished={total - est}"
        if intent == "link_up":
            upc = cur.execute(
                "SELECT COUNT(*) FROM docs WHERE type='interface' AND json_extract(json,'$.link')='up'"
            ).fetchone()[0]
            return f"Link up 件数={upc}"
        if intent == "link_down":
            dc = cur.execute(
                "SELECT COUNT(*) FROM docs WHERE type='interface' AND json_extract(json,'$.link')='down'"
            ).fetchone()[0]
            return f"Link down 件数={dc}"
    except Exception:
        return None
    return None


def ensure_db_path(db_opt: str) -> str:
    db = db_opt
    if not db:
        schema_dir = os.environ.get("IETF_SCHEMA_DIR")
        if schema_dir and os.path.exists(os.path.join(schema_dir, "rag.db")):
            db = os.path.join(schema_dir, "rag.db")
        elif os.path.exists("output/cmdb.sqlite"):
            db = "output/cmdb.sqlite"
        else:
            raise SystemExit("--db を指定してください（例: /path/to/ietf-network-schema/rag.db）")
    return db


def run_cmd(cmd: List[str]) -> int:
    print("$", " ".join(cmd))
    return subprocess.run(cmd).returncode


def parse_change_to_plan(text: str) -> dict:
    r"""Very small heuristics to generate a plan.
    Supports:
      - VLAN(\d+) SVI を <CIDR> に変更 → overlay
      - r(\d+) の router-id を <IP> に変更 → overlay
      - r(\d+) の (service|management) を (down|up)/落として/上げて → command(if.toggle)
    """
    s = unicodedata.normalize("NFKC", text or "").strip()
    vid_m = re.search(r"vlan\s*(\d+)", s, re.I)
    cidrs = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b", s)
    if vid_m and cidrs:
        vid = int(vid_m.group(1))
        cidr = cidrs[-1]
        overlay = {
            "ietf-network:networks": {
                "operational": {
                    "vlans": [ {"vlan-id": vid, "svi": {"address": cidr}} ]
                }
            }
        }
        return {"kind": "overlay", "desc": f"VLAN{vid} SVI={cidr}", "overlay": overlay}

    m = re.search(r"r(\d+)\D+router[- ]?id\D+([0-9]+(?:\.[0-9]+){3})", s, re.I)
    if m:
        node = f"r{m.group(1)}"
        ip = m.group(2)
        overlay = {
            "ietf-network:networks": {
                "operational": {"bgp": {"router_id": { node: ip }}}
            }
        }
        return {"kind": "overlay", "desc": f"{node} router-id={ip}", "overlay": overlay}

    m = re.search(r"r(\d+)\D+(service|management|svc|mgmt)\D+(down|up|落と|上げ)", s, re.I)
    if m:
        host = f"r{m.group(1)}.example"
        plane = m.group(2)
        state = m.group(3)
        plane = "service" if plane.lower() in ("service","svc") else "management"
        state = "down" if state in ("down","落と") else "up"
        return {"kind": "command", "desc": f"if.toggle {host} {plane} {state}",
                "cmd": [
                    "docker","compose","-f","compose.yaml","run","--rm","ansible",
                    "python","scripts/mcp.py","if.toggle","-l", host, "--plane", plane, "--state", state
                ]}

    # unknown interfaces → up
    if re.search(r"unknown.*(interface|インターフェース).*?(up|上げ)", s, re.I):
        return {"kind": "command", "desc": "if.fix-unknown on frr",
                "cmd": [
                    "docker","compose","-f","compose.yaml","run","--rm","ansible",
                    "python","scripts/mcp.py","if.fix-unknown","-l","frr"
                ]}

    return {"kind": "unknown", "desc": "未対応の変更です。具体的な対象と値を含めてください。"}


def apply_plan(plan: dict) -> int:
    if plan.get("kind") == "overlay":
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        fname = f"policy/overlays/nlctl-{ts}.yaml"
        os.makedirs(os.path.dirname(fname), exist_ok=True)
        with open(fname, "w", encoding="utf-8") as f:
            import yaml
            yaml.safe_dump(plan["overlay"], f, sort_keys=False, allow_unicode=True)
        print(f"[plan] overlay saved: {fname}")
        # render
        rc = run_cmd(["docker","compose","-f","compose.yaml","run","--rm","ansible",
                      "python","scripts/mcp.py","policy.render","--overlay", f"/work/{fname}",
                      "--out","/work/output/policies/effective.yaml"]) 
        if rc != 0:
            return rc
        # apply
        rc = run_cmd(["docker","compose","-f","compose.yaml","run","--rm","ansible",
                      "python","scripts/mcp.py","apply","--component","all","--policy","/work/output/policies/effective.yaml"]) 
        return rc
    if plan.get("kind") == "command":
        return run_cmd(plan["cmd"])
    if plan.get("kind") == "commands":
        rc_all = 0
        for c in plan.get("cmds", []):
            rc = run_cmd(c)
            rc_all = rc_all or rc
        return rc_all
    print("[WARN] 適用できるPlanがありません")
    return 1


def repl(args) -> int:
    print("REPLモードです。自然文で質問/変更を入力してください。'!!'で直前のPlanを適用。'exit'で終了。")
    db_path = None
    try:
        db_path = ensure_db_path(args.db)
    except SystemExit:
        pass  # DB未指定でも、変更系のみなら動作させたい

    last_plan = None
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in ("exit","quit",":q"):
            break
        if line == "!!":
            if not last_plan:
                print("[WARN] 直前のPlanがありません")
                continue
            rc = apply_plan(last_plan)
            print(f"[APPLY] rc={rc}")
            last_plan = None
            continue
        if line.startswith("/refresh") or line.startswith("/reflesh"):
            schema_dir = os.environ.get("IETF_SCHEMA_DIR")
            if not schema_dir:
                print("[ERROR] IETF_SCHEMA_DIR が設定されていません。/refresh の前に export IETF_SCHEMA_DIR=/path/to/ietf-network-schema を設定してください。")
                continue
            cmd = ["bash","scripts/publish_ops.sh","--schema-dir", schema_dir, "--db","rag.db","--validate","--debug"]
            rc = run_cmd(cmd)
            print(f"[REFRESH] rc={rc}")
            continue

        intent = classify_intent(line)
        if intent == "change":
            plan = parse_change_to_plan(line)
            # pronoun-based action: if last context was interface list and user says "それを全部up"
            if plan.get("kind") == "unknown" and '全部' in line and ('up' in line.lower() or '上げ' in line):
                # Try to build commands from last fetched context
                try:
                    cmdlist = []
                    for (i, js) in (last_rows or []):
                        obj = json.loads(js)
                        if obj.get('type') != 'interface':
                            continue
                        name = obj.get('name')
                        node = obj.get('node-id')
                        if not name or not node:
                            continue
                        if name == 'lo':
                            continue
                        host = f"{node}.example"
                        cmdlist.append(["docker","compose","-f","compose.yaml","run","--rm","ansible",
                                        "python","scripts/mcp.py","if.toggle","-l", host, "--if-name", name, "--state","up"])
                    if cmdlist:
                        plan = {"kind":"commands","desc":"bring last-listed interfaces up","cmds": cmdlist}
                except Exception:
                    pass
            print(f"[PLAN] kind={plan.get('kind')} desc={plan.get('desc')}")
            if plan.get("kind") == "overlay":
                print(json.dumps(plan["overlay"], ensure_ascii=False, indent=2))
            elif plan.get("kind") == "command":
                print("cmd:", " ".join(plan["cmd"]))
            elif plan.get("kind") == "commands":
                for c in plan["cmds"]:
                    print("cmd:", " ".join(c))
            last_plan = plan
            print("承認して適用するには '!!' を入力してください。")
            continue

        # query path
        if not db_path:
            print("[ERROR] DBが未設定のため、質問は実行できません（--db または IETF_SCHEMA_DIR を設定）")
            continue
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.cursor()
            rows = fetch_context(cur, intent, args.k)
            summary = make_summary(cur, intent)
            prompt = build_prompt(line, rows, intent, summary)
            if args.dry_run:
                print("=== PROMPT (dry-run) ===")
                print(prompt)
            else:
                ans = openai_call(prompt, args.model)
                print(ans)
            last_rows = rows
            last_intent = intent
        finally:
            conn.close()
    return 0


def local_answer(intent: str, rows: List[Tuple[int, str]]) -> str:
    # Build a simple textual answer with counts and [n] references without calling GPT
    def j(i):
        try:
            return json.loads(rows[i][1])
        except Exception:
            return {}
    n = len(rows)
    lines = []
    if intent == "neighbor":
        lines.append(f"合計 {n} 件のBGPピアが見つかりました。")
        for i in range(n):
            o = j(i)
            lines.append(f"- [{i+1}] node={o.get('node-id','?')} peer={o.get('peer','?')} state={o.get('state','?')}")
        return "\n".join(lines)
    if intent in ("link_up","link_down"):
        state = "up" if intent == "link_up" else "down"
        lines.append(f"Link {state} は {n} 件です。")
        for i in range(n):
            o = j(i)
            lines.append(f"- [{i+1}] node={o.get('node-id','?')} iface={o.get('name','?')} link={o.get('link','?')}")
        return "\n".join(lines)
    if intent == "vlan":
        lines.append(f"VLAN/SVI 該当 {n} 件です。")
        for i in range(n):
            o = j(i)
            lines.append(f"- [{i+1}] vlan={o.get('network-id','?')} tp={o.get('tp-id','?')} ipv4={o.get('operational:ipv4','')}")
        return "\n".join(lines)
    if intent == "route":
        lines.append(f"Routes {n} entries.")
        for i in range(min(n, 10)):
            o = j(i)
            lines.append(f"- [{i+1}] {o.get('prefix','?')} via {o.get('nexthop','')} proto={o.get('protocol','')}")
        return "\n".join(lines)
    # fallback
    lines.append(f"該当 {n} 件です。")
    for i in range(n):
        o = j(i)
        typ = o.get('type','?')
        lines.append(f"- [{i+1}] {typ}")
    return "\n".join(lines)


def cmd_query(args) -> int:
    db = args.db
    if not db:
        # 環境変数または推測: ietf-network-schema/rag.db or output/cmdb.sqlite
        schema_dir = os.environ.get("IETF_SCHEMA_DIR")
        if schema_dir and os.path.exists(os.path.join(schema_dir, "rag.db")):
            db = os.path.join(schema_dir, "rag.db")
        elif os.path.exists("output/cmdb.sqlite"):
            db = "output/cmdb.sqlite"
        else:
            print("[ERROR] --db を指定してください（例: /path/to/ietf-network-schema/rag.db）", file=sys.stderr)
            return 2

    def handle_one(cur: sqlite3.Cursor, question: str) -> int:
        intent = classify_intent(question)
        rows = fetch_context(cur, intent, args.k)
        if not rows:
            print("[WARN] コンテキストが見つかりませんでした。", file=sys.stderr)
        summary = make_summary(cur, intent)
        prompt = build_prompt(question, rows, intent, summary)
        engine = getattr(args, 'engine', 'gpt')
        if args.dry_run or engine == 'prompt':
            print("=== PROMPT (dry-run) ===")
            print(prompt)
            return 0
        if engine == 'local':
            print(local_answer(intent, rows))
            return 0
        print(openai_call(prompt, args.model))
        return 0

    conn = sqlite3.connect(db)
    try:
        cur = conn.cursor()
        if getattr(args, 'stdin', False):
            print("質問を入力してください（exit/quit/:q で終了）。")
            while True:
                try:
                    line = input('> ').strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    continue
                if line.lower() in ('exit','quit',':q'):
                    break
                handle_one(cur, line)
            return 0
        # single-shot
        if not args.question:
            print("[ERROR] 質問文がありません。--stdin を付けるか、質問を1行で指定してください。", file=sys.stderr)
            return 2
        return handle_one(cur, args.question)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(prog="nlctl", description="Natural language helper CLI (query/change)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_q = sub.add_parser("query", help="質問に対し、DBから適切な文脈を抽出して回答")
    p_q.add_argument("question", nargs='?', help="日本語の質問文（--stdin 指定時は省略可）")
    p_q.add_argument("--db", help="SQLite DB（例: ~/devNet/ietf-network-schema/rag.db）")
    p_q.add_argument("-k", type=int, default=8, help="コンテキスト件数（デフォルト: 8）")
    p_q.add_argument("--model", default="gpt-4o-mini", help="OpenAIモデル名（デフォルト: gpt-4o-mini）")
    p_q.add_argument("--dry-run", action="store_true", help="GPT呼び出しを行わず、プロンプトのみ出力")
    p_q.add_argument("--stdin", action="store_true", help="標準入力から複数の質問を対話的に処理（exit/quit/:q で終了）")
    p_q.add_argument("--engine", choices=["gpt","local","prompt"], default="gpt", help="回答エンジン: gpt(既定)/local/prompt")
    p_q.set_defaults(func=cmd_query)

    # repl
    p_r = sub.add_parser("repl", help="対話モード：質問/変更を自動判定。'!!'で直前のPlanを適用。exitで終了。")
    p_r.add_argument("--db", help="SQLite DB（例: ~/devNet/ietf-network-schema/rag.db）")
    p_r.add_argument("-k", type=int, default=8, help="コンテキスト件数（デフォルト: 8）")
    p_r.add_argument("--model", default="gpt-4o-mini", help="OpenAIモデル名（デフォルト: gpt-4o-mini）")
    p_r.add_argument("--engine", choices=["gpt","local","prompt"], default="gpt", help="回答エンジン: gpt(既定)/local/prompt")
    p_r.add_argument("--dry-run", action="store_true", help="GPT呼び出しを行わず、プロンプトのみ出力（engine=promptと同等）")
    p_r.set_defaults(func=lambda args: repl(args))

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
