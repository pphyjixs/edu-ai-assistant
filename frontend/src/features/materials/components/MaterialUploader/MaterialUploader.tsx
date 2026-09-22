/**
 * 课程资料上传（仅课程创建教师）。
 *
 * 走契约 4.1 的三段式：初始化 → 浏览器直传对象存储 → 完成确认。
 * 每一步都有可感知的提示，避免出现「点了没反应」的等待。
 */

import { useRef, useState, type DragEvent } from "react";

import { Icon } from "@/components/Icon/Icon";
import { UPLOAD_STEP_LABEL, type UploadStep } from "@/features/materials/api/upload";
import { useUploadMaterial } from "@/features/materials/hooks/useMaterials";
import { toAppError } from "@/services/http";
import { ACCEPTED_EXTENSIONS, DEFAULT_MAX_UPLOAD_BYTES } from "@/services/upload";

import styles from "./MaterialUploader.module.css";

const INPUT_ID = "material-upload-input";

export type MaterialUploaderProps = {
  courseId: string;
  /** 上传成功后回传任务信息，交给上层的解析进度组件轮询 */
  onUploaded: (result: { jobId: string; materialId: string; filename: string }) => void;
};

export function MaterialUploader({ courseId, onUploaded }: MaterialUploaderProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const upload = useUploadMaterial(courseId);

  const error = upload.isError ? toAppError(upload.error) : null;
  const step: UploadStep | null = upload.step;

  function start(file: File | undefined) {
    if (!file || upload.isPending) return;
    upload.mutate(file, {
      onSuccess: (result) => {
        onUploaded({
          jobId: result.job.id,
          materialId: result.material.id,
          filename: result.material.filename,
        });
      },
    });
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    start(event.dataTransfer.files[0]);
  }

  const maxMb = Math.floor(DEFAULT_MAX_UPLOAD_BYTES / 1024 / 1024);

  return (
    <div>
      <label
        htmlFor={INPUT_ID}
        className={dragging ? `${styles.dropzone} ${styles.active}` : styles.dropzone}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <span className={styles.icon} aria-hidden="true">
          <Icon name="upload" size={18} />
        </span>
        <span className={styles.text}>
          <strong>{step ? UPLOAD_STEP_LABEL[step] : "拖拽课件到这里"}</strong>
          <span>
            {step
              ? "请保持页面打开，上传完成后会自动开始解析。"
              : `或点击选择文件，支持 ${ACCEPTED_EXTENSIONS.join(" / ").toUpperCase()}，单个不超过 ${maxMb} MB`}
          </span>
        </span>

        <input
          ref={inputRef}
          id={INPUT_ID}
          type="file"
          className="srOnly"
          accept={ACCEPTED_EXTENSIONS.map((extension) => `.${extension}`).join(",")}
          disabled={upload.isPending}
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
