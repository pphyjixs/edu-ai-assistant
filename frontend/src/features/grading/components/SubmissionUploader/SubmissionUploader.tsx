/**
 * 学生报告上传（契约 9.2 / 9.3）。
 *
 * 走与课件上传相同的三段式：初始化 → 浏览器直传对象存储 → 完成确认。
 * 两处差异必须体现在界面上：
 *
 * - 只接受 **PDF / DOCX**（`.doc`、`.pptx` 会被后端拒绝），因此 `accept`
 *   与提示文案都用提交自己的白名单；
 * - 上传完成**不会**自动触发批改（契约 9.1），所以成功后文案是「已提交，
 *   等待教师批改」，不能写成「正在批改」。
 */

import { useRef, useState, type DragEvent } from "react";

import { Icon } from "@/components/Icon/Icon";
import { useUploadSubmission } from "@/features/grading/hooks/useSubmissions";
import { UPLOAD_STEP_LABEL, type UploadStep } from "@/features/grading/model/upload";
import { toAppError } from "@/services/http";
import { SUBMISSION_EXTENSIONS } from "@/services/upload";

import styles from "./SubmissionUploader.module.css";

const INPUT_ID = "submission-upload-input";

export type SubmissionUploaderProps = {
  assignmentId: string;
  courseId: string;
  /** 已经在批改流程中时不再允许重新上传（后端也会拒绝） */
  disabled?: boolean;
  /** 上传成功后回调，便于上层刷新或滚动到状态区 */
  onUploaded?: () => void;
};

export function SubmissionUploader({
  assignmentId,
  courseId,
  disabled = false,
  onUploaded,
}: SubmissionUploaderProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const upload = useUploadSubmission(assignmentId, courseId);

  const error = upload.isError ? toAppError(upload.error) : null;
  const step: UploadStep | null = upload.step;

  function start(file: File | undefined) {
    if (!file || upload.isPending || disabled) return;
    upload.mutate(file, { onSuccess: () => onUploaded?.() });
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    start(event.dataTransfer.files[0]);
  }

  const extensions = SUBMISSION_EXTENSIONS.join(" / ").toUpperCase();

  return (
    <div>
      <label
        htmlFor={INPUT_ID}
        className={[
          styles.dropzone,
          dragging ? styles.active : "",
          disabled || upload.isPending ? styles.disabled : "",
        ]
          .filter(Boolean)
          .join(" ")}
        onDragOver={(event) => {
          event.preventDefault();
          if (disabled || upload.isPending) return;
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <span className={styles.icon} aria-hidden="true">
          <Icon name="upload" size={18} />
        </span>
        <span className={styles.text}>
          <strong>{step ? UPLOAD_STEP_LABEL[step] : "上传报告"}</strong>
          <span>
            {step
              ? "请保持页面打开，上传完成后由教师发起批改。"
              : `拖拽文件到这里或点击选择，支持 ${extensions}，单个不超过 50 MB`}
          </span>
        </span>

        <input
          ref={inputRef}
          id={INPUT_ID}
          type="file"
          className="srOnly"
          accept={SUBMISSION_EXTENSIONS.map((extension) => `.${extension}`).join(",")}
          disabled={disabled || upload.isPending}
          onChange={(event) => {
            start(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
      </label>

      {error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}
    </div>
  );
}
