/**
 * 回答的来源引用（契约 6 的 `Citation`）。
 *
 * 展示形式遵循 DEVELOPMENT_SPEC 第 9.3 节：「来源 + [资料名 · 位置]」。
 * 位置只来自后端返回的 `page` 或 `location_start/_end`，解析不出来时不显示，
 * 绝不伪造定位。
 *
 * 引用有两类（评审文档「一、#2」）：
 *
 * - `MATERIAL`：指向课程资料的片段 / 章节，可点击跳到资料阅读器并带上章节定位；
 * - `ASSIGNMENT`：指向作业与评分标准。它们是可信依据，但没有页码，
 *   也不属于任何资料，因此只展示、不跳转。
 */

import { Link } from "react-router-dom";

import {
  citationLabel,
  isMaterialCitation,
  type CitationDto,
} from "@/features/buddy/api";
import { useBuddyContext } from "@/features/buddy/hooks/useBuddy";

import styles from "./BuddySourceCitation.module.css";

/** 来源类型 → 位置单位，与契约 5.4 的 source_type 对应 */
const LOCATION_UNIT: Record<string, string> = {
  PDF_PAGE: "P",
  PPTX_SLIDE: "幻灯片",
  DOCX_PARAGRAPH: "段落",
};

function locationLabel(citation: CitationDto): string {
  if (typeof citation.page === "number") return `P${citation.page}`;

  const unit = LOCATION_UNIT[citation.source_type];
  if (!unit) return "";

  const { location_start: start, location_end: end } = citation;
  if (typeof start !== "number") return "";
  if (start === end) return `${unit}${start}`;
  return `${unit}${start}-${end}`;
}

export type BuddySourceCitationProps = {
  citations: CitationDto[];
};

export function BuddySourceCitation({ citations }: BuddySourceCitationProps) {
  const context = useBuddyContext();

  if (citations.length === 0) return null;

  return (
    <div className={styles.sources}>
      <span className={styles.label}>
        {/* 作业/评分标准不是资料，标题写「依据」更准确 */}
        {citations.every((citation) => !isMaterialCitation(citation))
          ? "依据"
          : "来源"}
      </span>
      <ul className={styles.list}>
        {citations.map((citation, index) => {
          const material = isMaterialCitation(citation);
          const location = locationLabel(citation);
          const name = citationLabel(citation);
          const text = location ? `${name} · ${location}` : name;
          const title = [citation.section_title, citation.quote]
            .filter(Boolean)
            .join("：");
          const key = `${citation.source_id ?? citation.material_id ?? index}-${index}`;

          // 非资料引用（作业 / 评分标准）没有可跳转的目标
          if (!material || !context.courseId || !citation.material_id) {
            return (
              <li key={key}>
                <span className={styles.chip} title={title}>
                  {text}
                </span>
              </li>
            );
          }

          const target = `/courses/${context.courseId}/materials/${citation.material_id}${
            citation.section_id ? `?section=${citation.section_id}` : ""
          }`;

          return (
            <li key={key}>
              <Link className={styles.chip} to={target} title={title}>
                {text}
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
