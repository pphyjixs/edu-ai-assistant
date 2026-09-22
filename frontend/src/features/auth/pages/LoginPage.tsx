/**
 * 登录 / 注册页。
 *
 * 契约要点（docs/api-contract.md 2.1 / 2.6）：
 * - 仅支持邮箱登录；邮箱本地部分大小写敏感，因此**不做** lowercase 或 trim 之外的处理；
 * - 密码 8–128 个字符，允许空格且大小写敏感，**不 trim**；
 * - 注册开放教师与学生两种角色，成功后由前端用同一凭证登录；
 * - 登录失败达到阈值返回 429，`details.retry_after_seconds` 用来做倒计时。
 */

import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { Button } from "@/components/Button/Button";
import { Icon } from "@/components/Icon/Icon";
import { useLogin, useRegister } from "@/features/auth/hooks/useAuthActions";
import { toAppError, type AppError } from "@/services/http";
import type { Schemas } from "@/types/api";

import styles from "./LoginPage.module.css";

type Mode = "login" | "register";

const PASSWORD_MIN = 8;
const PASSWORD_MAX = 128;
const DISPLAY_NAME_MAX = 64;

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const redirectTo = (location.state as { from?: string } | null)?.from ?? "/";

  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<Schemas["UserRole"]>("STUDENT");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<AppError | null>(null);
  const [cooldown, setCooldown] = useState(0);

  const login = useLogin();
  const register = useRegister();
  const busy = login.isPending || register.isPending;

  // 限流倒计时：契约 2.6 的 details.retry_after_seconds
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setInterval(() => setCooldown((value) => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [cooldown]);

  function validate(): boolean {
    const next: Record<string, string> = {};

    if (email.trim().length === 0) next.email = "请输入邮箱。";

    // 密码不做 trim：首尾空格是密码的一部分
    if (password.length < PASSWORD_MIN) {
      next.password = `密码至少 ${PASSWORD_MIN} 个字符。`;
    } else if (password.length > PASSWORD_MAX) {
      next.password = `密码最多 ${PASSWORD_MAX} 个字符。`;
    }

    if (mode === "register") {
      const name = displayName.trim();
      if (name.length === 0) next.displayName = "请输入姓名。";
      else if (name.length > DISPLAY_NAME_MAX) next.displayName = `姓名最多 ${DISPLAY_NAME_MAX} 个字符。`;
    }

    setFieldErrors(next);
    return Object.keys(next).length === 0;
  }

  async function submit() {
    setError(null);
    if (!validate()) return;

    try {
      if (mode === "login") {
        await login.mutateAsync({ email: email.trim(), password });
      } else {
        await register.mutateAsync({
          email: email.trim(),
          password,
          display_name: displayName.trim(),
          role,
        });
      }
      navigate(redirectTo, { replace: true });
    } catch (cause) {
      const appError = toAppError(cause);

      if (appError.code === "AUTH_TOO_MANY_ATTEMPTS") {
        const seconds = Number(appError.details.retry_after_seconds);
        if (Number.isFinite(seconds) && seconds > 0) setCooldown(Math.ceil(seconds));
      }

      // 字段级错误单独展示，其余走顶部提示
      const errors = appError.details.errors;
      if (Array.isArray(errors)) {
        const mapped: Record<string, string> = {};
        for (const item of errors) {
          const entry = item as { loc?: unknown; message?: unknown };
          const loc = Array.isArray(entry.loc) ? entry.loc[entry.loc.length - 1] : undefined;
          if (typeof loc === "string" && typeof entry.message === "string") {
            mapped[loc] = entry.message;
          }
        }
        if (Object.keys(mapped).length > 0) {
          setFieldErrors(mapped);
          return;
        }
      }

      setError(appError);
    }
  }

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
    setFieldErrors({});
  }

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <img src="/svg/icons/logo-studybuddy.svg" alt="" width={36} height={36} aria-hidden="true" />
          <div>
            <p className={styles.brandName}>StudyBuddy</p>
            <p className={styles.brandSub}>课程、资料、作业和 Buddy 都在同一个工作空间里</p>
          </div>
        </div>

        <div className={styles.tabs} role="tablist" aria-label="登录或注册">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "login"}
            className={mode === "login" ? `${styles.tab} ${styles.tabActive}` : styles.tab}
            onClick={() => switchMode("login")}
          >
            登录
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "register"}
            className={mode === "register" ? `${styles.tab} ${styles.tabActive}` : styles.tab}
            onClick={() => switchMode("register")}
          >
            注册
          </button>
        </div>

        <form
          className={styles.form}
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          {error ? (
            <p className={styles.formError} role="alert">
              {error.code === "AUTH_TOO_MANY_ATTEMPTS" && cooldown > 0
                ? `${error.message}（约 ${cooldown} 秒后可重试）`
                : error.message}
            </p>
          ) : null}

          <div className={styles.field}>
            <label className={styles.label} htmlFor="auth-email">
              邮箱
            </label>
            <input
              id="auth-email"
              className={styles.input}
              type="email"
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              aria-invalid={Boolean(fieldErrors.email)}
              aria-describedby={fieldErrors.email ? "auth-email-error" : undefined}
            />
            {fieldErrors.email ? (
              <p className={styles.fieldError} id="auth-email-error">
                {fieldErrors.email}
              </p>
            ) : null}
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="auth-password">
              密码
            </label>
            <input
              id="auth-password"
              className={styles.input}
              type="password"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={Boolean(fieldErrors.password)}
              aria-describedby={fieldErrors.password ? "auth-password-error" : "auth-password-hint"}
            />
            {fieldErrors.password ? (
              <p className={styles.fieldError} id="auth-password-error">
                {fieldErrors.password}
              </p>
            ) : (
              <p className={styles.hint} id="auth-password-hint">
                8–128 个字符，区分大小写，首尾空格会被保留。
              </p>
            )}
          </div>

          {mode === "register" ? (
            <>
              <div className={styles.field}>
                <label className={styles.label} htmlFor="auth-name">
                  姓名
                </label>
                <input
                  id="auth-name"
                  className={styles.input}
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  aria-invalid={Boolean(fieldErrors.display_name)}
                />
                {fieldErrors.display_name ? (
                  <p className={styles.fieldError}>{fieldErrors.display_name}</p>
                ) : null}
              </div>

              <fieldset className={styles.fieldset}>
                <legend className={styles.label}>身份</legend>
                <div className={styles.roles}>
                  {(
                    [
                      { value: "STUDENT", label: "学生", hint: "加入课程、完成练习、提交报告" },
                      { value: "TEACHER", label: "教师", hint: "创建课程、上传资料、生成练习" },
                    ] as const
                  ).map((option) => (
                    <label
                      key={option.value}
                      className={
                        role === option.value
                          ? `${styles.role} ${styles.roleActive}`
                          : styles.role
                      }
                    >
                      <input
                        type="radio"
                        name="role"
                        className="srOnly"
                        value={option.value}
                        checked={role === option.value}
                        onChange={() => setRole(option.value)}
                      />
                      <span className={styles.roleLabel}>{option.label}</span>
                      <span className={styles.roleHint}>{option.hint}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
            </>
          ) : null}

          <Button
            type="submit"
            variant="primary"
            block
            disabled={busy || cooldown > 0}
            iconLeft={busy ? undefined : "spark"}
          >
            {busy
              ? "处理中…"
              : cooldown > 0
                ? `${cooldown} 秒后可重试`
                : mode === "login"
                  ? "登录"
                  : "注册并登录"}
          </Button>

          <p className={styles.footnote}>
            <Icon name="context" size={13} />
            第一版开放注册，无需邀请码或审批；邮箱暂不验证归属。
          </p>
        </form>
      </div>
    </div>
  );
}
