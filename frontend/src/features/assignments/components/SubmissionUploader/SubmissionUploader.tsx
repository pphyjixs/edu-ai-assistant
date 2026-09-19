/**
 * 实验报告提交区。
 *
 * 上传链路（契约 9）是「初始化上传 → 浏览器直传对象存储 → 完成提交」，
 * 本阶段由 assignments 的 mock adapter 直接返回 SUBMITTED，
 * 真实的直传实现在 services/upload 就绪后替换。
 *
 * 支持点击选择与拖拽；失败时给出可读原因并可重试。
 */

import { useRef, useState, type DragEvent } from "react";

import { Button } from "@/components/Button/Button";
import { Icon } from "@/components/Icon/Icon";
import { useCreateSubmission } from "@/features/assignments/hooks/useAssignments";
import { toAppError } from "@/services/http";

import styles from "./SubmissionUploader.module.css";

const ACCEPT = ".pdf,.doc,.docx";

export type SubmissionUploaderProps = {
  assignmentId: string;
  disabled: boolean;
  /** 不可提交时的说明，例如「作业已关闭提交」 */
  disabledReason?: string;
};

export function SubmissionUploader({
  assignmentId,
  disabled,
  disabledReason,
}: SubmissionUploaderProps) {
  const inputId = `submission-file-${assignmentId}`;
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const createSubmission = useCreateSubmission(assignmentId);

  const error = createSubmission.isError ? toAppError(createSubmission.error) : null;

  function submitFile(file: File | undefined) {
    if (!file) return;
    createSubmission.mutate({ filename: file.name, size: file.size });
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    if (disabled) return;
    submitFile(event.dataTransfer.files[0]);
  }

  return (
    <div className={styles.wrapper}>
      <label
        htmlFor={inputId}
        className={
          dragging
            ? `${styles.dropzone} ${styles.dropzoneActive}`
            : disabled
              ? `${styles.dropzone} ${styles.dropzoneDisabled}`
              : styles.dropzone
        }
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <span className={styles.icon} aria-hidden="true">
          <Icon name="upload" size={18} />
        </span>
        <span className={styles.text}>
          <strong>{createSubmission.isPending ? "正在提交…" : "拖拽实验报告到这里"}</strong>
          <span>
            {disabled
              ? (disabledReason ?? "当前不可提交")
              : "或点击选择文件，支持 PDF / Word"}
          </span>
        </span>

        <input
          ref={inputRef}
          id={inputId}
          type="file"
          accept={ACCEPT}
          className="srOnly"
          disabled={disabled || createSubmission.isPending}
          onChange={(event) => {
            submitFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
      </label>

      <div className={styles.footer}>
        <Button
          variant="secondary"
          size="sm"
          disabled={disabled || createSubmission.isPending}
          onClick={() => inputRef.current?.click()}
        >
          选择文件
        </Button>

        {error ? (
          <p className={styles.error} role="alert">
            {error.message}
          </p>
        ) : null}
      </div>
    </div>
  );
}
