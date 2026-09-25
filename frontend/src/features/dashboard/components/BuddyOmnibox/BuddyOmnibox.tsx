/**
 * 首页的 Buddy 大输入框。
 *
 * 它本身就是完整的入口：选好课程 → 直接提问或点预设 Action。
 *
 * **发送后进入中央会话，不再弹出右侧面板**（开发方案 4.1 / 4.3）：
 * ``onAsk`` 由 ``useStartHomeChat`` 提供，负责创建会话与 Run、导航到
 * ``/chats/{sessionId}``。创建失败时这里**保留输入内容**并在下方显示错误，
 * 用户改一下就能重发。
 *
 * 课程是必选的：契约 6 的会话按课程创建，没有课程就无法建立上下文。
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Icon } from "@/components/Icon/Icon";
import { Skeleton } from "@/components/Skeleton/Skeleton";
import type { CourseVM } from "@/features/courses/model/types";
import { dashboardQuickActions, resolveActionRoute } from "@/features/buddy/model/actions";

import styles from "./BuddyOmnibox.module.css";

export type BuddyOmniboxProps = {
  courses: CourseVM[];
  isCoursesPending: boolean;
  selectedCourseId: string | undefined;
  onSelectCourse: (courseId: string) => void;
  /** 发送首页第一条消息；失败时抛错，输入内容会被保留 */
  onAsk: (prompt: string) => Promise<unknown>;
  /** 发送失败的可读文案（显示在输入框下方） */
  errorMessage?: string;
};

export function BuddyOmnibox({
  courses,
  isCoursesPending,
  selectedCourseId,
  onSelectCourse,
  onAsk,
  errorMessage,
}: BuddyOmniboxProps) {
  const [value, setValue] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const navigate = useNavigate();

  const canSubmit = value.trim().length > 0 && !isSubmitting;

  /**
   * 统一的发送入口。
   *
   * 返回是否发送成功：调用方据此决定要不要清空草稿——**失败时保留输入内容**，
   * 用户在输入框里打的字不会因为一次失败就凭空消失（开发方案 4.1）。
   */
  async function runAsk(text: string): Promise<boolean> {
    if (isSubmitting) return false;
    setIsSubmitting(true);
    try {
      await onAsk(text);
      return true;
    } catch {
      // 错误经由页面的 errorMessage 展示，这里只报告失败
      return false;
    } finally {
      setIsSubmitting(false);
    }
  }

  async function submitDraft(): Promise<void> {
    const text = value.trim();
    if (!text) return;
    const sent = await runAsk(text);
    if (sent) setValue("");
  }

  return (
    <div className={styles.card}>
      <div className={styles.top}>
        <span className={styles.spark} aria-hidden="true">
          <Icon name="spark" size={16} />
        </span>
        <label className="srOnly" htmlFor="omnibox-input">
          向 Buddy 提问
        </label>
        <input
          id="omnibox-input"
          className={styles.input}
          placeholder="问问 Buddy，比如：帮我拆解实验二的任务"
          value={value}
          disabled={isSubmitting}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void submitDraft();
            }
          }}
        />
      </div>

      {errorMessage ? (
        <p className={styles.error} role="alert">
          {errorMessage}
        </p>
      ) : null}

      <div className={styles.bottom}>
        <div className={styles.chips}>
          {dashboardQuickActions.map((action) => {
            // 领域动作（例如自动出题）跳转到真实流程，不在对话里伪造结果
            const target = resolveActionRoute(action, selectedCourseId);
            return (
              <button
                key={action.label}
                type="button"
                className={styles.chip}
                title={action.hint ?? action.prompt ?? ""}
                disabled={Boolean(action.route) && !target}
                onClick={() => {
                  if (action.route) {
                    if (target) navigate(target);
                    return;
                  }
                  if (action.prompt) void runAsk(action.prompt);
                }}
              >
                {action.label}
              </button>
            );
          })}
        </div>

        <div className={styles.right}>
          <label className="srOnly" htmlFor="omnibox-course">
            选择课程
          </label>
          {isCoursesPending ? (
            <Skeleton width={116} height={30} radius="999px" />
          ) : (
            <select
              id="omnibox-course"
              className={styles.select}
              value={selectedCourseId ?? ""}
              onChange={(event) => onSelectCourse(event.target.value)}
            >
              {courses.map((course) => (
                <option key={course.id} value={course.id}>
                  {course.name}
                </option>
              ))}
            </select>
          )}
          <Button
            variant="primary"
            size="sm"
            iconLeft="send"
            onClick={() => void submitDraft()}
            disabled={!canSubmit}
            aria-label="发送"
          />
        </div>
      </div>
    </div>
  );
}
