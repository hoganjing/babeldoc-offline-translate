"""把 units_with_pages.json 按页分块, 供并行翻译子代理使用。

输入: [{"page": 1-based PDF 页, "unit": 原文}, ...] (文档顺序)
输出: <out-dir>/block_NNN.json + <out-dir>/index.json

每个 block_NNN.json:
{
  "block_id": 0,
  "pdf_pages": [3, 4, ...],
  "page_range": "3-10",
  "articles": ["可选: 本块覆盖的文章/章节名"],
  "units": ["原文串1", "原文串2", ...]   # 待译单元, 顺序=文档顺序
}

--article-map 可选: 给每个块附加"文章/章节"上下文, 帮助翻译保持术语与语气一致。
  支持两种格式:
    · 区间: [[start_page, end_page, "文章名"], ...]
    · 点映射: {"12": "文章名", 15: "另一篇"}
  无 article-map 时 articles 字段为空, 由调用方另行补充上下文。
"""
import argparse
import json
from pathlib import Path


def article_for_page(article_map, pdf_page):
    if not article_map:
        return None
    # 区间 [[start, end, name], ...]
    for entry in article_map:
        if isinstance(entry, list) and len(entry) == 3:
            s, e, name = entry
            if s <= pdf_page <= e:
                return name
    # 点映射
    return article_map.get(str(pdf_page)) or article_map.get(pdf_page)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--units", required=True, help="units_with_pages.json")
    ap.add_argument("--out-dir", default="blocks")
    ap.add_argument("--block-pages", type=int, default=8, help="每块页数 (默认 8 ≈ 13K 英文字符)")
    ap.add_argument("--article-map", default=None, help="可选 JSON: 文章/页码映射")
    args = ap.parse_args()

    items = json.loads(Path(args.units).read_text(encoding="utf-8"))
    article_map = None
    if args.article_map:
        article_map = json.loads(Path(args.article_map).read_text(encoding="utf-8"))

    page_units = {}
    for it in items:
        if it["page"] < 0:
            continue
        page_units.setdefault(it["page"], []).append(it["unit"])

    pages = sorted(page_units.keys())
    blocks, cur = [], []
    for p in pages:
        cur.append(p)
        if len(cur) >= args.block_pages:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清掉旧块文件
    for f in out_dir.glob("block_*.json"):
        f.unlink()

    index = []
    for bi, pgs in enumerate(blocks):
        units_in_block = []
        for p in pgs:
            units_in_block.extend(page_units[p])
        if not units_in_block:
            continue
        articles = sorted({a for a in (article_for_page(article_map, p) for p in pgs) if a})
        data = {
            "block_id": bi,
            "pdf_pages": pgs,
            "page_range": f"{pgs[0]}-{pgs[-1]}",
            "articles": articles,
            "units": units_in_block,
        }
        fname = out_dir / f"block_{bi:03d}.json"
        fname.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        index.append({
            "block_id": bi,
            "file": fname.name,
            "page_range": data["page_range"],
            "articles": articles,
            "unit_count": len(units_in_block),
            "char_count": sum(len(u) for u in units_in_block),
        })
        print(f"block_{bi:03d}: PDF p{data['page_range']}, {len(units_in_block)} units, {index[-1]['char_count']} chars")

    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"total blocks: {len(index)}")


if __name__ == "__main__":
    main()
