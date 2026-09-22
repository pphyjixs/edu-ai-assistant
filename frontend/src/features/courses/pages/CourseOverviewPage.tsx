/**
 * 课程概览。
 *
 * 学生与教师看到同一个页面，差异在入口：教师多一个「课程管理」。
 * 概览只放真实存在的数据——资料与练习都来自接口；
 * 契约里没有学习进度、近期活跃或成绩统计，这里就不画这些图。
 */

import { Link, useParams } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Pill } from "@/components/Pill/Pill";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { AgentActionButton } from "@/features/buddy/components/AgentActionButton/AgentActionButton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { useMaterials } from "@/features/materials/hooks/useMaterials";
import { usePracticeSets } from "@/features/practice/hooks/usePractice";
import { toAppError } from "@/services/http";

import styles from "./CourseOverviewPage.module.css";

export function CourseOverviewPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const courseQuery = useCourse(courseId);
  const materialsQuery = useMaterials(courseId);
  const practiceQuery = usePracticeSets(courseId);

  useSetBuddyContext({ courseId, entityType: "course", entityId: courseId, route: "" });

  if (courseQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={28} width="40%" />
        <div className={styles.gap} />
        <SkeletonLines lines={3} />
      </div>
    );
  }

  if (courseQuery.isError) {
    const error = toAppError(courseQuery.error);
    return (
      <div className={styles.page}>
        <ErrorState
          title="课程加载失败"
          message={error.message}
          requestId={error.requestId}
          onRetry={() => void courseQuery.refetch()}
        />
      </div>
    );
  }

  const course = courseQuery.data;
  const recentMaterials = (materialsQuery.data?.items ?? []).slice(0, 4);
  const recentPractice = (practiceQuery.data?.items ?? []).slice(0, 3);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.headMain}>
          <div className={styles.statusRow}>
            <Pill tone={course.status === "archived" ? "neutral" : "info"}>
              {course.statusLabel}
            </Pill>
            {course.isOwner ? <Pill tone="agent">我创建的</Pill> : null}
          </div>
          <h1 className={styles.title}>{course.name}</h1>
          <p className={styles.description}>
            {course.description || "这门课程还没有填写说明。"}
          </p>
        </div>

        <div className={styles.actions}>
          <AgentActionButton label="总结课程" prompt="请帮我总结这门课程的主要内容和知识点。" agentAction="SUMMARIZE_CONTEXT" />
          <Link className={styles.linkButton} to={`/courses/${courseId}/materials`}>
            课程资料
          </Link>
          <Link className={styles.linkButton} to={`/courses/${courseId}/learn`}>
            学习
          </Link>
          {course.isOwner ? (
            <Link className={styles.linkButton} to={`/courses/${courseId}/manage`}>
              课程管理
            </Link>
          ) : null}
        </div>
      </header>

      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>课程资料</h2>
          <Link className={styles.more} to={`/courses/${courseId}/materials`}>
            查看全部
          </Link>
        </div>

        {materialsQuery.isPending ? (
          <Skeleton height={64} radius="14px" />
        ) : recentMaterials.length === 0 ? (
          <EmptyState
            title="还没有课程资料"
            description={
              course.isOwner
                ? "上传课件后，系统会解析出章节大纲和知识点。"
                : "教师上传课件后，这里会出现课程内容。"
            }
            illustration="empty-learning"
          />
        ) : (
          <Card>
            <ul className={styles.list}>
              {recentMaterials.map((material) => (
                <li className={styles.listItem} key={material.id}>
                  <Link
                    className={styles.itemTitle}
                    to={`/courses/${courseId}/materials/${material.id}`}
                  >
                    {material.filename}
                  </Link>
                  <div className={styles.itemMeta}>
                    <Pill tone={material.statusTone}>{material.statusLabel}</Pill>
                    <span>{material.typeLabel}</span>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </section>

      <section className={styles.section}>
        <div className={styles.sectionHead}>
          <h2 className={styles.sectionTitle}>练习</h2>
          <Link className={styles.more} to={`/courses/${courseId}/learn`}>
            查看全部
          </Link>
        </div>

        {practiceQuery.isPending ? (
          <Skeleton height={64} radius="14px" />
        ) : recentPractice.length === 0 ? (
          <EmptyState
            title="还没有已发布的练习"
            description={
              course.isOwner
                ? "在学习页生成并发布练习后，学生就能在这里开始作答。"
                : "教师发布练习后，会出现在这里。"
            }
          />
        ) : (
          <Card>
            <ul className={styles.list}>
              {recentPractice.map((practiceSet) => (
                <li className={styles.listItem} key={practiceSet.id}>
                  <Link
                    className={styles.itemTitle}
                    to={`/courses/${courseId}/learn/${practiceSet.id}`}
                  >
                    {practiceSet.title}
                  </Link>
                  <div className={styles.itemMeta}>
                    <Pill tone={practiceSet.statusTone}>{practiceSet.statusLabel}</Pill>
                    <span>
                      {practiceSet.questionCount} 题 · {practiceSet.difficultyLabel}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </section>
    </div>
  );
}
