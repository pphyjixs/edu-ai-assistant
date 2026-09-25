/**
 * 首页中央会话页（``/chats/:sessionId``）。
 *
 * 交互形态接近桌面版 ChatGPT：消息列在中央、输入框固定在内容底部
 * （开发方案 1 / 4.1）。刷新后只有 ``sessionId``，因此用
 * :func:`useHomeChatSession` 从服务端取回所属课程，再声明 Buddy 上下文——
 * **不相信 URL 或 sessionStorage 里的课程 ID**。
 *
 * 这个页面不渲染 Buddy 面板与 BuddyFab（由 ``DashboardLayout`` 按路由决定）。
 */

import { Button } from "@/components/Button/Button";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import { BuddyThreadView } from "@/features/buddy/components/BuddyThreadView/BuddyThreadView";
import { useSetBuddyContext, useSetBuddySurface } from "@/features/buddy/hooks/useBuddy";
import { useHomeChatSession } from "@/features/buddy/hooks/useHomeChat";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { DashboardRightRail } from "@/features/dashboard/components/DashboardRightRail/DashboardRightRail";
import { toAppError } from "@/services/http";
import { useNavigate, useParams } from "react-router-dom";

import styles from "./HomeChatPage.module.css";

export function HomeChatPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  const navigate = useNavigate();

  // 中央会话页属于 HOME 承载面：面板的「自动打开」在这里不生效
  useSetBuddySurface("HOME");

  const sessionQuery = useHomeChatSession(sessionId);
  const courseId = sessionQuery.data?.course_id;
  const courseQuery = useCourse(courseId);

  // 上下文只认服务端返回的 course_id
  useSetBuddyContext({ courseId, route: "" });

  const error = sessionQuery.isError ? toAppError(sessionQuery.error) : null;

  return (
    <div className={styles.page}>
      <main className={styles.chatColumn}>
        <header className={styles.bar}>
          <Button
            variant="ghost"
            size="sm"
            iconLeft="chevron-left"
            onClick={() => navigate("/")}
            aria-label="返回首页"
          />
          <div className={styles.crumbs}>
            {sessionQuery.isPending ? (
              <Skeleton width={140} height={16} radius="6px" />
            ) : (
              <span className={styles.course}>{courseQuery.data?.name ?? "课程"}</span>
            )}
            <strong className={styles.section}>对话</strong>
          </div>
        </header>

        {error ? (
          <div className={styles.errorWrap}>
            <ErrorState
              title="会话加载失败"
              message={error.message}
              requestId={error.requestId}
              onRetry={() => void sessionQuery.refetch()}
            />
          </div>
        ) : (
          <BuddyThreadView
            variant="center"
            sessionId={sessionId}
            autoFocusComposer
          />
        )}
      </main>

      <DashboardRightRail activeCourseId={courseId} />
    </div>
  );
}
