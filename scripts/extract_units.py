"""从 babeldoc record 模式产出的 translate_tracking.json 提取带页码的翻译单元。

translate_tracking.json 典型结构 (babeldoc 0.6.x):
{
  "page": [ {"paragraph": [ {"input": 原文, ...}, ... ]}, ... ],   # 每页一个元素
  "cross_page":   [ {"input": ...}, ... ],
  "cross_column": [ {"input": ...}, ... ]
}

输出 units_with_pages.json: [{"page": 1-based PDF 页, "unit": 原文}, ...] (文档顺序)
page = -1 表示跨页/跨栏段落 (babeldoc 已单独配对, 但通用流程仍纳入翻译)。
"""
import argparse
import json
from pathlib import Path

# 与 run_babeldoc.py 的 --min-text-length 默认一致
DEFAULT_MIN_LEN = 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracking", required=True, help="translate_tracking.json 路径")
    ap.add_argument("--out", default="units_with_pages.json")
    ap.add_argument("--min-len", type=int, default=DEFAULT_MIN_LEN)
    args = ap.parse_args()

    d = json.loads(Path(args.tracking).read_text(encoding="utf-8"))
    units = []
    seen = set()

    for pi, pg in enumerate(d.get("page", [])):
        for para in pg.get("paragraph", []):
            text = para.get("input") or ""
            if not text or len(text.strip()) < args.min_len:
                continue
            key = (pi, text)
            if key in seen:
                continue
            seen.add(key)
            units.append({"page": pi + 1, "unit": text})  # 1-based PDF 页

    # 跨页/跨栏段落也纳入 (通用, 不一定每本都有)
    for section in ("cross_page", "cross_column"):
        for para in d.get(section, []):
            text = para.get("input") or ""
            if text and len(text.strip()) >= args.min_len:
                units.append({"page": -1, "unit": text})

    Path(args.out).write_text(
        json.dumps(units, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    total_chars = sum(len(u["unit"]) for u in units)
    print(f"units: {len(units)}, total chars: {total_chars}")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
