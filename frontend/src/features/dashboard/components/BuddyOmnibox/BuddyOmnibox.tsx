/**
 * 首页的 Buddy 大输入框。
 *
 * 它本身就是一个完整的入口：选好课程 → 直接提问或点预设 Action，
 * 面板会自动打开并进入对应课程的会话（DEVELOPMENT_SPEC 第 2.2 节 A / 第 7.2 节）。
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
  onAsk: (prompt: string) => void;
};

export function BuddyOmnibox({
  courses,
  isCoursesPending,
  selectedCourseId,
  onSelectCourse,
  onAsk,
}: BuddyOmniboxProps) {
  const [value, setValue] = useState("");
  const navigate = useNavigate();

  function submit() {
    const text = value.trim();
    if (!text) return;
    onAsk(text);
    setValue("");
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
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
        />
      </div>

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
                  if (action.prompt) onAsk(action.prompt);
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
            onClick={submit}
            disabled={value.trim().length === 0}
            aria-label="发送"
          />
        </div>
      </div>
    </div>
  );
}
