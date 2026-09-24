/**
 * 作业附件（契约 8.15）。
 *
 * 一个组件两种视角，因为两边的**能力**不同：
 *
 * - 课程创建教师：可以上传、删除，看到上传者与校验和；
 * - 学生与其他成员：只能查看与下载。
 *
 * 附件列表里带的是**短时有效的下载地址**，所以不做长期缓存；页面每次加载都会
 * 重新取一次，过期后刷新页面即可。删除后地址随之失效，这是预期行为。
 */

import { useRef, useState, type DragEvent } from "react";

import { Button } from "@/components/Button/Button";
import { Card } from "@/components/Card/Card";
import { Icon } from "@/components/Icon/Icon";
import { SkeletonLines } from "@/components/Skeleton/Skeleton";
import {
  useAssignmentAttachments,
  useDeleteAssignmentAttachment,
  useUploadAssignmentAttachment,
} from "@/features/assignments/hooks/useAssignments";
import { UPLOAD_STEP_LABEL } from "@/features/assignments/model/attachment";
import { toAppError } from "@/services/http";
import { ACCEPTED_EXTENSIONS, DEFAULT_MAX_UPLOAD_BYTES } from "@/services/upload";

import styles from "./AttachmentPanel.module.css";

const INPUT_ID = "assignment-attachment-input";

export type AttachmentPanelProps = {
  assignmentId: string;
  /** 当前用户是否为课程创建教师（决定能否上传/删除） */
  isOwner: boolean;
  /** 归档课程或归档任务：只读 */
  readOnly: boolean;
};

export function AttachmentPanel({
  assignmentId,
  isOwner,
  readOnly,
}: AttachmentPanelProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  const attachments = useAssignmentAttachments(assignmentId);
  const upload = useUploadAssignmentAttachment(assignmentId);
  const remove = useDeleteAssignmentAttachment(assignmentId);

  const canWrite = isOwner && !readOnly;
  const uploadError = upload.isError ? toAppError(upload.error) : null;
  const deleteError = remove.isError ? toAppError(remove.error) : null;

  function start(file: File | undefined) {
    if (!file || upload.isPending) return;
    upload.mutate(file);
  }

  function handleDrop(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    start(event.dataTransfer.files[0]);
  }

  const items = attachments.data ?? [];
  const maxMb = Math.floor(DEFAULT_MAX_UPLOAD_BYTES / 1024 / 1024);

  return (
    <Card>
      <div className={styles.head}>
        <h2 className={styles.title}>作业附件</h2>
        <span className={styles.count}>
          {attachments.isPending ? "读取中…" : `${items.length} 个附件`}
        </span>
      </div>

      <p className={styles.hint}>
        {canWrite
          ? `上传参考文件（${ACCEPTED_EXTENSIONS.join(" / ").toUpperCase()}），学生可以直接下载。`
          : "教师提供的参考资料，可以下载查看。"}
      </p>

      {canWrite ? (
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
          <span className={styles.dropText}>
            <strong>
              {upload.step ? UPLOAD_STEP_LABEL[upload.step] : "上传附件"}
            </strong>
            <span>拖拽文件到这里或点击选择，单个不超过 {maxMb} MB</span>
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
      ) : null}

      {uploadError ? (
        <p className={styles.error} role="alert">
          {uploadError.message}
        </p>
      ) : null}
      {deleteError ? (
        <p className={styles.error} role="alert">
          {deleteError.message}
        </p>
      ) : null}

      {attachments.isPending ? (
        <div aria-busy="true">
          <SkeletonLines lines={2} />
        </div>
      ) : attachments.isError ? (
        <p className={styles.empty}>
          附件读取失败：
          {toAppError(attachments.error).message}
        </p>
      ) : items.length === 0 ? (
        <p className={styles.empty}>
          {canWrite ? "还没有附件。" : "这份任务没有附件。"}
        </p>
      ) : (
        <ul className={styles.list}>
          {items.map((item) => (
            <li key={item.id} className={styles.row}>
              <span className={styles.fileIcon} aria-hidden="true">
                <Icon name="file" size={16} />
              </span>

              <div className={styles.info}>
                <span className={styles.filename} title={item.filename}>
                  {item.filename}
                </span>
                <span className={styles.meta}>
                  {item.typeLabel} · {item.sizeLabel}
                  {item.uploadedByName ? ` · 上传者 ${item.uploadedByName}` : ""}
                  {` · ${item.createdAtLabel}`}
                  {item.sha256 ? ` · 校验和 ${item.sha256Short}` : ""}
                </span>
              </div>

              <div className={styles.actions}>
                <a
                  className={styles.download}
                  href={item.downloadUrl}
                  target="_blank"
                  rel="noreferrer"
                  download={item.filename}
                  title={`下载地址有效期至 ${item.downloadExpiresLabel}`}
                >
                  下载
                </a>
                {canWrite ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={remove.isPending && remove.variables === item.id}
                    onClick={() => remove.mutate(item.id)}
                    aria-label={`删除附件 ${item.filename}`}
                  >
                    {remove.isPending && remove.variables === item.id ? "删除中…" : "删除"}
                  </Button>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
