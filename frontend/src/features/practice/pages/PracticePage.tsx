/**
 * 练习详情页。
 *
 * 同一个路由，三种形态：
 * - 教师：预览含答案的题目，草稿可发布（`TeacherPracticePreview`）；
 * - 学生已提交：展示得分与逐题解析（`PracticeResultView`）；
 * - 学生未提交：作答（`PracticeRunner`）。
 *
 * 已提交的判定优先用 URL 上的 `attempt` 参数，其次用本地索引——
 * 契约里没有「按练习查我的答题记录」的接口，本地索引是让它可续接的唯一办法。
 */

import { Link, useParams, useSearchParams } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Pill } from "@/components/Pill/Pill";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { PracticeResultView } from "@/features/practice/components/PracticeResultView/PracticeResultView";
import { PracticeRunner } from "@/features/practice/components/PracticeRunner/PracticeRunner";
import { TeacherPracticePreview } from "@/features/practice/components/TeacherPracticePreview/TeacherPracticePreview";
import {
  usePracticeAttempt,
  usePracticeSet,
  useStoredAttemptId,
} from "@/features/practice/hooks/usePractice";
import { toAppError } from "@/services/http";

import styles from "./PracticePage.module.css";

export function PracticePage() {
  const { courseId, setId } = useParams<{ courseId: string; setId: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  const courseQuery = useCourse(courseId);
  const setQuery = usePracticeSet(setId);

  const storedAttemptId = useStoredAttemptId(setId);
  const attemptId = searchParams.get("attempt") ?? storedAttemptId;
  const attemptQuery = usePracticeAttempt(setId, attemptId);

  useSetBuddyContext({
    courseId,
    entityType: "practice",
    entityId: setId,
    route: "",
  });

  const isOwner = Boolean(courseQuery.data?.isOwner);
  const readOnly = courseQuery.data?.status === "archived";
  const backTo = `/courses/${courseId}/learn`;

  if (setQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={26} width="38%" />
        <div className={styles.gap} />
        <Card>
          <SkeletonLines lines={5} />
        </Card>
      </div>
    );
  }

  if (setQuery.isError) {
    const error = toAppError(setQuery.error);

    // 学生视角下未发布的练习按不存在处理（契约 7.1）
    if (error.code === "RESOURCE_NOT_FOUND") {
      return (
        <div className={styles.page}>
          <Card>
            <div className={styles.stateCard}>
              <p className={styles.stateTitle}>练习不存在或尚未发布</p>
              <p className={styles.stateText}>
                这套练习可能还没有发布，或者链接已经失效。
              </p>
              <Link className={styles.stateLink} to={backTo}>
                回到学习页
              </Link>
            </div>
          </Card>
        </div>
      );
    }

    return (
      <div className={styles.page}>
        <ErrorState
          title="练习加载失败"
          message={error.message}
          requestId={error.requestId}
          onRetry={() => void setQuery.refetch()}
        />
      </div>
    );
  }

  const practiceSet = setQuery.data;

  const showResult = attemptId !== undefined && attemptQuery.data !== undefined;

  return (
    <div className={styles.page}>
      <div className={styles.breadcrumb}>
        <Link to={backTo} className={styles.back}>
          学习
        </Link>
        <span aria-hidden="true">›</span>
      </div>

      <header className={styles.header}>
        <div>
          <div className={styles.statusRow}>
            <Pill tone={practiceSet.statusTone}>{practiceSet.statusLabel}</Pill>
            <span className={styles.meta}>
              {practiceSet.questionCount} 题 · {practiceSet.difficultyLabel} ·{" "}
              {practiceSet.typeLabels.join(" / ")}
            </span>
          </div>
          <h1 className={styles.title}>{practiceSet.title}</h1>
        </div>
      </header>

      {showResult ? (
        attemptQuery.isPending ? (
          <Card>
            <div aria-busy="true">
              <SkeletonLines lines={6} />
            </div>
          </Card>
        ) : attemptQuery.isError ? (
          <Card>
            <div className={styles.stateCard}>
              <p className={styles.stateTitle}>答题结果读取失败</p>
              <p className={styles.stateText}>{toAppError(attemptQuery.error).message}</p>
              <button
                type="button"
                className={styles.stateLink}
                onClick={() => {
                  // 结果取不到（例如记录已失效）：退回答题态重新开始
                  setSearchParams({}, { replace: true });
                }}
              >
                返回练习
              </button>
            </div>
          </Card>
        ) : (
          <PracticeResultView attempt={attemptQuery.data!} />
        )
      ) : isOwner ? (
        <TeacherPracticePreview
          courseId={courseId ?? ""}
          practiceSet={practiceSet}
          readOnly={readOnly}
        />
      ) : attemptId !== undefined ? (
        <Card>
          <div aria-busy="true">
            <SkeletonLines lines={5} />
          </div>
        </Card>
      ) : (
        <PracticeRunner
          courseId={courseId ?? ""}
          practiceSet={practiceSet}
          onSubmitted={(newAttemptId) => setSearchParams({ attempt: newAttemptId })}
        />
      )}
    </div>
  );
}
