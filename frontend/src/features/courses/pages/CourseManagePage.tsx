/**
 * 课程管理（仅课程创建教师，契约 3）。
 *
 * 邀请码只在「创建教师查看未归档课程」以及「重置邀请码」的响应里出现，
 * 因此归档后这里不再显示邀请码——这不是前端藏起来，而是接口本来就不返回。
 */

import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Card } from "@/components/Card/Card";
import { EmptyState } from "@/components/EmptyState/EmptyState";
import { ErrorState } from "@/components/ErrorState/ErrorState";
import { Pill } from "@/components/Pill/Pill";
import { Skeleton, SkeletonLines } from "@/components/Skeleton/Skeleton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import {
  useArchiveCourse,
  useCourse,
  useCourseMembers,
  useRegenerateInviteCode,
  useUpdateCourse,
} from "@/features/courses/hooks/useCourses";
import { formatMonthDayTime } from "@/utils/datetime";
import { toAppError } from "@/services/http";

import styles from "./CourseManagePage.module.css";

const NAME_MAX = 100;
const DESCRIPTION_MAX = 2000;

export function CourseManagePage() {
  const { courseId } = useParams<{ courseId: string }>();
  const courseQuery = useCourse(courseId);
  const membersQuery = useCourseMembers(courseId);

  const update = useUpdateCourse(courseId ?? "");
  const archive = useArchiveCourse(courseId ?? "");
  const regenerate = useRegenerateInviteCode(courseId ?? "");

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [copied, setCopied] = useState(false);
  const initialized = useRef(false);

  useSetBuddyContext({ courseId, entityType: "course", entityId: courseId, route: "" });

  const course = courseQuery.data;

  // 只在详情首次到手时填充表单，避免用户输入被重渲染覆盖
  useEffect(() => {
    if (!course || initialized.current) return;
    initialized.current = true;
    setName(course.name);
    setDescription(course.description);
  }, [course]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1800);
    return () => window.clearTimeout(timer);
  }, [copied]);

  if (courseQuery.isPending) {
    return (
      <div className={styles.page} aria-busy="true">
        <Skeleton height={28} width="30%" />
        <div className={styles.gap} />
        <Card>
          <SkeletonLines lines={4} />
        </Card>
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

  if (!course || !course.isOwner) {
    return (
      <div className={styles.page}>
        <EmptyState
          title="只有课程创建教师可以管理这门课程"
          description="平台角色是教师并不代表可以管理其他教师创建的课程。"
        />
      </div>
    );
  }

  const readOnly = course.status === "archived";
  const updateError = update.isError ? toAppError(update.error) : null;
  const archiveError = archive.isError ? toAppError(archive.error) : null;
  const regenerateError = regenerate.isError ? toAppError(regenerate.error) : null;

  const dirty = name.trim() !== course.name || description !== course.description;

  function save() {
    const trimmed = name.trim();
    if (trimmed.length === 0) return;
    update.mutate({ name: trimmed, description });
  }

  async function copyInviteCode() {
    if (!course?.inviteCode) return;
    try {
      await navigator.clipboard.writeText(course.inviteCode);
      setCopied(true);
    } catch {
      // 剪贴板不可用时不影响展示，教师可以手动选中复制
      setCopied(false);
    }
  }

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.title}>课程管理</h1>
        <p className={styles.subtitle}>
          {course.name} · {course.statusLabel}
          {readOnly ? " · 已归档课程保持只读" : ""}
        </p>
      </header>

      <Card className={styles.card}>
        <h2 className={styles.cardTitle}>课程信息</h2>
        <div className={styles.form}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="manage-name">
              课程名称
            </label>
            <input
              id="manage-name"
              className={styles.input}
              value={name}
              maxLength={NAME_MAX}
              disabled={readOnly}
              onChange={(event) => setName(event.target.value)}
            />
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="manage-description">
              课程说明
            </label>
            <textarea
              id="manage-description"
              className={styles.textarea}
              rows={3}
              maxLength={DESCRIPTION_MAX}
              value={description}
              disabled={readOnly}
              onChange={(event) => setDescription(event.target.value)}
            />
            <p className={styles.hint}>清空说明可以留空保存。</p>
          </div>

          {updateError ? (
            <p className={styles.error} role="alert">
              {updateError.message}
            </p>
          ) : null}

          {!readOnly ? (
            <div>
              <Button variant="primary" onClick={save} disabled={update.isPending || !dirty}>
                {update.isPending ? "保存中…" : "保存"}
              </Button>
            </div>
          ) : null}
        </div>
      </Card>

      <Card className={styles.card}>
        <h2 className={styles.cardTitle}>邀请码</h2>
        {course.inviteCode ? (
          <>
            <p className={styles.cardHint}>
              把邀请码发给学生即可加入。重新生成后旧码会立刻失效。
            </p>
            <div className={styles.inviteRow}>
              <code className={styles.inviteCode}>{course.inviteCode}</code>
              <Button variant="secondary" size="sm" onClick={() => void copyInviteCode()}>
                {copied ? "已复制" : "复制"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={regenerate.isPending}
                onClick={() => regenerate.mutate()}
              >
                {regenerate.isPending ? "生成中…" : "重新生成"}
              </Button>
            </div>
          </>
        ) : (
          <p className={styles.cardHint}>
            课程已归档，接口不再返回邀请码；归档课程也不能再加入。
          </p>
        )}
        {regenerateError ? (
          <p className={styles.error} role="alert">
            {regenerateError.message}
          </p>
        ) : null}
      </Card>

      <Card className={styles.card}>
        <h2 className={styles.cardTitle}>
          课程成员
          {membersQuery.isSuccess ? (
            <span className={styles.count}>共 {membersQuery.data.total} 人</span>
          ) : null}
        </h2>

        {membersQuery.isPending ? (
          <div className={styles.gap} aria-busy="true">
            <SkeletonLines lines={3} />
          </div>
        ) : membersQuery.isError ? (
          <ErrorState
            message={toAppError(membersQuery.error).message}
            onRetry={() => void membersQuery.refetch()}
          />
        ) : (membersQuery.data?.items.length ?? 0) === 0 ? (
          <p className={styles.cardHint}>还没有学生加入。</p>
        ) : (
          <ul className={styles.members}>
            {membersQuery.data?.items.map((member) => (
              <li className={styles.member} key={member.userId}>
                <span className={styles.avatar} aria-hidden="true">
                  {member.initial}
                </span>
                <span className={styles.memberName}>{member.displayName}</span>
                <Pill tone={member.roleLabel === "教师" ? "info" : "neutral"}>
                  {member.roleLabel}
                </Pill>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card className={styles.card}>
        <h2 className={styles.cardTitle}>归档课程</h2>
        <p className={styles.cardHint}>
          归档后课程保持可读，但不能再上传资料、生成练习或加入新成员。第一版不提供恢复。
        </p>
        {archiveError ? (
          <p className={styles.error} role="alert">
            {archiveError.message}
          </p>
        ) : null}
        {readOnly ? (
          <p className={styles.hint}>这门课程已经归档。</p>
        ) : (
          <Button
            variant="secondary"
            disabled={archive.isPending}
            onClick={() => archive.mutate()}
          >
            {archive.isPending ? "归档中…" : "归档课程"}
          </Button>
        )}
      </Card>

      <p className={styles.footerNote}>
        课程更新于 {formatMonthDayTime(course.updatedAtLabel)}
      </p>
    </div>
  );
}
