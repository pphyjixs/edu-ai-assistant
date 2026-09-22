/**
 * 我的课程。
 *
 * 教师在这里创建课程；学生在这里用邀请码加入。
 * 课程列表包含已归档课程（契约 3.1），因此卡片上会标出状态。
 */

import { Card } from "@/components/Card/Card";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { CourseGrid } from "@/features/courses/components/CourseGrid/CourseGrid";
import { CreateCourseForm } from "@/features/courses/components/CreateCourseForm/CreateCourseForm";
import { JoinCourseForm } from "@/features/courses/components/JoinCourseForm/JoinCourseForm";
import { useCoursesPaged } from "@/features/courses/hooks/useCourses";
import { toAppError } from "@/services/http";

import styles from "./CoursesPage.module.css";

export function CoursesPage() {
  const userQuery = useCurrentUser();
  const role = userQuery.data?.role;
  const coursesQuery = useCoursesPaged(1, 100);

  useSetBuddyContext({ route: "" });

  const error = coursesQuery.isError ? toAppError(coursesQuery.error) : null;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>我的课程</h1>
        <p className={styles.subtitle}>
          {coursesQuery.isSuccess
            ? `共 ${coursesQuery.data.total} 门课程，包含已归档的课程`
            : "正在读取课程列表…"}
        </p>
      </header>

      <Card className={styles.actionCard}>
        {role === "teacher" ? (
          <>
            <h2 className={styles.actionTitle}>创建课程</h2>
            <p className={styles.actionHint}>
              创建后会自动生成邀请码，把它发给学生即可加入。
            </p>
            <CreateCourseForm />
          </>
        ) : (
          <>
            <h2 className={styles.actionTitle}>加入课程</h2>
            <p className={styles.actionHint}>向教师索取邀请码，输入后即可加入课程。</p>
            <JoinCourseForm />
          </>
        )}
      </Card>

      {error ? (
        <ErrorState
          title="课程加载失败"
          message={error.message}
          requestId={error.requestId}
          onRetry={() => void coursesQuery.refetch()}
        />
      ) : (
        <CourseGrid
          courses={coursesQuery.data?.items ?? []}
          isPending={coursesQuery.isPending}
        />
      )}
    </div>
  );
}
