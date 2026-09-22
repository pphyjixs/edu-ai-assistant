/**
 * 资料大纲：章节 + 知识点 + 原文摘录。
 *
 * 契约 5.4 只提供章节与知识点（含可核对的 `quote` 原文摘录与来源定位），
 * 不提供资料正文，因此这里不假装能渲染原文档——
 * 展示的就是解析产物本身。
 */

import { useEffect, useRef } from "react";

import type { MaterialOutlineVM } from "@/features/materials/model/types";

import styles from "./MaterialOutlineView.module.css";

export type MaterialOutlineViewProps = {
  outline: MaterialOutlineVM;
  /** 从引用跳进来时定位到的章节 */
  activeSectionId?: string;
};

export function MaterialOutlineView({ outline, activeSectionId }: MaterialOutlineViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!activeSectionId) return;
    const target = containerRef.current?.querySelector(`#section-${activeSectionId}`);
    target?.scrollIntoView({ block: "start", behavior: "smooth" });
  }, [activeSectionId]);

  return (
    <div className={styles.wrapper} ref={containerRef}>
      <nav className={styles.toc} aria-label="章节">
        <p className={styles.tocLabel}>章节</p>
        <ol className={styles.tocList}>
          {outline.sections.map((section) => (
            <li key={section.id}>
              <a
                className={
                  section.id === activeSectionId
                    ? `${styles.tocItem} ${styles.tocItemActive}`
                    : styles.tocItem
                }
                href={`#section-${section.id}`}
              >
                <span className={styles.tocOrder}>{String(section.order).padStart(2, "0")}</span>
                <span className={styles.tocTitle}>{section.title}</span>
              </a>
            </li>
          ))}
        </ol>
      </nav>

      <div className={styles.sections}>
        {outline.sections.map((section) => (
          <section className={styles.section} id={`section-${section.id}`} key={section.id}>
            <header className={styles.sectionHead}>
              <h2 className={styles.sectionTitle}>
                <span className={styles.sectionOrder}>{String(section.order).padStart(2, "0")}</span>
                {section.title}
              </h2>
              <span className={styles.location}>
                {section.sourceTypeLabel}
                {section.locationLabel ? ` · ${section.locationLabel}` : ""}
              </span>
            </header>

            {section.knowledgePoints.length === 0 ? (
              <p className={styles.emptySection}>这一节没有提取到知识点。</p>
            ) : (
              <ul className={styles.points}>
                {section.knowledgePoints.map((point) => (
                  <li className={styles.point} key={point.id}>
                    <div className={styles.pointHead}>
                      <span className={styles.pointTitle}>{point.title}</span>
                      {point.locationLabel ? (
                        <span className={styles.pointLocation}>{point.locationLabel}</span>
                      ) : null}
                    </div>
                    <p className={styles.pointDescription}>{point.description}</p>
                    {point.quote ? (
                      <blockquote className={styles.quote}>{point.quote}</blockquote>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
