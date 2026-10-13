"""诊断工具: 在真实的几页 PDF 上跑 babeldoc 的 doclayout 模型, 看它"看到"了什么。

输出每页的版面区域(layout)类别、置信度、边界框, 以及落在该框内的文字。
用于排查: 某段被错误切分 / 页眉被当成正文 / 图表被误判 等问题。

用法:
  python doclayout_demo.py --pdf book.pdf --pages 17,18
  (pages 为 0-indexed; 默认 [17,18] 即 PDF 第 18,19 页)
"""
import argparse
import json

import numpy as np
import pymupdf
from babeldoc.docvision.doclayout import OnnxModel
from babeldoc.format.pdf.document_il.utils.mupdf_helper import get_no_rotation_img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default="17,18", help="0-indexed 页号, 逗号分隔")
    ap.add_argument("--out", default="doclayout_demo.json")
    args = ap.parse_args()

    pages = [int(p) for p in args.pages.split(",") if p.strip() != ""]

    doc = pymupdf.open(args.pdf)
    model = OnnxModel.from_pretrained()

    out = {}
    for pi in pages:
        pix = get_no_rotation_img(doc[pi])
        image = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1]
        H, W = image.shape[:2]
        res = model.predict([image])[0]
        words = doc[pi].get_text("words")  # x0,y0,x1,y1,word,block,line,wordno
        layouts = []
        for box in res.boxes:
            x1, y1, x2, y2 = [float(v) for v in box.xyxy]
            cls = int(box.cls)
            name = res.names[cls]
            conf = float(box.conf)
            inside = []
            for w in words:
                wx0, wy0, wx1, wy1, wt = w[0], w[1], w[2], w[3], w[4]
                cx, cy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
                if x1 <= cx <= x2 and y1 <= cy <= y2:
                    inside.append((wy0, wx0, wt))
            inside.sort()
            text = " ".join(t for _, _, t in inside)
            layouts.append({
                "class_name": name,
                "box": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                "conf": round(conf, 3),
                "text": text,
            })
        layouts.sort(key=lambda L: L["box"][1])
        out[str(pi + 1)] = {"img_h": H, "img_w": W, "layouts": layouts}
        print(f"\n===== PDF page {pi + 1}  (image {W}x{H}) =====")
        for L in layouts:
            print(f"  [{L['class_name']:>12}] conf={L['conf']:.2f} box={L['box']}")
            print(f"        text: {L['text'][:160]!r}")

    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nsaved {args.out}")


if __name__ == "__main__":
    main()
