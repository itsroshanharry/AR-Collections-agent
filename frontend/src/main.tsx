import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/toast";
import { AuthProvider } from "./lib/auth";
import "./index.css";
import { CallQueue } from "./pages/CallQueue";
import { Cash } from "./pages/Cash";
import { Customer } from "./pages/Customer";
import { Customers } from "./pages/Customers";
import { Experiment } from "./pages/Experiment";
import { Experiments } from "./pages/Experiments";
import { Invoice } from "./pages/Invoice";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { Scenario } from "./pages/Scenario";
import { WorkQueues } from "./pages/WorkQueues";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 10_000, refetchOnWindowFocus: true, retry: 1 },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <AuthProvider>
        <BrowserRouter basename="/app">
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<Overview />} />
              <Route path="calls" element={<CallQueue />} />
              <Route path="customers" element={<Customers />} />
              <Route path="customers/:customerId" element={<Customer />} />
              <Route path="invoices/:invoiceId" element={<Invoice />} />
              <Route path="tasks" element={<WorkQueues />} />
              <Route path="cash" element={<Cash />} />
              <Route path="experiments" element={<Experiments />} />
              <Route path="experiments/:experimentId" element={<Experiment />} />
              <Route path="experiments/:experimentId/scenarios/:scenarioId" element={<Scenario />} />
              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </BrowserRouter>
        </AuthProvider>
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
