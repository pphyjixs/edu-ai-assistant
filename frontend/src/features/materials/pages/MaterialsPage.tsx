/**
 * 课程资料页。
 *
 * 教师可以上传课件并看到解析进度；学生只能阅读已解析的大纲
 * （契约 5.1 两者都可以读列表，但只有创建教师能上传与删除）。
 */

import { useState } from "react";
import { useParams } from "react-router-dom";

import { Card } from "@/components/Card/Card";
import { AgentActionButton } from "@/features/buddy/components/AgentActionButton/AgentActionButton";
import { useSetBuddyContext } from "@/features/buddy/hooks/useBuddy";
import { useCourse } from "@/features/courses/hooks/useCourses";
import { MaterialList } from "@/features/materials/components/MaterialList/MaterialList";
import { MaterialParseProgress } from "@/features/materials/components/MaterialParseProgress/MaterialParseProgress";
import { MaterialUploader } from "@/features/materials/components/MaterialUploader/MaterialUploader";
import { useMaterials } from "@/features/materials/hooks/useMaterials";
import { toAppError } from "@/services/http";

import styles from "./MaterialsPage.module.css";

type ActiveJob = {
  jobId: string;
  materialId: string;
  filename: string;
};

export function MaterialsPage() {
  const { courseId } = useParams<{ courseId: string }>();
  const courseQuery = useCourse(courseId);
  const materialsQuery = useMaterials(courseId);
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(null);

  useSetBuddyContext({ courseId, entityType: "course", entityId: courseId, route: "" });

  const course = courseQuery.data;
  const canManage = Boolean(course?.isOwner);
  const readOnly = course?.status === "archived";
  const listError = materialsQuery.isError ? toAppError(materialsQuery.error) : null;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>课程资料</h1>
          <p className={styles.subtitle}>
            {materialsQuery.isPending
              ? "正在读取资料列表…"
              : `共 ${materialsQuery.data?.total ?? 0} 份资料`}
            {readOnly ? " · 课程已归档，保持只读" : ""}
          </p>
        </div>
        <AgentActionButton
          label="总结课程"
          prompt="请帮我总结这门课程资料里的主要内容。"
          agentAction="SUMMARIZE_CONTEXT"
        />
      </header>

      {canManage && !readOnly ? (
        <Card className={styles.uploadCard}>
          <h2 className={styles.cardTitle}>上传课件</h2>
          <p className={styles.cardHint}>
            支持 PDF、PPTX、DOCX。上传完成后系统会异步解析，生成章节大纲与知识点。
          </p>
          <MaterialUploader courseId={courseId ?? ""} onUploaded={setActiveJob} />
        </Card>
      ) : null}

      {activeJob ? (
        <div className={styles.progressSlot}>
          <MaterialParseProgress
            jobId={activeJob.jobId}
            courseId={courseId ?? ""}
            materialId={activeJob.materialId}
            filename={activeJob.filename}
          />
        </div>
      ) : null}

      <div className={styles.listSlot}>
        <MaterialList
          materials={materialsQuery.data?.items ?? []}
          isPending={materialsQuery.isPending}
          error={listError}
          onRetry={() => void materialsQuery.refetch()}
          courseId={courseId ?? ""}
          canManage={canManage}
          readOnly={readOnly}
        />
      </div>
    </div>
  );
}
