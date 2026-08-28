import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { Navigate, Route, HashRouter as Router, Routes } from "react-router-dom";
import Ask from "./pages/Ask";
import History from "./pages/History";
import Landing from "./pages/Landing";
import { useTheme } from "./hooks/useTheme";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { refetchOnWindowFocus: false, retry: 1 },
  },
});

function ThemeInit() {
  // Applies the persisted/default theme to <html data-theme> on first
  // paint; the hook itself keeps it in sync afterward.
  useTheme();
  return null;
}

export default function App() {
  useEffect(() => {
    document.title = "EvidenceBoard · verification-first clinical evidence";
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeInit />
      <Router>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/history" element={<History />} />
          <Route path="/history/:id" element={<History />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Router>
    </QueryClientProvider>
  );
}
