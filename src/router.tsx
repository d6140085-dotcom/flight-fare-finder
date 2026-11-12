import { createBrowserRouter, type RouteObject } from "react-router";

import { NotFoundPage, RootErrorPage, RootLayout } from "./pages/RootLayout";
import Index from "./pages/Index";
import AppPage, { appLoader } from "./pages/AppPage";
import SignIn from "./pages/SignIn";
import SignUp from "./pages/SignUp";
import M14Subscriptions from "./pages/M14Subscriptions";

import { M14_APP_PATH, M14_PROTOTYPE_ENABLED, M14_PROTOTYPE_PATH } from "./lib/m14/routes";

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
        ? [
            { path: M14_PROTOTYPE_PATH, element: <M14Subscriptions mode="preview" /> },
            { path: M14_APP_PATH, element: <M14Subscriptions mode="app" />, loader: appLoader },
          ]
        : []),
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export const createAppRouter = () => createBrowserRouter(routes);
