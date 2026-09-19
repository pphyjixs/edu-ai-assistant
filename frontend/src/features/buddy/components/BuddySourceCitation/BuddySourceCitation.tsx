/**
 * 回答的来源引用（契约 6 的 citations）。
 *
 * 展示形式遵循 DEVELOPMENT_SPEC 第 9.3 节：「来源 + [资料名 · 页码]」。
 * 页码只来自后端返回的 page 字段，解析不出来时不显示，绝不伪造定位。
 *
 * 已知缺口：资料阅读器属于 materials 模块，本阶段不在范围内，
 * 因此引用暂不可点击跳转；资料模块完成后在这里接上路由即可。
 */

import type { CitationDto } from "@/features/buddy/api";

import styles from "./BuddySourceCitation.module.css";

export type BuddySourceCitationProps = {
  citations: CitationDto[];
};

export function BuddySourceCitation({ citations }: BuddySourceCitationProps) {
  if (citations.length === 0) return null;

  return (
    <div className={styles.sources}>
      <span className={styles.label}>来源</span>
      <ul className={styles.list}>
        {citations.map((citation) => (
          <li key={`${citation.material_id}-${citation.section_id}`}>
            <span
              className={styles.chip}
              title={`${citation.section_title}：${citation.quote}`}
            >
              {citation.material_name}
              {citation.page > 0 ? ` · P${citation.page}` : ""}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
