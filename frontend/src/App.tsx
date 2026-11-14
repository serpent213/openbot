import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import SaveNotification from "./components/SaveNotification";
import CustomCaret from "./components/CustomCaret";
import Layout from "./components/Layout";
import ApiKeyGate from "./components/ApiKeyGate";
import BackendFallback from "./components/BackendFallback";
import SetupGate from "./components/SetupGate";
const InboxPage = lazy(() => import("./pages/InboxPage"));
const ThreadsPage = lazy(() => import("./pages/ThreadsPage"));
const ThreadPage = lazy(() => import("./pages/ThreadPage"));
const BotsPage = lazy(() => import("./pages/BotsPage"));
const BotEditorPage = lazy(() => import("./pages/BotEditorPage"));
const BotPage = lazy(() => import("./pages/BotPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const ScheduledPage = lazy(() => import("./pages/ScheduledPage"));
import { pluginPages } from "./plugins/registry";
import DocumentTitle from "./components/DocumentTitle";
import PwaUpdates from "./components/PwaUpdates";
import { Spinner } from "./components/ui";

// Internal 3D character lab: lazy so three.js stays out of the main bundle, and outside the
// backend gates because it only needs static assets.
const OpenlingsLabPage = lazy(() => import("./pages/OpenlingsLabPage"));
const OfficeLabPage = lazy(() => import("./pages/OfficeLabPage"));

export default function App() {
  return (
    <>
    <DocumentTitle />
    <PwaUpdates />
    <Routes>
      <Route path="/lab/openlings" element={
        <Suspense fallback={<div className="flex h-dvh items-center justify-center"><Spinner /></div>}><OpenlingsLabPage /></Suspense>
      } />
      <Route path="/lab/office" element={
        <Suspense fallback={<div className="flex h-dvh items-center justify-center"><Spinner /></div>}><OfficeLabPage /></Suspense>
      } />
      <Route path="*" element={<MainApp />} />
    </Routes>
    </>
  );
}

function MainApp() {
  return (
    <>
    <CustomCaret />
    <SaveNotification />
    <ApiKeyGate>
      <BackendFallback>
        <SetupGate>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Navigate to="/inbox" replace />} />
            <Route path="/inbox" element={<InboxPage />} />
            <Route path="/threads" element={<ThreadsPage />} />
            <Route path="/threads/:id" element={<ThreadPage />} />
            <Route path="/bots" element={<BotsPage />} />
            <Route path="/bots/new" element={<BotEditorPage />} />
            <Route path="/bots/:id" element={<BotPage />} />
            <Route path="/settings/*" element={<SettingsPage />} />
            <Route path="/scheduled" element={<ScheduledPage />} />
            {pluginPages.map(({ path, component: Page }) => <Route key={path} path={path} element={<Page />} />)}
          </Route>
        </Routes>
        </SetupGate>
      </BackendFallback>
    </ApiKeyGate>
    </>
  );
}
