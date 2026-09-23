/**
 * 学生视角的一份提交。
 *
 * 状态文案与「下一步能做什么」都来自 VM，不在这里重新判断枚举。
 * 契约 9.5：下载地址是短时有效的预签名 GET，过期后重新进入页面即可刷新，
 * 因此界面上标注了过期时间，而不是让用户对着一个 403 的链接反复点。
 */

import { Link } from "react-router-dom";

import { Icon } from "@/components/Icon/Icon";
import { Pill } from "@/components/Pill/Pill";
import { SubmissionUploader } from "@/features/grading/components/SubmissionUploader/SubmissionUploader";
import type { SubmissionVM } from "@/features/grading/model/types";

import styles from "./SubmissionCard.module.css";

export type SubmissionCardProps = {
  submission: SubmissionVM;
  courseId: string;
  assignmentId: string;
  /** 作业当前是否仍允许提交（用于「重新上传」，后端仍会独立校验） */
  canUpload: boolean;
};

export function SubmissionCard({
  submission,
  courseId,
  assignmentId,
  canUpload,
}: SubmissionCardProps) {
  // 只有尚未正式提交的提交才允许重新上传；已提交后重新初始化会被 409 拒绝
  const allowReupload = canUpload && submission.canUpload;

  return (
    <div className={styles.wrap}>
      <div className={styles.fileRow}>
        <span className={styles.fileIcon} aria-hidden="true">
          <Icon name="file" size={16} />
        </span>
        <div className={styles.fileInfo}>
          <span className={styles.filename} title={submission.filename}>
            {submission.filename}
          </span>
          <span className={styles.meta}>
            {submission.typeLabel} · {submission.sizeLabel}
            {submission.submittedAtLabel ? ` · 提交于 ${submission.submittedAtLabel}` : ""}
            {submission.rubricVersion !== null
              ? ` · 按评分版本 v${submission.rubricVersion}`
              : ""}
          </span>
        </div>
        <div className={styles.pills}>
          {submission.isLateLabel ? <Pill tone="warn">{submission.isLateLabel}</Pill> : null}
          <Pill tone={submission.statusTone}>{submission.statusLabel}</Pill>
        </div>
      </div>

      <p className={styles.hint}>{submission.statusHint}</p>

      <div className={styles.actions}>
        {submission.downloadUrl ? (
          <a
            className={styles.link}
            href={submission.downloadUrl}
            target="_blank"
            rel="noreferrer"
          >
            下载已提交的报告
          </a>
        ) : null}

        {/*
          提交详情**总是**可达：发布前学生也要能确认"我交的是哪一份、现在到哪一步"，
          不能因为还没出分就把入口藏起来（发布后文案才变成"查看成绩与评语"）。
        */}
        <Link
          className={`${styles.link} ${submission.scoreVisible ? styles.primary : ""}`}
          to={`/courses/${courseId}/submissions/${submission.id}`}
        >
          {submission.scoreVisible ? "查看成绩与评语" : "查看提交详情"}
        </Link>

        {submission.downloadExpiresLabel ? (
          <span className={styles.expiry}>下载地址有效期至 {submission.downloadExpiresLabel}</span>
        ) : null}
      </div>

      {allowReupload ? (
        <details className={styles.reupload}>
          <summary>还没有正式提交？可以重新上传</summary>
          <p className={styles.reuploadHint}>
            重新上传会作废上一次未完成的上传地址，并在确认后覆盖这条记录。
          </p>
          <SubmissionUploader assignmentId={assignmentId} courseId={courseId} />
        </details>
      ) : null}
    </div>
  );
}
