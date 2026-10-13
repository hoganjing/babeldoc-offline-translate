"""合并所有 block_NNN.translations.json 为单一 translations.json ({原文: 译文})。

用法:
  python merge_translations.py --blocks-dir blocks --out translations.json

- 遍历 blocks/block_*.translations.json, 合并成一个大字典。
- 若同一原文出现不同译文, 报告冲突 (取最后写入的值, 但打印警告供人工核对)。
- 输出可直接传给 run_babeldoc.py --translations。
"""
import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks-dir", required=True)
    ap.add_argument("--out", default="translations.json")
    args = ap.parse_args()

    merged = {}
    conflicts = []
    files = sorted(Path(args.blocks_dir).glob("block_*.translations.json"))
    if not files:
        raise SystemExit(f"no block_*.translations.json found in {args.blocks_dir}")

    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for k, v in data.items():
            if k in merged and merged[k] != v:
                conflicts.append((f.name, k, merged[k], v))
            merged[k] = v

    if conflicts:
        print("CONFLICTS (same original, different translation) — keeping last, please review:")
        for c in conflicts[:20]:
            print(f"  {c[0]}: {c[1][:50]!r}")

    Path(args.out).write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"merged {len(merged)} unique units from {len(files)} block files -> {args.out}")


if __name__ == "__main__":
    main()
