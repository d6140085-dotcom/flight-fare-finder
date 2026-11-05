import { createBrowserRouter, type RouteObject } from "react-router";

import { NotFoundPage, RootErrorPage, RootLayout } from "./pages/RootLayout";
import Index from "./pages/Index";
import AppPage, { appLoader } from "./pages/AppPage";
import SignIn from "./pages/SignIn";
import SignUp from "./pages/SignUp";
import PrototypeSubscriptions from "./pages/PrototypeSubscriptions";

// M1.4 prototype (mock data only). Fail-closed: the route exists only when the build set
// VITE_M14_PROTOTYPE to exactly "true" (dev server and Vercel preview builds; see
// src/lib/prototype-flag.ts and vite.config.ts). Unset or any other value means no route.
export const M14_PROTOTYPE_ENABLED = import.meta.env.VITE_M14_PROTOTYPE === "true";
export const M14_PROTOTYPE_PATH = "prototype/subscriptions";

export const routes: RouteObject[] = [
  {
    path: "/",
    element: <RootLayout />,
    errorElement: <RootErrorPage />,
    children: [
      { index: true, element: <Index /> },
      { path: "app", element: <AppPage />, loader: appLoader },
      { path: "signin", element: <SignIn /> },
      { path: "signup", element: <SignUp /> },
      ...(M14_PROTOTYPE_ENABLED
        ? [{ path: M14_PROTOTYPE_PATH, element: <PrototypeSubscriptions /> }]
        : []),
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export const createAppRouter = () => createBrowserRouter(routes);
