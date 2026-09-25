/**
 * 测试用的 HTTP 路由桩。
 *
 * 前端所有请求都经过 ``@/services/http``，因此测试只需要替换其中的 ``http``
 * 对象（各测试文件里用 ``vi.mock`` 完成），再在这里声明"哪个路径返回什么"。
 *
 * 未声明的请求会**直接失败**，避免测试悄悄漏掉一次真实调用后仍然通过。
 */

export type HttpRouteRequest = {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  /** 去掉查询串的路径，例如 ``/chat-sessions/xxx/messages`` */
  path: string;
  body?: unknown;
};

export type HttpRouteHandler = (request: HttpRouteRequest) => unknown;
export type HttpRoute = [string | RegExp, HttpRouteHandler];

type MockFn = {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  mockImplementation: (impl: (...args: any[]) => unknown) => void;
  mockReset: () => void;
};

export type HttpMockLike = {
  get: MockFn;
  post: MockFn;
  patch: MockFn;
  delete: MockFn;
};

/**
 * 把路由表安装到 ``http`` 的 mock 上。
 *
 * 匹配使用**去掉查询串**的裸路径：真实调用会带 ``?page=1&page_size=20``。
 */
export function installHttpRoutes(httpMock: HttpMockLike, routes: HttpRoute[]): void {
  const resolve = (
    method: HttpRouteRequest["method"],
    path: string,
    body?: unknown,
  ): Promise<unknown> => {
    const bare = path.split("?")[0];
    for (const [pattern, handler] of routes) {
      const matched =
        typeof pattern === "string" ? bare === pattern : pattern.test(bare);
      if (matched) {
        return Promise.resolve(handler({ method, path: bare, body }));
      }
    }
    return Promise.reject(new Error(`测试未覆盖的请求：${method} ${bare}`));
  };

  httpMock.get.mockImplementation((path: string) => resolve("GET", path));
  httpMock.post.mockImplementation((path: string, body?: unknown) =>
    resolve("POST", path, body),
  );
  httpMock.patch.mockImplementation((path: string, body?: unknown) =>
    resolve("PATCH", path, body),
  );
  httpMock.delete.mockImplementation((path: string) => resolve("DELETE", path));
}
