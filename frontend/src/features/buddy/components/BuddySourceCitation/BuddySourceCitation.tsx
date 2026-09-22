/**
 * 回答的来源引用（契约 6 的 `Citation`）。
 *
 * 展示形式遵循 DEVELOPMENT_SPEC 第 9.3 节：「来源 + [资料名 · 位置]」。
 * 位置只来自后端返回的 `page` 或 `location_start/_end`，解析不出来时不显示，
 * 绝不伪造定位。
 *
 * 引用可点击：会话是按课程创建的，所以当前上下文里的 courseId 就是
 * 这条引用所属的课程，据此跳到资料阅读器并带上章节定位。
 */

import { Link } from "react-router-dom";

import { useBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { CitationDto } from "@/features/buddy/api";

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
      <span className={styles.label}>来源</span>
      <ul className={styles.list}>
        {citations.map((citation, index) => {
          const location = locationLabel(citation);
          const text = location
            ? `${citation.material_name} · ${location}`
            : citation.material_name;
          const title = [citation.section_title, citation.quote].filter(Boolean).join("：");
          const key = `${citation.material_id}-${citation.section_id ?? index}`;

          if (!context.courseId) {
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
