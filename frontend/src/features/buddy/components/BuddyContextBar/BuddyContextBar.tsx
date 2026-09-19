/**
 * 当前上下文条。
 *
 * 这里体现产品心智：Buddy 必须知道「当前课程 / 资料 / 作业 / 反馈」
 * （DEVELOPMENT_SPEC 第 8.3 与 9.2 节）。芯片只展示真实存在的对象名，
 * 解析不到具体名称时退回类型化标签，不编造标题。
 */

import { Icon } from "@/components/Icon/Icon";
import { useAssignment } from "@/features/assignments/hooks/useAssignments";
import { useBuddyContext } from "@/features/buddy/hooks/useBuddy";
import type { BuddyEntityType } from "@/features/buddy/model/types";
import { useCourse } from "@/features/courses/hooks/useCourses";

import styles from "./BuddyContextBar.module.css";

/** 没有业务对象时，用路由说明「你在哪一页」 */
const routeLabels: Array<[RegExp, string]> = [
  [/^\/$/, "首页"],
  [/^\/courses$/, "我的课程"],
  [/^\/tasks$/, "任务"],
  [/^\/workspace$/, "学习空间"],
  [/\/assignments\/[^/]+$/, "作业详情"],
  [/\/assignments$/, "作业列表"],
  [/\/materials\/[^/]+$/, "资料阅读"],
  [/\/materials$/, "课程资料"],
  [/\/learn/, "学习空间"],
  [/\/grades$/, "成绩与反馈"],
  [/\/grading\//, "AI 批改"],
  [/\/manage/, "课程管理"],
  [/^\/courses\/[^/]+$/, "课程概览"],
];

const entityLabels: Record<BuddyEntityType, string> = {
  course: "当前课程",
  material: "课程资料",
  "material-section": "章节内容",
  assignment: "当前作业",
  submission: "我的提交",
  practice: "练习",
  grade: "成绩反馈",
};

function labelForRoute(route: string): string {
  const matched = routeLabels.find(([pattern]) => pattern.test(route));
  return matched ? matched[1] : "当前页面";
}

export function BuddyContextBar() {
  const context = useBuddyContext();

  const courseQuery = useCourse(context.courseId);
  const assignmentQuery = useAssignment(
    context.entityType === "assignment" ? context.entityId : undefined,
  );

  const chips: string[] = [];

  const courseName = courseQuery.data?.name;
  if (courseName) {
    chips.push(courseName);
  }

  if (context.entityType === "assignment") {
    const title = assignmentQuery.data?.title;
    chips.push(title ?? entityLabels.assignment);
  } else if (context.entityType) {
    chips.push(entityLabels[context.entityType]);
  }

  if (context.sectionId) chips.push("章节定位");
  if (context.selectedText) chips.push("已选中文字");

  if (chips.length === 0) chips.push(labelForRoute(context.route));

  return (
    <div className={styles.bar}>
      <span className={styles.label}>
        <Icon name="context" size={13} />
        当前上下文
      </span>
      <ul className={styles.chips}>
        {chips.map((chip) => (
          <li key={chip} className={styles.chip}>
            {chip}
          </li>
        ))}
      </ul>
    </div>
  );
}
