#!/usr/bin/env python3
"""
rewrite_mgmt_wrapper.py - safe mgmt-subnet rewriter for mcp-ansible-wrapper

Usage examples (run at repo root):
  # Dry-run (see what would change)
  python3 scripts/rewrite_mgmt_wrapper.py --root .

  # Apply changes
  python3 scripts/rewrite_mgmt_wrapper.py --root . --apply

  # Custom mapping
  python3 scripts/rewrite_mgmt_wrapper.py --root . \
    --old-net 192.168.0.0/24 --new-net 172.30.0.0/24 \
    --old-base 192.168.0 --new-base 172.30.0 \
    --hosts 1,2,11,12,101 --apply

  # Rewrite ALL 192.168.0.X -> 172.30.0.X (dangerous, but useful)
  python3 scripts/rewrite_mgmt_wrapper.py --root . --all-hosts --apply
"""
import argparse, pathlib, re, shutil, sys, csv

SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', '.idea', 'dist', 'build', 'output', 'logs'}
TEXT_EXTS = {'.txt','.md','.yaml','.yml','.cfg','.conf','.ini','.py','.sh','.bash','.zsh','.json','.toml','.jinja','.j2'}

def is_text_file(p: pathlib.Path) -> bool:
    if p.suffix.lower() in TEXT_EXTS: return True
    try:
        b = p.read_bytes()[:4096]
        return b'\x00' not in b
    except Exception: return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='.', help='directory to scan (repo root)')
    ap.add_argument('--old-net', default='192.168.0.0/24')
    ap.add_argument('--new-net', default='172.30.0.0/24')
    ap.add_argument('--old-base', default='192.168.0')
    ap.add_argument('--new-base', default='172.30.0')
    ap.add_argument('--hosts', default='1,2,11,12,101', help='last octets to rewrite; ignored when --all-hosts is set')
    ap.add_argument('--all-hosts', action='store_true', help='rewrite ALL 192.168.0.X -> 172.30.0.X (use with care)')
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry-run)')
    ap.add_argument('--report', default='rewrite_mgmt_report.csv', help='CSV report filename')
    args = ap.parse_args()

    root = pathlib.Path(args.root).resolve()
    if not root.exists():
        print(f"[ERR] root not found: {root}", file=sys.stderr); sys.exit(2)

    subnet_re = re.compile(re.escape(args.old_net))
    if args.all_hosts:
        host_re = re.compile(rf"\b{re.escape(args.old_base)}\.(\d+)\b")
        def repl_host(m): return f"{args.new_base}.{m.group(1)}"
        host_rules = [(host_re, repl_host)]
    else:
        hosts = [h.strip() for h in args.hosts.split(',') if h.strip()]
        host_rules = [(re.compile(rf"\b{re.escape(args.old_base)}\.{re.escape(h)}\b"), f"{args.new_base}.{h}") for h in hosts]

    planned = []
    report_rows = []

    for p in root.rglob('*'):
        if p.is_dir():
            name = p.name
            if name in SKIP_DIRS or name.startswith('.'): 
                continue
            continue
        parts = set(p.parts)
        if parts & SKIP_DIRS: 
            continue
        if not is_text_file(p): 
            continue

        try:
            text = p.read_text(errors='ignore')
        except Exception:
            continue

        found = False
        new = subnet_re.sub(args.new_net, text)
        if new != text: found = True
        for rx, rep in host_rules:
            new2 = rx.sub(rep, new)
            if new2 != new:
                found = True
                new = new2

        if found:
            planned.append(p)
            before_lines = text.count('\n')+1
            after_lines  = new.count('\n')+1
            report_rows.append([str(p.relative_to(root)), before_lines, after_lines])
            if args.apply:
                bak = p.with_suffix(p.suffix + '.bak')
                if not bak.exists():
                    try: shutil.copy2(p, bak)
                    except Exception: shutil.copy(p, bak)
                p.write_text(new)

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    print(f"[{mode}] root={root}")
    print(f"[{mode}] subnet: {args.old_net} -> {args.new_net}")
    if args.all_hosts:
        print(f"[{mode}] hosts : {args.old_base}.X -> {args.new_base}.X (ALL)")
    else:
        # summarize explicit host set
        print(f"[{mode}] hosts : explicit set -> {args.new_base}.{{{','.join([r.pattern.split('.')[-1][:-2] if hasattr(r, 'pattern') else '?' for r,_ in host_rules])}}}")
    print(f"[{mode}] files to change: {len(planned)}")

    report_path = root / args.report
    with report_path.open('w', newline='') as f:
        w = csv.writer(f); w.writerow(['file','before_lines','after_lines'])
        w.writerows(report_rows)
    print(f"[{mode}] report written: {report_path}")

if __name__ == '__main__':
    main()
