import { createBrowserRouter, type RouteObject } from "react-router";

import { NotFoundPage, RootErrorPage, RootLayout } from "./pages/RootLayout";
import Index from "./pages/Index";
import AppPage, { appLoader } from "./pages/AppPage";
import SignIn from "./pages/SignIn";
import SignUp from "./pages/SignUp";

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
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export const createAppRouter = () => createBrowserRouter(routes);
