import { useAppStore } from '@/stores/appStore';
import { hasInlineRichText, stripInlineRichText } from '@/utils/richText';
import { useInlineLabelHtml } from '@/services/contentResolver';

interface RichLabelProps {
  /** 已经解析过国际化的 label 文本 */
  text: string;
  className?: string;
  /** 覆盖 tooltip 文案；默认用去掉富文本标记的纯文本 */
  title?: string;
}

/**
 * 选项 / 输入项的 label 渲染。
 *
 * 资源作者可以在 label 里写行内 Markdown（如 `![](resource/image/icon/xxx.png) 名称 ★5`），
 * 支持范围见 {@link hasInlineRichText}：图片 / 链接 / 行内代码 / 加粗 / 白名单行内 HTML 标签。
 * 判定不通过时直接按纯文本输出，不引入解析开销。
 *
 * - 解析完成前先用纯文本占位，避免闪出 `![](xxx.png)` 这类源码；
 * - tooltip 始终使用去掉标记的纯文本（不放 HTML 与 data URL）；
 * - 本地图片解析失败时对应 `<img>` 会被移除，不留破图。
 */
export function RichLabel({ text, className, title }: RichLabelProps) {
  const basePath = useAppStore((state) => state.basePath);
  const { html, pending } = useInlineLabelHtml(text, basePath);
  const rich = hasInlineRichText(text);
  const plainText = rich ? stripInlineRichText(text) : text;

  if (pending || !html) {
    return (
      <span className={className} title={title ?? plainText}>
        {plainText}
      </span>
    );
  }

  return (
    <span
      className={className}
      title={title ?? plainText}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
