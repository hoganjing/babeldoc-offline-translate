"""babeldoc-offline-translate 的核心驱动：用预翻译字典驱动 babeldoc 排版。

两段式：
  record 模式（不传 --translations，传 --record）：
      do_translate 把收到的原文单元记录到 --record 指定的 JSON 文件。
      babeldoc 同时产出 <outdir>/<book>/translate_tracking.json（全书所有段）。
  render 模式（传 --translations 字典）：
      do_translate 查字典返回译文；babeldoc 原生排版输出 dual.pdf + mono.pdf。

关键：do_llm_translate 抛 NotImplementedError，强制走非 LLM 分支
（babeldoc 逐段调用 do_translate）。我们从不改 babeldoc 源码，只用
BaseTranslator 扩展点挂一个子类。
"""
import argparse
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pretranslated")

from babeldoc.docvision.doclayout import OnnxModel
from babeldoc.format.pdf.high_level import translate as babeldoc_translate
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.translator.translator import BaseTranslator


class PreTranslatedTranslator(BaseTranslator):
    name = "pretranslated"

    def __init__(self, lang_in, lang_out, translations=None, record_path=None, ignore_cache=True):
        super().__init__(lang_in, lang_out, ignore_cache)
        self.translations = translations or {}
        self.record_path = Path(record_path) if record_path else None
        self.recorded = {}  # 原文 -> 出现次数
        self.missing = []   # 未命中缓存的原文(按顺序)

    def do_translate(self, text, rate_limit_params=None):
        if text is None:
            return None
        self.recorded[text] = self.recorded.get(text, 0) + 1
        if text in self.translations:
            return self.translations[text]
        self.missing.append(text)
        return text  # record 模式: 返回原文, 不影响流程

    def do_llm_translate(self, text, rate_limit_params=None):
        # 必须抛 NotImplementedError, 否则 babeldoc 会走 LLM prompt 包装路径
        raise NotImplementedError

    def dump_records(self):
        if self.record_path is None:
            return
        units = sorted(self.recorded.keys())
        self.record_path.write_text(
            json.dumps(units, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        logger.info("recorded %d unique units -> %s", len(units), self.record_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="输入 PDF")
    ap.add_argument("--translations", default=None, help="预翻译 JSON {原文: 译文}; 不传=record 模式")
    ap.add_argument("--record", default=None, help="record 模式输出 units.json")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--working-dir", default=None)
    ap.add_argument("--lang-in", default="en")
    ap.add_argument("--lang-out", default="zh-Hans")
    ap.add_argument("--no-mono", action="store_true")
    ap.add_argument("--no-dual", action="store_true")
    ap.add_argument("--ocr-workaround", action="store_true")
    ap.add_argument("--min-text-length", type=int, default=5)
    args = ap.parse_args()

    translations = {}
    if args.translations:
        translations = json.loads(Path(args.translations).read_text(encoding="utf-8"))
        logger.info("loaded %d pre-translations", len(translations))

    translator = PreTranslatedTranslator(
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        translations=translations,
        record_path=args.record,
        ignore_cache=True,
    )

    logger.info("loading doclayout onnx model ...")
    doc_layout_model = OnnxModel.from_pretrained()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    config = TranslationConfig(
        translator=translator,
        input_file=args.input,
        lang_in=args.lang_in,
        lang_out=args.lang_out,
        doc_layout_model=doc_layout_model,
        output_dir=str(outdir),
        no_dual=args.no_dual,
        no_mono=args.no_mono,
        ocr_workaround=args.ocr_workaround,
        auto_enable_ocr_workaround=args.ocr_workaround,
        auto_extract_glossary=False,       # 关闭术语提取(避免 LLM 调用)
        save_auto_extracted_glossary=False,
        skip_scanned_detection=True,        # 已知是扫描版判定, 跳过重复检测
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        min_text_length=args.min_text_length,
        working_dir=str(Path(args.working_dir).resolve()) if args.working_dir else None,
        pool_max_workers=4,
        debug=False,
    )

    logger.info("running babeldoc pipeline ...")
    result = None
    try:
        result = babeldoc_translate(config)
    except Exception as e:
        # fix_cmap 后处理可能因 working_dir 路径问题崩溃, 但 PDF 主体已生成;
        # 容忍该异常, 以输出目录中的实际文件为准。
        logger.warning(f"babeldoc pipeline raised (ignored, checking outputs): {type(e).__name__}: {e}")

    translator.dump_records()
    if translator.missing:
        miss_path = outdir / "missing.json"
        miss_path.write_text(
            json.dumps(sorted(set(translator.missing)), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        logger.warning("missing translations: %d (unique) -> %s", len(set(translator.missing)), miss_path)

    # 以输出目录实际文件为准（文件名随 lang_out 变化）
    ext = args.lang_out
    monos = sorted(outdir.glob(f"*.{ext}.mono.pdf"))
    duals = sorted(outdir.glob(f"*.{ext}.dual.pdf"))
    if not monos and not duals:
        raise RuntimeError("no output PDF generated")
    mono = monos[-1] if monos else None
    dual = duals[-1] if duals else None
    logger.info("outputs: mono=%s dual=%s", mono, dual)
    print("RESULT_JSON:", json.dumps({"mono": str(mono), "dual": str(dual)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
