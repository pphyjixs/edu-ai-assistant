/**
 * 加入课程（仅学生，契约 3）。
 *
 * 邀请码按**不透明字符串**处理：不 trim 之外的规范化、不校验长度或字符集。
 * 旧码失效返回 422 INVITE_CODE_INVALID，归档课程返回 409 COURSE_ARCHIVED。
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { useJoinCourse } from "@/features/courses/hooks/useCourses";
import { toAppError } from "@/services/http";

import styles from "./JoinCourseForm.module.css";

export function JoinCourseForm() {
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const join = useJoinCourse();

  const error = join.isError ? toAppError(join.error) : null;

  function submit() {
    const value = code.trim();
    if (!value) return;
    join.mutate(value, {
      onSuccess: (course) => {
        setCode("");
        navigate(`/courses/${course.id}`);
      },
    });
  }

  return (
    <div className={styles.form}>
      <label className={styles.label} htmlFor="join-code">
        邀请码
      </label>
      <div className={styles.row}>
        <input
          id="join-code"
          className={styles.input}
          value={code}
          placeholder="输入教师提供的邀请码"
          onChange={(event) => setCode(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              submit();
            }
          }}
        />
        <Button
          variant="primary"
          onClick={submit}
          disabled={join.isPending || code.trim().length === 0}
        >
          {join.isPending ? "加入中…" : "加入课程"}
        </Button>
      </div>

      {error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}
    </div>
  );
}
