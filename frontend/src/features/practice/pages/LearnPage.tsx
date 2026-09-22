/**
 * 课程学习页（练习）。
 *
 * 学生看到的是已发布练习列表；教师在同一个页面里生成练习、
 * 跟踪生成任务并进入草稿预览发布（契约 7.1 的角色差异）。
 */

import { useState } from "react";
import { useParams } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { AgentActionButton } from "@/features/buddy/components/AgentActionButton/AgentActionButton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { useMaterials } from "@/features/materials/hooks/useMaterials";
import { GeneratePracticeForm } from "@/features/practice/components/GeneratePracticeForm/GeneratePracticeForm";
import { PracticeGenerateProgress } from "@/features/practice/components/PracticeGenerateProgress/PracticeGenerateProgress";
import { PracticeSetCard } from "@/features/practice/components/PracticeSetCard/PracticeSetCard";
import { useGeneratedDrafts, usePracticeSets } from "@/features/practice/hooks/usePractice";
import { toAppError } from "@/services/http";

import styles from "./LearnPage.module.css";

type ActiveJob = { jobId: string; setId: string };

export function LearnPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const courseQuery = useCourse(courseId);
  const practiceQuery = usePracticeSets(courseId);
  const materialsQuery = useMaterials(courseId);
  const drafts = useGeneratedDrafts(courseId);
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);

  useSetBuddyContext({ courseId, entityType: "course", entityId: courseId, route: "" });

  const course = courseQuery.data;
  const isOwner = Boolean(course?.isOwner);
  const readOnly = course?.status === "archived";
  const listError = practiceQuery.isError ? toAppError(practiceQuery.error) : null;

  const published = practiceQuery.data?.items ?? [];

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>学习</h1>
          <p className={styles.subtitle}>
            基于课程资料生成的练习。教师发布后学生可以作答，提交后立刻看到得分与解析。
            {readOnly ? " 课程已归档，练习与历史结果仍可查看。" : ""}
          </p>
        </div>
        <AgentActionButton label="总结课程" prompt="请帮我总结这门课程的重点内容。" agentAction="SUMMARIZE_CONTEXT" />
      </header>

      {isOwner && !readOnly ? (
        <Card className={styles.block}>
          <h2 className={styles.blockTitle}>生成练习</h2>
          <p className={styles.blockHint}>
            选择已解析完成的资料，指定题量与题型，题目由后台任务生成。
          </p>
          <GeneratePracticeForm
            courseId={courseId ?? ""}
            materials={materialsQuery.data?.items ?? []}
            onGenerated={(jobId, setId) => setActiveJob({ jobId, setId })}
          />
        </Card>
      ) : null}

      {activeJob ? (
        <div className={styles.block}>
          <PracticeGenerateProgress
            jobId={activeJob.jobId}
            courseId={courseId ?? ""}
            setId={activeJob.setId}
          />
        </div>
      ) : null}

      {isOwner && drafts.items.length > 0 ? (
        <section className={styles.section}>
          <h2 className={styles.sectionTitle}>待发布（仅你可见）</h2>
          <div className={styles.grid}>
            {drafts.items.map(({ vm }) => (
              <PracticeSetCard key={vm.id} practiceSet={vm} courseId={courseId ?? ""} />
            ))}
          </div>
        </section>
      ) : null}

      <section className={styles.section}>
        <h2 className={styles.sectionTitle}>
          已发布练习
          {practiceQuery.isSuccess ? (
            <span className={styles.count}>共 {practiceQuery.data.total} 套</span>
          ) : null}
        </h2>

        {practiceQuery.isPending ? (
          <div className={styles.grid} aria-busy="true">
            <Skeleton height={148} radius="14px" />
            <Skeleton height={148} radius="14px" />
          </div>
        ) : listError ? (
          <ErrorState
            title="练习加载失败"
            message={listError.message}
            requestId={listError.requestId}
            onRetry={() => void practiceQuery.refetch()}
          />
        ) : published.length === 0 ? (
          <EmptyState
            illustration="empty-learning"
            title="还没有已发布的练习"
            description={
              isOwner
                ? "生成练习并发布后，学生就能在这里看到。"
                : "教师发布练习后，会出现在这里。"
            }
          />
        ) : (
          <div className={styles.grid}>
            {published.map((practiceSet) => (
              <PracticeSetCard
                key={practiceSet.id}
                practiceSet={practiceSet}
                courseId={courseId ?? ""}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
