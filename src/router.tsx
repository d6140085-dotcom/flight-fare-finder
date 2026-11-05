import { createBrowserRouter, type RouteObject } from "react-router";

import { NotFoundPage, RootErrorPage, RootLayout } from "./pages/RootLayout";
import Index from "./pages/Index";
import AppPage, { appLoader } from "./pages/AppPage";
import SignIn from "./pages/SignIn";
import SignUp from "./pages/SignUp";
import PrototypeSubscriptions from "./pages/PrototypeSubscriptions";

// M1.4 prototype (mock data only). Registered in local dev and Vercel preview builds; Vercel
// production builds set VITE_M14_PROTOTYPE=false (vite.config.ts), so the route doesn't exist there.
export const M14_PROTOTYPE_ENABLED = import.meta.env.VITE_M14_PROTOTYPE !== "false";
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
