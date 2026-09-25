/**
 * 首页 Dashboard。
 *
 * 回答四件事：今天要做什么、我有哪些课程、最近学到哪里、Buddy 现在能帮什么
 * （DEVELOPMENT_SPEC 第 7.1 节）。页面只负责组织区块与声明 Buddy 上下文，
 * 数据来自 dashboard / courses 两个 feature 的查询。
 */

import { useEffect, useState } from "react";
import { useCurrentUser } from "@/features/auth/hooks/useCurrentUser";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useStartHomeChat } from "@/features/buddy/hooks/useHomeChat";
import { useBuddyStore } from "@/features/buddy/store/buddyStore";
import { useCourses } from "@/features/courses/hooks/useCourses";
import { BuddyOmnibox } from "@/features/dashboard/components/BuddyOmnibox/BuddyOmnibox";
import { DashboardRightRail } from "@/features/dashboard/components/DashboardRightRail/DashboardRightRail";
import { GreetingHero } from "@/features/dashboard/components/GreetingHero/GreetingHero";

import styles from "./DashboardPage.module.css";

export function DashboardPage() {
  const userQuery = useCurrentUser();
  const coursesQuery = useCourses();
  // 首页发送：创建会话与 Run 后进入 /chats/:sessionId，不打开右侧面板
  const { start, error: chatError, clearError } = useStartHomeChat();
  const lastCourseId = useBuddyStore((state) => state.activeChatCourseId);

  const courses = coursesQuery.data ?? [];
  // 初始课程优先沿用「最近选择的课程」（新建对话后不会退回第一门课）
  const [selectedCourseId, setSelectedCourseId] = useState<string | undefined>(
    lastCourseId,
  );

  // 默认选中最近一门课程：契约 6 的会话按课程创建，必须先有课程才能提问
  useEffect(() => {
    if (selectedCourseId && courses.some((course) => course.id === selectedCourseId)) {
      return;
    }
    if (courses.length > 0) {
      setSelectedCourseId(courses[0].id);
    }
  }, [selectedCourseId, courses]);

  // 首页只需要声明「当前在跟哪门课对话」，不需要组装任何请求
  useSetBuddyContext({ courseId: selectedCourseId, route: "" });

  return (
    <div className={styles.page}>
      <main className={styles.conversation}>
        <div className={styles.heroWrap}>
          <GreetingHero displayName={userQuery.data?.displayName ?? "同学"}>
            <BuddyOmnibox
              courses={courses}
              isCoursesPending={coursesQuery.isPending}
              selectedCourseId={selectedCourseId}
              onSelectCourse={(courseId) => {
                clearError();
                setSelectedCourseId(courseId);
              }}
              onAsk={start}
              errorMessage={chatError?.message}
            />
          </GreetingHero>
        </div>
      </main>

      <DashboardRightRail activeCourseId={selectedCourseId} />
    </div>
  );
}
