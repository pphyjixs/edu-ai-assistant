/** 应用装配根。Provider 与路由的组装顺序在这里一目了然。 */

import { AppProviders } from "./providers";
import { AppRoutes } from "./router";

export function App() {
  return (
    <AppProviders>
      <AppRoutes />
    </AppProviders>
  );
}
